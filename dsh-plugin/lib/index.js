/**
 * dsh-downloadx — DeepSeek Harness 插件。
 *
 * 把本机「刃研社-DownloadX」下载引擎（Python，本地 HTTP + Agent API）注册成模型可调用的工具，
 * 并负责引擎服务的健康检查与按需拉起。
 *
 * 设计约束：
 * - 零 `@deepseek-ai/*` 导入：第三方插件在 profile 里解析官方包并不可靠，本插件只用 Node 内置模块；
 * - 下载引擎独立于模型运行：插件只做确定性的 HTTP 调用与进程托管，不在下载路径里放任何 LLM；
 * - 引擎进程以 detached 方式后台运行：DSH 退出后下载继续，下次会话通过健康检查复用。
 */

import { spawn } from 'node:child_process'
import { closeSync, openSync, readdirSync } from 'node:fs'
import { readFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute, join } from 'node:path'

/** 稳定 Loader 身份（profile 的 cordis.patch.yml 按 id 覆写本行）。 */
export const name = 'downloadx'
/** 工具注册表由 dsh-tools 提供；插件必须在它之后激活。 */
export const inject = ['tools']

const PLUGIN_VERSION = '0.1.0'
const DEFAULT_BASE_URL = 'http://127.0.0.1:8720'
const DEFAULT_PYTHON = 'python'
const HEALTH_TIMEOUT_MS = 2500
const HEALTH_POLL_MS = 300
const DEFAULT_START_TIMEOUT_MS = 30000
const DEFAULT_REQUEST_TIMEOUT_MS = 15000
const PROBE_TIMEOUT_MS = 60000
const DEFAULT_LIST_LIMIT = 10
const MAX_LIST_LIMIT = 100
const TASK_STATES = [
  'queued',
  'downloading',
  'paused',
  'verifying',
  'completed',
  'failed',
  'canceled',
]
const LIST_ORDERS = ['created', 'priority', 'updated']

/** 工具统一返回内容：给模型看的紧凑文本 + 稳定结构。 */
const OUTPUT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean', description: '调用是否成功' },
    text: { type: 'string', description: '给模型阅读的结果文本' },
  },
  required: ['ok', 'text'],
  additionalProperties: false,
}

// ────────────────────────────────────────────────────────────────────────────
// 配置
// ────────────────────────────────────────────────────────────────────────────

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function positiveInteger(value, fallback) {
  const number = Number(value)
  return Number.isInteger(number) && number > 0 ? number : fallback
}

function normalizeBaseUrl(value) {
  const raw = typeof value === 'string' && value.trim() ? value.trim() : DEFAULT_BASE_URL
  const url = new URL(raw)
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`dsh-downloadx: baseUrl 必须是 http(s) 地址，收到 ${raw}`)
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, '')}`
}

/**
 * 合并 patch config 与默认值。没有导出 Config schema 时 Cordis 会把 patch 里的原始对象
 * 原样传给 apply，因此这里做一次手写校验，非法值直接抛错（插件行变为启动失败，日志可见）。
 */
export function normalizeConfig(raw) {
  const config = isPlainObject(raw) ? raw : {}
  const baseUrl = normalizeBaseUrl(config.baseUrl ?? process.env.DOWNLOADX_BASE_URL)
  const port = Number(new URL(baseUrl).port || (baseUrl.startsWith('https:') ? 443 : 80))
  const python =
    typeof config.python === 'string' && config.python.trim()
      ? config.python.trim()
      : typeof process.env.DOWNLOADX_PYTHON === 'string' && process.env.DOWNLOADX_PYTHON.trim()
        ? process.env.DOWNLOADX_PYTHON.trim()
        : undefined // 未显式配置时交给自动发现（桌面宿主的 PATH 可能不含 Python）
  const token =
    typeof config.token === 'string'
      ? config.token
      : typeof process.env.DOWNLOADX_TOKEN === 'string'
        ? process.env.DOWNLOADX_TOKEN
        : ''
  return {
    baseUrl,
    port,
    python,
    token,
    autoStart: config.autoStart !== false,
    stateDir: typeof config.stateDir === 'string' && config.stateDir.trim() ? config.stateDir.trim() : undefined,
    downloadDir:
      typeof config.downloadDir === 'string' && config.downloadDir.trim()
        ? config.downloadDir.trim()
        : undefined,
    startTimeoutMs: positiveInteger(config.startTimeoutMs, DEFAULT_START_TIMEOUT_MS),
    requestTimeoutMs: positiveInteger(config.requestTimeoutMs, DEFAULT_REQUEST_TIMEOUT_MS),
    logPath:
      typeof config.logPath === 'string' && config.logPath.trim()
        ? config.logPath.trim()
        : join(tmpdir(), `dsh-downloadx-engine-${port}.log`),
  }
}

// ────────────────────────────────────────────────────────────────────────────
// 进程/HTTP 基础工具
// ────────────────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function safeLog(ctx, level, message) {
  try {
    ctx?.logger?.[level]?.(`[dsh-downloadx] ${message}`)
  } catch {
    /* logger 不可用时静默 */
  }
}

async function readLogTail(path, maxBytes = 1600) {
  const text = await readFile(path, 'utf8')
  return text.length > maxBytes ? text.slice(-maxBytes) : text
}

function pickDetail(data, text) {
  if (isPlainObject(data)) {
    if (typeof data.detail === 'string') return data.detail
    if (typeof data.error === 'string') return data.error
    if (isPlainObject(data.detail)) {
      try {
        return JSON.stringify(data.detail)
      } catch {
        return String(data.detail)
      }
    }
  }
  return typeof text === 'string' ? text.slice(0, 400).trim() : ''
}

async function fetchJson(baseUrl, pathname, options = {}) {
  const {
    method = 'GET',
    body,
    timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
    signal,
    token = '',
    headers = {},
  } = options
  const timeout = AbortSignal.timeout(Math.max(1, timeoutMs))
  const combined =
    signal !== undefined && typeof AbortSignal.any === 'function'
      ? AbortSignal.any([signal, timeout])
      : timeout
  const requestHeaders = { ...headers }
  if (body !== undefined) requestHeaders['content-type'] = 'application/json'
  if (token) requestHeaders.authorization = `Bearer ${token}`
  const response = await fetch(baseUrl + pathname, {
    method,
    headers: requestHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: combined,
  })
  const text = await response.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!response.ok) {
    const detail = pickDetail(data, text)
    const error = new Error(detail || `HTTP ${response.status}`)
    error.httpStatus = response.status
    error.detail = detail
    throw error
  }
  return data
}

// ────────────────────────────────────────────────────────────────────────────
// 引擎托管：健康检查 + 按需拉起 + 工具调用
// ────────────────────────────────────────────────────────────────────────────

/**
 * 创建一个与 DownloadX 引擎对话的客户端。引擎进程由本插件按需 detached 拉起，
 * 因此 DSH 退出后下载不中断；下次启动时健康检查通过即直接复用。
 */
function createEngine(config, ctx) {
  let startPromise = null
  let spawnedPid = null

  function engineUrl(pathname) {
    return config.baseUrl + pathname
  }

  /** @returns {Promise<{state:'ready',data:object}|{state:'foreign'}|{state:'down',error:unknown}>} */
  async function probe(timeoutMs = HEALTH_TIMEOUT_MS) {
    try {
      const data = await fetchJson(config.baseUrl, '/api/health', { timeoutMs, token: config.token })
      if (isPlainObject(data) && data.app === 'DownloadX' && data.status === 'ok') {
        return { state: 'ready', data }
      }
      return { state: 'foreign', data }
    } catch (error) {
      if (error?.httpStatus !== undefined) return { state: 'foreign', error }
      return { state: 'down', error }
    }
  }

  async function health(timeoutMs = HEALTH_TIMEOUT_MS) {
    const result = await probe(timeoutMs)
    return result.state === 'ready' ? result.data : null
  }

  // ── 状态目录 / 锁文件 ─────────────────────────────────────────────────────
  // DownloadX 没有 /api/shutdown、也没有 pid 文件，唯一的 pid 来源是 state_dir 下的
  // downloadx.lock（5s 心跳刷新，30s 不刷新视为崩溃残留）。硬杀后 30s 内重启会报
  // “已有 DownloadX 服务在运行”，所以自有的崩溃残留锁要主动清理。

  function defaultStateDir() {
    if (process.env.DOWNLOADX_STATE_DIR) return process.env.DOWNLOADX_STATE_DIR
    if (process.platform === 'win32') {
      return process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, 'DownloadX') : join(tmpdir(), 'DownloadX')
    }
    const home = process.env.HOME || tmpdir()
    if (process.platform === 'darwin') return join(home, 'Library', 'Application Support', 'DownloadX')
    return join(process.env.XDG_DATA_HOME || join(home, '.local', 'share'), 'DownloadX')
  }

  function stateDir() {
    return config.stateDir || defaultStateDir()
  }

  function lockPath() {
    return join(stateDir(), 'downloadx.lock')
  }

  function pidAlive(pid) {
    try {
      process.kill(pid, 0)
      return true
    } catch (error) {
      return error?.code === 'EPERM'
    }
  }

  async function readLock() {
    try {
      const data = JSON.parse(await readFile(lockPath(), 'utf8'))
      if (!isPlainObject(data) || !Number.isInteger(data.pid) || data.pid <= 0) return null
      let ageSeconds = Number.POSITIVE_INFINITY
      try {
        ageSeconds = (Date.now() - (await stat(lockPath())).mtimeMs) / 1000
      } catch {
        ageSeconds = Number.POSITIVE_INFINITY
      }
      return { pid: data.pid, ageSeconds, host: data.host ?? null, alive: pidAlive(data.pid) }
    } catch {
      return null
    }
  }

  /** 锁已陈旧（进程不在或心跳 > 30s 未刷新）就删掉；返回是否清理了。 */
  async function cleanupStaleLock(lock) {
    if (!lock) return false
    if (lock.alive && lock.ageSeconds <= 30) return false
    try {
      await rm(lockPath(), { force: true })
      safeLog(ctx, 'info', `清理 DownloadX 残留锁（pid ${lock.pid}，心跳 ${Math.round(lock.ageSeconds)}s 前）`)
      return true
    } catch {
      return false
    }
  }

  /** 结束后清理仍属于被结束进程的锁，避免接下来 30s 内无法重启。 */
  async function removeLockForPid(pid) {
    const lock = await readLock()
    if (lock && lock.pid === pid) {
      try {
        await rm(lockPath(), { force: true })
      } catch {
        /* 清理失败不影响结束结果 */
      }
    }
  }

  // ── 任务 ID 前缀解析 ──────────────────────────────────────────────────────
  // 引擎只接受 32 位十六进制完整 ID（短 ID 直接 404）；这里按唯一前缀在插件侧解析。
  const FULL_ID_RE = /^[0-9a-f]{32}$/i
  const PREFIX_RE = /^[0-9a-f]{4,31}$/i

  async function resolveTaskId(raw) {
    const id = String(raw ?? '').trim()
    if (FULL_ID_RE.test(id)) return id.toLowerCase()
    if (!PREFIX_RE.test(id)) return id
    const result = await callTool('list_downloads', { limit: MAX_LIST_LIMIT })
    const tasks = Array.isArray(result?.tasks) ? result.tasks : []
    const prefix = id.toLowerCase()
    const matches = tasks.filter((task) => String(task?.id ?? '').toLowerCase().startsWith(prefix))
    if (matches.length === 1) return matches[0].id
    if (matches.length === 0) {
      throw new Error(`找不到以 "${id}" 开头的任务。先用 downloadx_list 查看任务列表。`)
    }
    throw new Error(`任务前缀 "${id}" 不唯一（匹配 ${matches.length} 个），请提供更长的 ID。`)
  }

  // ── 启动器发现 ────────────────────────────────────────────────────────────
  // 桌面应用进程的 PATH 可能不完整（实测 `spawn('python')` 会 ENOENT，即使终端里 python 可用），
  // 所以不能只依赖命令名：这里枚举常见安装位置 + pip --user 的 downloadx.exe，并逐个试跑
  // `--version` 选中真正装了 downloadx 的那个解释器。
  let launcherPromise = null

  function readDirSafe(path) {
    try {
      return readdirSync(path, { withFileTypes: true })
    } catch {
      return []
    }
  }

  /** 枚举候选启动器：显式配置 → PATH 命令名 → pip --user 入口 → 常见安装目录绝对路径。 */
  function candidateLaunchers() {
    const candidates = []
    const seen = new Set()
    const add = (file, prefix, label) => {
      if (!file) return
      const key = `${file}|${prefix.join(' ')}`
      if (seen.has(key)) return
      seen.add(key)
      candidates.push({ file, prefix, label })
    }
    const env = process.env
    if (config.python) {
      add(config.python, ['-m', 'downloadx'], config.python)
      return candidates
    }
    for (const name of ['python', 'python3']) add(name, ['-m', 'downloadx'], name)
    add('py', ['-3', '-m', 'downloadx'], 'py -3')

    const appData = env.APPDATA
    const localAppData = env.LOCALAPPDATA
    const userProfile = env.USERPROFILE
    // pip --user 安装的可执行入口：%APPDATA%\Python\Python3xx\Scripts\downloadx.exe
    if (appData) {
      for (const entry of readDirSafe(join(appData, 'Python'))) {
        if (/^Python\d+/i.test(entry.name)) {
          add(join(appData, 'Python', entry.name, 'Scripts', 'downloadx.exe'), [], `downloadx.exe (${entry.name})`)
        }
      }
    }
    if (localAppData) {
      for (const entry of readDirSafe(join(localAppData, 'Programs', 'Python'))) {
        if (/^Python\d+/i.test(entry.name)) {
          add(join(localAppData, 'Programs', 'Python', entry.name, 'Scripts', 'downloadx.exe'), [], `downloadx.exe (${entry.name})`)
        }
      }
    }
    // 常见 Python 安装目录里的绝对路径
    const installRoots = []
    if (env.ProgramFiles) installRoots.push(env.ProgramFiles)
    if (env['ProgramFiles(x86)']) installRoots.push(env['ProgramFiles(x86)'])
    if (localAppData) installRoots.push(join(localAppData, 'Programs', 'Python'))
    if (userProfile) installRoots.push(join(userProfile, 'AppData', 'Local', 'Programs', 'Python'))
    for (const root of installRoots) {
      for (const entry of readDirSafe(root)) {
        if (!/^Python\d+/i.test(entry.name)) continue
        add(join(root, entry.name, 'python.exe'), ['-m', 'downloadx'], `python (${entry.name})`)
      }
      add(join(root, 'python.exe'), ['-m', 'downloadx'], `python (${root})`)
    }
    return candidates
  }

  /**
   * 试跑 `<launcher> --version`，确认这个启动方式真的能跑 DownloadX。
   * 输出写临时文件而不是 pipe：宿主进程可能处在受限环境，piped stdio 不一定可用，
   * 而文件重定向在普通用户权限下总是可用。
   */
  let probeSequence = 0
  async function probeLauncher(candidate) {
    probeSequence += 1
    const outPath = join(tmpdir(), `dsh-downloadx-probe-${process.pid}-${probeSequence}.log`)
    let logFd = -1
    try {
      logFd = openSync(outPath, 'w')
    } catch {
      logFd = -1
    }
    return new Promise((resolve) => {
      let settled = false
      const finish = async (result) => {
        if (settled) return
        settled = true
        try {
          await rm(outPath, { force: true })
        } catch {
          /* ignore */
        }
        resolve(result)
      }
      let child
      try {
        child = spawn(candidate.file, [...candidate.prefix, '--version'], {
          windowsHide: true,
          stdio: logFd >= 0 ? ['ignore', logFd, logFd] : 'ignore',
          env: process.env,
        })
      } catch (error) {
        finish({ ok: false, error: error?.message ?? String(error) })
        return
      } finally {
        if (logFd >= 0) {
          try {
            closeSync(logFd)
          } catch {
            /* ignore */
          }
        }
      }
      const timer = setTimeout(() => {
        try {
          child.kill()
        } catch {
          /* ignore */
        }
        finish({ ok: false, error: '探测超时（8s）' })
      }, 8000)
      child.once('error', (error) => {
        clearTimeout(timer)
        finish({ ok: false, error: error?.message ?? String(error) })
      })
      child.once('exit', async (code) => {
        clearTimeout(timer)
        let output = ''
        try {
          output = String(await readFile(outPath, 'utf8')).trim()
        } catch {
          output = ''
        }
        const broken = /No module named|ModuleNotFoundError|Traceback \(most recent|not recognized|不是内部或外部命令/i.test(output)
        const versionMatch = output.match(/\d+\.\d+(?:\.\d+)?/)
        if (!broken && versionMatch && !output.startsWith('Error')) {
          finish({ ok: true, version: output.split(/\r?\n/)[0] })
        } else {
          const detail = output ? output.split(/\r?\n/).slice(-3).join(' / ') : `exit ${code ?? 'null'} 且没有输出`
          finish({ ok: false, error: detail })
        }
      })
    })
  }

  async function resolveLauncher() {
    if (launcherPromise === null) {
      launcherPromise = (async () => {
        const failures = []
        for (const candidate of candidateLaunchers()) {
          const result = await probeLauncher(candidate)
          if (result.ok) {
            safeLog(ctx, 'info', `使用 DownloadX 启动方式：${candidate.label}（${result.version}）`)
            return candidate
          }
          failures.push(`${candidate.label}: ${result.error}`)
        }
        const lines = ['找不到可用的 DownloadX 启动方式（Python 或 downloadx 未就绪）。', '', '已尝试：']
        failures.slice(0, 8).forEach((failure, index) => lines.push(`${index + 1}. ${failure}`))
        if (failures.length > 8) lines.push(`…另有 ${failures.length - 8} 个候选未列出`)
        lines.push(
          '',
          '请任选一种修复后重试：',
          'a) 安装依赖：pip install -e D:\\Projects\\scalpel-downloadx（需 Python 3.10+）；',
          'b) 在插件配置里写死解释器：config.python: C:\\Program Files\\Python312\\python.exe；',
          'c) 先手动启动引擎：python -m downloadx serve --port 8720。',
        )
        const error = new Error(lines.join('\n'))
        error.isDownloadxStartupError = true
        throw error
      })()
      launcherPromise.catch(() => {
        launcherPromise = null // 允许下次重试：用户可能刚装好依赖
      })
    }
    return launcherPromise
  }

  function spawnCommand(launcher) {
    const prefix = launcher?.prefix ?? ['-m', 'downloadx']
    const args = [...prefix, 'serve', '--host', '127.0.0.1', '--port', String(config.port)]
    if (config.stateDir) args.push('--state-dir', config.stateDir)
    if (config.downloadDir) args.push('--download-dir', config.downloadDir)
    const file = launcher?.file ?? config.python ?? DEFAULT_PYTHON
    return { file, args, label: launcher?.label ?? file }
  }

  function spawnEngine(launcher) {
    const command = spawnCommand(launcher)
    let logFd = -1
    try {
      logFd = openSync(config.logPath, 'a')
    } catch {
      logFd = -1
    }
    const child = spawn(command.file, command.args, {
      detached: true,
      windowsHide: true,
      stdio: logFd >= 0 ? ['ignore', logFd, logFd] : 'ignore',
      env: process.env,
      // 不设置 cwd：DownloadX 用绝对路径定位状态/下载目录；而 Node 对不存在的 cwd 会报 ENOENT，
      // 会被误判成“解释器不存在”。
    })
    if (logFd >= 0) {
      try {
        closeSync(logFd)
      } catch {
        /* ignore */
      }
    }
    // 启动期的失败信号：可执行文件不存在（ENOENT）或进程提前退出。
    const failure = new Promise((resolve) => {
      child.once('error', (error) => resolve(error))
      child.once('exit', (code, signal) =>
        resolve(
          Object.assign(new Error(`引擎进程启动即退出（code=${code ?? 'null'}，signal=${signal ?? 'none'}）`), {
            exitCode: code ?? null,
          }),
        ),
      )
    })
    child.unref()
    return { child, failure, command }
  }

  async function startFailure(error, launcher) {
    let tail = ''
    try {
      tail = (await readLogTail(config.logPath)).trim()
    } catch {
      tail = ''
    }
    const command = spawnCommand(launcher)
    const lines = [
      `无法启动 DownloadX 引擎：${error?.message ?? String(error)}`,
      '',
      `启动方式：${command.label}`,
      `尝试的命令：${command.file} ${command.args.join(' ')}`,
      `日志文件：${config.logPath}`,
      `状态目录：${stateDir()}`,
    ]
    if (tail) lines.push('', '引擎日志（最后若干行）：', tail)
    lines.push(
      '',
      '排查建议：',
      '1) 确认 Python 3.10+ 可用：`python --version`；',
      '2) 确认 downloadx 已安装：`python -m downloadx --version`（在 D:\\Projects\\scalpel-downloadx 里 `pip install -e .`）；',
      `3) 也可以先在终端手动运行：python -m downloadx serve --port ${config.port}，再重试工具调用。`,
    )
    const wrapped = new Error(lines.join('\n'))
    wrapped.isDownloadxStartupError = true
    return wrapped
  }

  async function startService() {
    const before = await probe(3000)
    if (before.state === 'ready') return before.data
    if (before.state === 'foreign') {
      const error = new Error(
        [
          `端口 ${config.port} 上有进程在应答，但它不是 DownloadX（/api/health 响应不匹配）。`,
          '可能是别的程序占用了端口；请换一个端口（插件配置 baseUrl），或先结束占用进程。',
        ].join('\n'),
      )
      error.isDownloadxStartupError = true
      throw error
    }
    // 状态目录里可能已有实例（本次 DSH 只启动一个）：锁新鲜就先等它就绪，锁陈旧就清理。
    const lock = await readLock()
    if (lock) {
      const cleaned = await cleanupStaleLock(lock)
      if (!cleaned) {
        const waitDeadline = Date.now() + Math.min(5000, config.startTimeoutMs)
        while (Date.now() < waitDeadline) {
          const data = await health(1200)
          if (data) return data
          await sleep(300)
        }
        const error = new Error(
          [
            `状态目录 ${stateDir()} 里已有 DownloadX 实例（pid ${lock.pid}，锁心跳 ${Math.round(lock.ageSeconds)}s 前），`,
            `但 ${config.baseUrl} 的 /api/health 访问不到。`,
            '它可能绑定在别的端口：把插件 baseUrl 指向那个端口，或先结束该实例再重试。',
            '插件只会结束自己启动的实例，不会误杀你手动启动的引擎。',
          ].join('\n'),
        )
        error.isDownloadxStartupError = true
        throw error
      }
    }
    const launcher = await resolveLauncher()
    const { child, failure, command } = spawnEngine(launcher)
    spawnedPid = child.pid ?? null
    safeLog(ctx, 'info', `启动引擎：${command.file} ${command.args.join(' ')}（日志：${config.logPath}）`)
    const deadline = Date.now() + config.startTimeoutMs
    while (Date.now() < deadline) {
      const exited = await Promise.race([failure, sleep(HEALTH_POLL_MS).then(() => null)])
      if (exited) throw await startFailure(exited, launcher)
      const data = await health(HEALTH_TIMEOUT_MS)
      if (data) {
        safeLog(ctx, 'info', `引擎就绪：v${data.version ?? '?'} @ ${config.baseUrl}`)
        return data
      }
    }
    throw await startFailure(new Error(`启动超时（${config.startTimeoutMs}ms 内 /api/health 未就绪）`), launcher)
  }

  /** 保证引擎可用：已运行直接复用；否则按配置自动拉起（并发调用共享同一次启动）。 */
  async function ensureReady() {
    const data = await health()
    if (data) return data
    if (!config.autoStart) {
      const error = new Error(
        [
          `DownloadX 引擎未运行（${config.baseUrl}），且插件配置 autoStart=false。`,
          `请先启动引擎：python -m downloadx serve --port ${config.port}`,
        ].join('\n'),
      )
      error.isDownloadxStartupError = true
      throw error
    }
    if (startPromise === null) {
      startPromise = startService().finally(() => {
        startPromise = null
      })
    }
    return startPromise
  }

  function describeError(error) {
    if (error?.isDownloadxStartupError) return error
    if (error?.name === 'TimeoutError' || error?.name === 'AbortError') {
      return new Error(`DownloadX 引擎请求超时（${config.baseUrl}）。任务可能仍在后台继续，稍后用 downloadx_get 查看。`)
    }
    if (error?.httpStatus === 401) {
      return new Error('DownloadX 引擎要求访问令牌：请在插件配置里填 token，或去掉引擎配置中的 api_token。')
    }
    if (error?.httpStatus === 404) {
      const detail = error.detail ? `（${error.detail}）` : ''
      return new Error(`DownloadX 找不到对应任务${detail}。先用 downloadx_list 取得完整任务 ID。`)
    }
    if (error?.httpStatus === 400) {
      return new Error(`DownloadX 拒绝了请求：${error.detail || error.message}`)
    }
    if (error?.httpStatus !== undefined) {
      return new Error(`DownloadX 返回 HTTP ${error.httpStatus}：${error.detail || error.message}`)
    }
    return new Error(`连不上 DownloadX 引擎（${config.baseUrl}）：${error?.message ?? String(error)}`)
  }

  async function rawCall(tool, args, options) {
    const data = await fetchJson(config.baseUrl, '/agent/tool', {
      method: 'POST',
      body: { name: tool, arguments: args ?? {} },
      timeoutMs: options?.timeoutMs ?? config.requestTimeoutMs,
      signal: options?.signal,
      token: config.token,
    })
    if (!isPlainObject(data) || data.ok !== true) {
      throw new Error(`DownloadX 工具 ${tool} 返回了异常结果：${JSON.stringify(data)?.slice(0, 400)}`)
    }
    return data.result
  }

  /** 调用一个 DownloadX Agent 工具；引擎中途消失时自动恢复一次。 */
  async function callTool(tool, args, options = {}) {
    await ensureReady()
    try {
      return await rawCall(tool, args, options)
    } catch (error) {
      const retriable =
        error?.httpStatus === undefined &&
        error?.name !== 'TimeoutError' &&
        error?.name !== 'AbortError'
      if (!retriable || options.signal?.aborted) throw describeError(error)
      const probeResult = await probe(1500)
      if (probeResult.state === 'ready' || !config.autoStart) throw describeError(error)
      safeLog(ctx, 'warn', '引擎在调用中途消失，尝试重新拉起后重试一次')
      await ensureReady().catch(() => {})
      try {
        return await rawCall(tool, args, options)
      } catch (retryError) {
        throw describeError(retryError)
      }
    }
  }

  /** 引擎状态快照（不做副作用，不自动拉起）。 */
  async function status() {
    const probed = await probe(2000)
    const lock = await readLock()
    return {
      baseUrl: config.baseUrl,
      port: config.port,
      python: config.python ?? '(自动发现)',
      autoStart: config.autoStart,
      logPath: config.logPath,
      stateDir: stateDir(),
      spawnedPid,
      running: probed.state === 'ready',
      engine: probed.state === 'ready' ? probed.data : null,
      foreignListener: probed.state === 'foreign',
      lock: lock ? { pid: lock.pid, alive: lock.alive, ageSeconds: Math.round(lock.ageSeconds) } : null,
    }
  }

  async function stop() {
    if (spawnedPid === null) {
      const error = new Error(
        [
          '这个引擎进程不是当前插件实例启动的（可能是上次 DSH 会话、终端里手动启动的，或已经运行很久的服务）。',
          '为避免误杀其他进程，插件不主动结束它。',
          `手动停止：在任务管理器结束 python 进程，或找到运行 python -m downloadx serve --port ${config.port} 的终端按 Ctrl+C。`,
        ].join('\n'),
      )
      error.isDownloadxStartupError = true
      throw error
    }
    const pid = spawnedPid
    try {
      process.kill(pid)
    } catch (error) {
      throw new Error(`结束引擎进程 PID ${pid} 失败：${error?.message ?? String(error)}`)
    }
    const deadline = Date.now() + 5000
    while (Date.now() < deadline) {
      if ((await health(1200)) === null) {
        spawnedPid = null
        await removeLockForPid(pid)
        return pid
      }
      await sleep(250)
    }
    throw new Error(`已向 PID ${pid} 发送结束信号，但引擎仍在应答（可能还有其他实例占用端口）。`)
  }

  return { config, ensureReady, health, probe, callTool, status, stop, resolveTaskId, stateDir }
}

// ────────────────────────────────────────────────────────────────────────────
// 文本格式化（模型侧 token 友好）
// ────────────────────────────────────────────────────────────────────────────

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

export function formatBytes(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  let number = value
  let index = 0
  while (number >= 1024 && index < UNITS.length - 1) {
    number /= 1024
    index += 1
  }
  const digits = index === 0 ? 0 : number >= 100 ? 0 : number >= 10 ? 1 : 2
  const text = index === 0 ? String(Math.round(number)) : number.toFixed(digits)
  return `${text} ${UNITS[index]}`
}

export function formatSpeed(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return '0 B/s'
  return `${formatBytes(value)}/s`
}

export function formatEta(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '未知'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${String(Math.round(seconds % 60)).padStart(2, '0')}s`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${hours}h${String(minutes).padStart(2, '0')}m`
}

function clockText(epochSeconds) {
  if (typeof epochSeconds !== 'number' || !Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (value) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function shortId(id) {
  const text = String(id ?? '')
  return text.length > 10 ? text.slice(0, 8) : text
}

function oneLine(value, limit = 160) {
  const text = String(value ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

export function percentText(task) {
  if (!isPlainObject(task)) return null
  if (typeof task.percent === 'number' && Number.isFinite(task.percent)) return `${task.percent.toFixed(1)}%`
  const total = Number(task.total_size)
  const done = Number(task.downloaded_size)
  if (Number.isFinite(total) && total > 0 && Number.isFinite(done) && done >= 0) {
    return `${((done / total) * 100).toFixed(1)}%`
  }
  return null
}

/** 单行任务摘要，用于列表。 */
export function taskLine(task, index) {
  const parts = [`${index + 1}. [${shortId(task?.id)}] ${task?.status ?? '?'}`]
  const percent = percentText(task)
  if (percent !== null && task?.status !== 'completed') parts.push(percent)
  const total = typeof task?.total_size === 'number' && task.total_size > 0 ? task.total_size : null
  const done = typeof task?.downloaded_size === 'number' ? task.downloaded_size : null
  if (total !== null) parts.push(`${formatBytes(done)} / ${formatBytes(total)}`)
  else if (done !== null && done > 0) parts.push(formatBytes(done))
  if (task?.status === 'downloading' && typeof task.speed === 'number' && task.speed > 0) {
    parts.push(formatSpeed(task.speed))
    if (task.eta !== null && task.eta !== undefined) parts.push(`剩余 ${formatEta(task.eta)}`)
  }
  let line = parts.join(' · ')
  if (task?.filename) line += ` · ${oneLine(task.filename, 80)}`
  if (task?.error) line += ` · 错误：${oneLine(task.error, 120)}`
  return line
}

/** 多行任务详情，用于 get。 */
export function taskDetail(task) {
  const lines = [`任务：${task?.id ?? '?'}`, `状态：${task?.status ?? '?'}${task?.mode ? ` · 模式 ${task.mode}` : ''}`]
  if (task?.filename) lines.push(`文件：${task.filename}`)
  const percent = percentText(task)
  const sizeText =
    typeof task?.total_size === 'number' && task.total_size > 0
      ? `${formatBytes(task.downloaded_size)} / ${formatBytes(task.total_size)}`
      : formatBytes(task?.downloaded_size)
  lines.push(`进度：${percent ?? '—'}（${sizeText}）`)
  if (task?.status === 'downloading') {
    lines.push(`速度：${formatSpeed(task.speed)}${task.eta !== null && task.eta !== undefined ? ` · 剩余 ${formatEta(task.eta)}` : ''}`)
  }
  if (typeof task?.connections === 'number') {
    const effective = typeof task.effective_connections === 'number' ? task.effective_connections : task.connections
    lines.push(`连接：${effective}${effective !== task.connections ? `（配置 ${task.connections}）` : ''}${typeof task.priority === 'number' ? ` · 优先级 ${task.priority}` : ''}`)
  }
  if (task?.resumable !== undefined) lines.push(`可续传：${task.resumable ? '是' : '否'}${task?.plan_reason ? `（${oneLine(task.plan_reason, 120)}）` : ''}`)
  if (task?.save_path) lines.push(`保存到：${task.save_path}`)
  if (task?.sha256) lines.push(`SHA-256：${task.sha256}${task.verified === true ? ' · 校验通过' : task.verified === false ? ' · 校验失败' : ''}`)
  if (task?.created_at) lines.push(`创建：${clockText(task.created_at)}${task?.completed_at ? ` · 完成：${clockText(task.completed_at)}` : ''}`)
  if (typeof task?.elapsed === 'number' && task.elapsed > 0) lines.push(`耗时：${formatEta(task.elapsed)}`)
  if (task?.error) lines.push(`错误：${oneLine(task.error, 300)}${task?.error_code ? `（${task.error_code}）` : ''}`)
  return lines.join('\n')
}

function statsText(stats) {
  if (!isPlainObject(stats)) return '（没有拿到统计信息）'
  const lines = [`DownloadX v${stats.version ?? '?'} 全局统计`, `任务：总数 ${stats.total_tasks ?? 0} · 下载中 ${stats.active_tasks ?? 0} · 排队 ${stats.queued_tasks ?? 0} · 暂停 ${stats.paused_tasks ?? 0} · 完成 ${stats.completed_tasks ?? 0} · 失败 ${stats.failed_tasks ?? 0} · 取消 ${stats.canceled_tasks ?? 0}`]
  const queue = isPlainObject(stats.queue) ? stats.queue : {}
  const connections = isPlainObject(stats.connections) ? stats.connections : {}
  const throttled = queue.globally_paused === true ? '（队列已全局暂停）' : ''
  lines.push(`速度：${formatSpeed(stats.download_speed)}${throttled} · 连接 ${connections.in_use ?? 0}/${connections.limit ?? 0} · 并发 ${queue.active ?? 0}/${queue.max_concurrent ?? 0}`)
  if (typeof stats.total_downloaded_bytes === 'number') {
    lines.push(`累计已下载：${formatBytes(stats.total_downloaded_bytes)}${typeof stats.total_remaining_bytes === 'number' && stats.total_remaining_bytes > 0 ? ` · 待下载 ${formatBytes(stats.total_remaining_bytes)}` : ''}`)
  }
  return lines.join('\n')
}

function historyLine(record, index) {
  const parts = [`${index + 1}. [${shortId(record?.id)}] ${record?.status ?? '?'}`, formatBytes(record?.total_size)]
  if (typeof record?.elapsed === 'number' && record.elapsed > 0) parts.push(formatEta(record.elapsed))
  let line = parts.join(' · ')
  if (record?.filename) line += ` · ${oneLine(record.filename, 80)}`
  if (record?.error) line += ` · 错误：${oneLine(record.error, 120)}`
  return line
}

// ────────────────────────────────────────────────────────────────────────────
// 工具定义
// ────────────────────────────────────────────────────────────────────────────

/** 构造一个注册表可用的 ToolDefinition（不依赖 defineTool，避免导入官方包）。 */
function makeTool({ toolName, description, parameters, execute }) {
  return {
    name: toolName,
    description,
    parameters,
    output: {
      schema: OUTPUT_SCHEMA,
      render: (_args, value) => [{ type: 'text', text: typeof value?.text === 'string' ? value.text : '' }],
    },
    async execute(args, exec) {
      const text = await execute(args ?? {}, exec)
      return { ok: true, text: String(text) }
    },
  }
}

const TASK_ID_PARAMETER = {
  type: 'string',
  description: '任务 ID：downloadx_add 返回的 32 位十六进制完整 ID，或它的唯一前缀（插件会解析前缀）',
}

const CONTROL_ACTIONS = [
  'pause',
  'resume',
  'cancel',
  'retry',
  'remove',
  'set_priority',
  'set_speed_limit',
  'pause_all',
  'resume_all',
]

function controlToolName(action) {
  const map = {
    pause: 'pause_download',
    resume: 'resume_download',
    cancel: 'cancel_download',
    retry: 'retry_download',
    remove: 'remove_download',
    set_priority: 'set_priority',
    set_speed_limit: 'set_speed_limit',
    pause_all: 'pause_all',
    resume_all: 'resume_all',
  }
  return map[action]
}

const CONTROL_VERBS = {
  pause: '已暂停',
  resume: '已继续',
  cancel: '已取消',
  retry: '已重试',
  remove: '已移除',
  set_priority: '已调整优先级',
  set_speed_limit: '已设置限速',
  pause_all: '已暂停全部任务',
  resume_all: '已继续全部任务',
}

function buildTools(engine) {
  const config = engine.config
  return [
    makeTool({
      toolName: 'downloadx_add',
      description:
        '用本机 DownloadX 下载引擎新建一个 HTTP/HTTPS（含 HLS/DASH 流媒体）下载任务：大文件、需要断点续传/完整性校验/限速/后台继续下载时优先使用。加入队列后立即返回，下载在引擎后台继续，不阻塞对话。不要用它抓普通小页面/API（用 web_fetch），也不要下载 YouTube/Bilibili/X 等视频网站页面——引擎不路由 yt-dlp，会把页面当网页保存；视频请用 DownloadX 的 fetch_video.py 技能。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          url: { type: 'string', description: 'http/https 下载链接（也支持 .m3u8/.mpd 流媒体）' },
          filename: { type: 'string', description: '另存文件名；缺省用 URL 里的文件名' },
          save_dir: { type: 'string', description: '保存目录的绝对路径；缺省用引擎默认下载目录' },
          priority: {
            type: 'integer',
            description: '优先级，数字越小越先下载，默认 5',
          },
          connections: {
            type: 'integer',
            description: '分片连接数 1-32；缺省由引擎自适应。对小型文件或不友好的站点建议 1',
          },
          speed_limit: {
            type: 'integer',
            description: '单任务限速，单位 bytes/s；0 表示不限速',
          },
          sha256: {
            type: 'string',
            description: '期望的 SHA-256（十六进制）。提供后引擎在完成后校验，校验通过才原子改名成最终文件',
          },
          start: { type: 'boolean', description: '是否立即开始，默认 true；false 表示只入队不启动' },
        },
        required: ['url'],
      },
      execute: async (args) => {
        if (!isPlainObject(args) || typeof args.url !== 'string' || !args.url.trim()) {
          throw new Error('downloadx_add 需要非空的 url 参数')
        }
        if (args.connections !== undefined && (!Number.isInteger(args.connections) || args.connections < 1 || args.connections > 32)) {
          throw new Error('connections 必须是 1-32 的整数')
        }
        if (args.priority !== undefined && (!Number.isInteger(args.priority) || args.priority < 0 || args.priority > 99)) {
          throw new Error('priority 必须是 0-99 的整数')
        }
        if (args.speed_limit !== undefined && (!Number.isInteger(args.speed_limit) || args.speed_limit < 0)) {
          throw new Error('speed_limit 必须是 >= 0 的整数（bytes/s）')
        }
        if (typeof args.save_dir === 'string' && args.save_dir.trim() && !isAbsolute(args.save_dir.trim())) {
          throw new Error('save_dir 必须是绝对路径（例如 D:\\\\Downloads）；相对路径请改用当前工作目录拼好再传')
        }
        const payload = { url: args.url.trim() }
        for (const key of ['filename', 'save_dir', 'sha256']) {
          if (typeof args[key] === 'string' && args[key].trim()) payload[key] = args[key].trim()
        }
        for (const key of ['priority', 'connections', 'speed_limit']) {
          if (args[key] !== undefined) payload[key] = args[key]
        }
        if (typeof args.start === 'boolean') payload.start = args.start
        const task = await engine.callTool('add_download', payload, { timeoutMs: 30000 })
        const lines = ['已加入下载队列。', `任务 ID：${task?.id ?? '?'}`]
        if (task?.filename) lines.push(`文件名：${task.filename}`)
        lines.push(`状态：${task?.status ?? 'queued'}（引擎后台继续下载）`)
        if (task?.save_path) lines.push(`保存到：${task.save_path}`)
        if (task?.total_size) lines.push(`大小：${formatBytes(task.total_size)}`)
        lines.push('', '用 downloadx_get 查看进度，用 downloadx_control 暂停/取消，用 downloadx_list 查看队列。')
        return lines.join('\n')
      },
    }),

    makeTool({
      toolName: 'downloadx_list',
      description: '列出 DownloadX 的下载任务（可按状态过滤），返回任务 ID、状态、进度、速度、文件名。用于回答“下载到哪了/有哪些任务”。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          status: { type: 'string', enum: TASK_STATES, description: '只看某个状态的任务' },
          order: { type: 'string', enum: LIST_ORDERS, description: '排序：created（默认）、priority（优先级）、updated（最近更新）' },
          limit: { type: 'integer', description: `最多返回多少条，默认 ${DEFAULT_LIST_LIMIT}，上限 ${MAX_LIST_LIMIT}` },
        },
        required: [],
      },
      execute: async (args) => {
        const limit = args.limit === undefined ? DEFAULT_LIST_LIMIT : Number(args.limit)
        if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIST_LIMIT) {
          throw new Error(`limit 必须是 1-${MAX_LIST_LIMIT} 的整数`)
        }
        const payload = { limit }
        if (typeof args.status === 'string' && args.status) payload.status = args.status
        if (typeof args.order === 'string' && args.order) payload.order = args.order
        const result = await engine.callTool('list_downloads', payload)
        const tasks = Array.isArray(result?.tasks) ? result.tasks : []
        const count = typeof result?.count === 'number' ? result.count : tasks.length
        if (tasks.length === 0) {
          return `没有匹配的下载任务${payload.status ? `（状态：${payload.status}）` : ''}。`
        }
        const lines = [`共 ${count} 个任务${payload.status ? `（状态：${payload.status}）` : ''}，显示 ${tasks.length} 条：`]
        tasks.forEach((task, index) => lines.push(taskLine(task, index)))
        if (count > tasks.length) lines.push(`（还有 ${count - tasks.length} 条未显示，可加大 limit）`)
        lines.push('', '提示：downloadx_get 查看单个任务详情；downloadx_control 控制任务。')
        return lines.join('\n')
      },
    }),

    makeTool({
      toolName: 'downloadx_get',
      description: '查看单个 DownloadX 下载任务的完整详情：进度、速度、连接数、保存路径、SHA-256 校验结果、错误信息。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { task_id: { ...TASK_ID_PARAMETER, description: `任务 ID（完整 ID 或唯一前缀）` } },
        required: ['task_id'],
      },
      execute: async (args) => {
        if (typeof args.task_id !== 'string' || !args.task_id.trim()) {
          throw new Error('downloadx_get 需要 task_id')
        }
        const task = await engine.callTool('get_download', { task_id: await engine.resolveTaskId(args.task_id) })
        return taskDetail(task)
      },
    }),

    makeTool({
      toolName: 'downloadx_control',
      description:
        '控制 DownloadX 下载任务：暂停/继续/取消/重试/移除、调整优先级或单任务限速，以及全部暂停/全部继续。下载失败或被中断后应优先用 retry（从已下载字节续传），不要重新 add。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: { type: 'string', enum: CONTROL_ACTIONS, description: '要执行的动作' },
          task_id: TASK_ID_PARAMETER,
          reset: { type: 'boolean', description: '仅 retry：true 表示丢弃已下载进度、从零重下（默认 false 续传）' },
          delete_file: { type: 'boolean', description: '仅 remove：true 表示连已下载文件一起删除' },
          priority: { type: 'integer', description: '仅 set_priority：新的优先级，0 最高' },
          speed_limit: { type: 'integer', description: '仅 set_speed_limit：新的单任务限速 bytes/s，0 取消限速' },
        },
        required: ['action'],
      },
      execute: async (args) => {
        const action = args.action
        if (!CONTROL_ACTIONS.includes(action)) {
          throw new Error(`action 必须是 ${CONTROL_ACTIONS.join('/')} 之一`)
        }
        const needsTask = !action.endsWith('_all')
        if (needsTask && (typeof args.task_id !== 'string' || !args.task_id.trim())) {
          throw new Error(`${action} 需要 task_id（先用 downloadx_list 取得）`)
        }
        const payload = {}
        if (needsTask) payload.task_id = await engine.resolveTaskId(args.task_id)
        const tool = controlToolName(action)
        if (action === 'retry' && typeof args.reset === 'boolean') payload.reset = args.reset
        if (action === 'remove' && typeof args.delete_file === 'boolean') payload.delete_file = args.delete_file
        if (action === 'set_priority') {
          if (!Number.isInteger(args.priority)) throw new Error('set_priority 需要整数 priority')
          payload.priority = args.priority
        }
        if (action === 'set_speed_limit') {
          if (!Number.isInteger(args.speed_limit) || args.speed_limit < 0) {
            throw new Error('set_speed_limit 需要 >=0 的整数 speed_limit（bytes/s）')
          }
          payload.speed_limit = args.speed_limit
        }
        const result = await engine.callTool(tool, payload)
        const verb = CONTROL_VERBS[action] ?? `已完成 ${action}`
        if (action === 'pause_all' || action === 'resume_all') {
          return `${verb}（${result?.paused ?? result?.resumed ?? 0} 个任务）。`
        }
        if (action === 'set_priority') return `${verb}：任务 ${shortId(payload.task_id)} → 优先级 ${result?.priority ?? payload.priority}。`
        if (action === 'set_speed_limit') {
          const limit = result?.speed_limit ?? payload.speed_limit
          return `${verb}：任务 ${shortId(payload.task_id)} → ${limit === 0 ? '不限速' : formatSpeed(limit)}。`
        }
        if (action === 'remove') return `${verb}：任务 ${shortId(payload.task_id)}${result?.deleted_file ? '（文件已一并删除）' : ''}。`
        const status = result?.status ? `，当前状态 ${result.status}` : ''
        return `${verb}：任务 ${shortId(payload.task_id)}${status}。`
      },
    }),

    makeTool({
      toolName: 'downloadx_probe',
      description:
        '只探测一个 URL 的资源信息（大小、是否支持断点续传、文件名、内容类型、HTTP 状态），不下载任何数据。下载前先探测可以避免下到一半才发现不支持续传或文件过大。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: { url: { type: 'string', description: '要探测的 http/https 链接' } },
        required: ['url'],
      },
      execute: async (args, exec) => {
        if (typeof args.url !== 'string' || !args.url.trim()) throw new Error('downloadx_probe 需要 url')
        const info = await engine.callTool(
          'probe_url',
          { url: args.url.trim() },
          { timeoutMs: PROBE_TIMEOUT_MS, signal: exec?.signal },
        )
        const lines = [`资源探测：${info?.final_url ?? args.url.trim()}`]
        lines.push(`大小：${info?.size === null || info?.size === undefined ? '未知（服务端未给 Content-Length）' : formatBytes(info.size)}`)
        lines.push(`可断点续传：${info?.resumable ? '是' : '否'}`)
        if (info?.filename) lines.push(`文件名：${info.filename}`)
        if (info?.content_type) lines.push(`类型：${info.content_type}`)
        if (info?.status !== undefined) lines.push(`HTTP 状态：${info.status}`)
        return lines.join('\n')
      },
    }),

    makeTool({
      toolName: 'downloadx_engine',
      description:
        '检查、启动或停止本机 DownloadX 引擎服务，查看全局统计与下载历史。日常下载通常不需要手动调用：downloadx_add 等工具会自动检查并拉起引擎。',
      parameters: {
        type: 'object',
        additionalProperties: false,
        properties: {
          action: {
            type: 'string',
            enum: ['status', 'start', 'stop', 'stats', 'history'],
            description: 'status（默认）/start/stop/stats/history',
          },
          limit: { type: 'integer', description: '仅 history：返回多少条历史记录，默认 10' },
        },
        required: [],
      },
      execute: async (args) => {
        const action = args.action ?? 'status'
        if (action === 'status') {
          const snapshot = await engine.status()
          const lines = [`DownloadX 插件 dsh-downloadx v${PLUGIN_VERSION}`, `引擎地址：${snapshot.baseUrl}`]
          if (snapshot.running) {
            lines.push(`引擎状态：运行中（v${snapshot.engine?.version ?? '?'}，uptime ${formatEta(Number(snapshot.engine?.uptime ?? 0))}）`)
            if (snapshot.engine?.download_dir) lines.push(`默认下载目录：${snapshot.engine.download_dir}`)
            if (snapshot.engine?.state_dir) lines.push(`状态目录：${snapshot.engine.state_dir}`)
          } else {
            lines.push(`引擎状态：未运行${snapshot.foreignListener ? '（端口被其它程序占用）' : ''}`)
          }
          lines.push(`自动启动：${snapshot.autoStart ? '开' : '关'} · Python：${snapshot.python}`)
          lines.push(`引擎日志：${snapshot.logPath}`)
          lines.push(`状态目录：${snapshot.stateDir}`)
          if (snapshot.lock) lines.push(`状态锁：pid ${snapshot.lock.pid}（${snapshot.lock.alive ? '存活' : '已退出'}，心跳 ${snapshot.lock.ageSeconds}s 前）`)
          if (snapshot.spawnedPid) lines.push(`本实例启动的引擎 PID：${snapshot.spawnedPid}`)
          if (!snapshot.running) lines.push('', '用 downloadx_engine(action="start") 或直接调用 downloadx_add 自动拉起引擎。')
          return lines.join('\n')
        }
        if (action === 'start') {
          const data = await engine.ensureReady()
          return `DownloadX 引擎已就绪：v${data?.version ?? '?'} @ ${engine.config.baseUrl}${data?.download_dir ? `\n默认下载目录：${data.download_dir}` : ''}`
        }
        if (action === 'stop') {
          const pid = await engine.stop()
          return `已结束本插件启动的 DownloadX 引擎（PID ${pid}）。`
        }
        if (action === 'stats') {
          const stats = await engine.callTool('get_stats', {})
          return statsText(stats)
        }
        if (action === 'history') {
          const limit = args.limit === undefined ? 10 : Number(args.limit)
          if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIST_LIMIT) {
            throw new Error(`limit 必须是 1-${MAX_LIST_LIMIT} 的整数`)
          }
          const result = await engine.callTool('get_history', { limit })
          const records = Array.isArray(result?.records) ? result.records : []
          if (records.length === 0) return '没有下载历史记录。'
          const lines = [`最近 ${records.length} 条下载历史：`]
          records.forEach((record, index) => lines.push(historyLine(record, index)))
          return lines.join('\n')
        }
        throw new Error(`未知 action：${action}（可用 status/start/stop/stats/history）`)
      },
    }),
  ]
}

// ────────────────────────────────────────────────────────────────────────────
// 插件入口
// ────────────────────────────────────────────────────────────────────────────

/**
 * 可选的 HTTP 路由（只有带 webServer 的 profile 才注册）：
 * - `GET  /api/downloadx/health`：插件 + 引擎状态（只读，无副作用）
 * - `GET  /api/downloadx/info`  ：给侧边栏面板用，返回引擎地址与是否在运行
 * - `POST /api/downloadx/open`  ：给侧边栏面板用，确保引擎已启动并返回 UI 地址
 * 这些路由不额外做鉴权：Web profile 的 /api/* 由 DSH 自身的登录态拦截，路由只暴露本机回环信息。
 */
function registerDiagnosticsRoute(ctx, engine, toolNames) {
  const webServer = ctx.webServer
  if (!webServer || typeof webServer.register !== 'function') return
  const sendJson = (res, status, payload) => {
    const body = JSON.stringify(payload)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': Buffer.byteLength(body),
      'cache-control': 'no-store',
    })
    res.end(body)
  }
  const register = (path, methods, handler) => {
    webServer.register({
      kind: 'exact',
      path,
      handler: async (req, res) => {
        if (req.method === 'OPTIONS') {
          res.writeHead(204, {
            'access-control-allow-origin': '*',
            'access-control-allow-methods': `${methods.join(', ')}, OPTIONS`,
            'access-control-allow-headers': 'content-type',
          })
          res.end()
          return
        }
        if (!methods.includes(req.method)) {
          sendJson(res, 405, { error: `method ${req.method} not allowed` })
          return
        }
        try {
          await handler(req, res)
        } catch (error) {
          safeLog(ctx, 'warn', `路由 ${path} 失败：${error?.message ?? String(error)}`)
          sendJson(res, 500, { error: error?.message ?? String(error) })
        }
      },
    })
  }
  const engineSnapshot = async () => {
    const snapshot = await engine.status()
    return {
      baseUrl: snapshot.baseUrl,
      running: snapshot.running,
      foreignListener: snapshot.foreignListener,
      version: snapshot.engine?.version ?? null,
      downloadDir: snapshot.engine?.download_dir ?? null,
      stateDir: snapshot.engine?.state_dir ?? null,
      pid: snapshot.spawnedPid,
    }
  }
  const uiUrl = () => `${engine.config.baseUrl}/`
  try {
    register('/api/downloadx/health', ['GET'], async (_req, res) => {
      sendJson(res, 200, {
        plugin: 'dsh-downloadx',
        version: PLUGIN_VERSION,
        tools: toolNames,
        engine: await engineSnapshot(),
      })
    })
    register('/api/downloadx/info', ['GET'], async (_req, res) => {
      sendJson(res, 200, {
        plugin: 'dsh-downloadx',
        version: PLUGIN_VERSION,
        tools: toolNames,
        engine: await engineSnapshot(),
        uiUrl: uiUrl(),
      })
    })
    register('/api/downloadx/open', ['POST'], async (_req, res) => {
      const health = await engine.ensureReady()
      sendJson(res, 200, {
        ok: true,
        uiUrl: uiUrl(),
        version: health?.version ?? null,
        engine: await engineSnapshot(),
      })
    })
  } catch (error) {
    safeLog(ctx, 'warn', `诊断路由注册失败：${error?.message ?? String(error)}`)
  }
}

export function apply(ctx, rawConfig) {
  const config = normalizeConfig(rawConfig)
  const engine = createEngine(config, ctx)
  const tools = buildTools(engine)
  for (const tool of tools) ctx.tools.register(tool)
  attachDiagnosticsRoute(
    ctx,
    engine,
    tools.map((tool) => tool.name),
  )
  safeLog(
    ctx,
    'info',
    `已注册 ${tools.length} 个下载工具（引擎 ${config.baseUrl}，autoStart=${config.autoStart}，python=${config.python ?? '自动发现'}）`,
  )
}

/**
 * 诊断路由的挂载。webServer 是可选服务：插件主 inject 只声明 tools（headless 也能加载），
 * 这里用 ctx.inject 起一个等待 webServer 的子插件，服务出现时才注册路由。
 */
function attachDiagnosticsRoute(ctx, engine, toolNames) {
  const mount = (scopedCtx) => {
    try {
      registerDiagnosticsRoute(scopedCtx, engine, toolNames)
    } catch (error) {
      safeLog(ctx, 'warn', `诊断路由注册失败：${error?.message ?? String(error)}`)
    }
  }
  try {
    if (typeof ctx.inject === 'function') {
      ctx.inject(['webServer'], mount)
      return
    }
  } catch (error) {
    safeLog(ctx, 'warn', `等待 webServer 失败：${error?.message ?? String(error)}`)
  }
  mount(ctx)
}

/** 测试/诊断用内部导出，不属于插件契约。 */
export const __internal = {
  PLUGIN_VERSION,
  percentText,
  taskLine,
  taskDetail,
  statsText,
  historyLine,
  shortId,
  oneLine,
}
