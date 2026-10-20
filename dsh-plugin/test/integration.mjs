/**
 * 集成测试：用真实的 Python DownloadX 引擎跑插件产线代码（tools + ServiceManager）。
 * 不需要 DSH：用最小的 fake ctx 捕获工具注册，然后直接调用工具 execute。
 * 覆盖：自动拉起引擎、add/get/list/probe/stats/history/control、错误路径、stop 结束进程。
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { createHash, randomBytes } from 'node:crypto'
import { mkdir, readFile, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply } from '../lib/index.js'

const ROOT = join(tmpdir(), 'dsh-downloadx-it')
const STATE = join(ROOT, 'state')
const DL = join(ROOT, 'dl')
// 端口可用环境变量覆盖，避免与本地其它测试冲突。
const ENGINE_PORT = Number(process.env.DOWNLOADX_TEST_ENGINE_PORT ?? 8734)
const FILE_PORT = Number(process.env.DOWNLOADX_TEST_FILE_PORT ?? 8735)

const payload = randomBytes(3 * 1024 * 1024)
const payloadHash = createHash('sha256').update(payload).digest('hex')

function startFileServer() {
  const server = createServer((req, res) => {
    if (req.url === '/payload.bin') {
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': payload.length })
      res.end(payload)
      return
    }
    if (req.url === '/slow.bin') {
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': payload.length })
      let offset = 0
      const chunk = 64 * 1024
      const timer = setInterval(() => {
        if (offset >= payload.length) {
          clearInterval(timer)
          res.end()
          return
        }
        res.write(payload.subarray(offset, offset + chunk))
        offset += chunk
      }, 120)
      req.on('close', () => clearInterval(timer))
      return
    }
    res.writeHead(404)
    res.end()
  })
  return new Promise((resolve) => server.listen(FILE_PORT, '127.0.0.1', () => resolve(server)))
}

function makeCtx() {
  const registry = new Map()
  const ctx = {
    tools: {
      register(def) {
        registry.set(def.name, def)
      },
    },
    logger: {
      info: (message) => console.error('[plugin:info]', message),
      warn: (message) => console.error('[plugin:warn]', message),
      error: (message) => console.error('[plugin:error]', message),
    },
    on: () => {},
  }
  return { ctx, registry }
}

async function waitFor(check, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const value = await check()
    if (value) return value
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`timeout waiting for ${label}`)
}

async function main() {
  await rm(ROOT, { recursive: true, force: true })
  await mkdir(STATE, { recursive: true })
  await mkdir(DL, { recursive: true })
  const server = await startFileServer()
  try {
  const { ctx, registry } = makeCtx()
  apply(ctx, {
    baseUrl: `http://127.0.0.1:${ENGINE_PORT}`,
    autoStart: true,
    python: 'python',
    stateDir: STATE,
    downloadDir: DL,
    startTimeoutMs: 30000,
  })
  assert.equal(registry.size, 6)
  const tool = (name) => registry.get(name)
  const engineInfo = () => {
    const def = tool('downloadx_engine')
    return def.execute({ action: 'status' }, {})
  }

  // 1) 引擎未运行 → status 不产生副作用
    const before = await engineInfo()
    assert.match(before.text, /未运行/)
    console.log('✓ status 未运行')

    // 2) probe（会触发自动拉起引擎）
    const probe = await tool('downloadx_probe').execute({ url: `http://127.0.0.1:${FILE_PORT}/payload.bin` }, {})
    assert.match(probe.text, /资源探测/)
    assert.match(probe.text, /3\.00 MB/)
    assert.match(probe.text, /可断点续传：否/)
    console.log('✓ probe + 自动拉起引擎')

    // 3) add（带 sha256）→ 完成 → 文件落盘且校验通过
    const added = await tool('downloadx_add').execute(
      { url: `http://127.0.0.1:${FILE_PORT}/payload.bin`, sha256: payloadHash, connections: 2 },
      {},
    )
    assert.match(added.text, /已加入下载队列/)
    const taskId = added.text.match(/任务 ID：([0-9a-f]{32})/)[1]
    const final = await waitFor(async () => {
      const result = await tool('downloadx_get').execute({ task_id: taskId }, {})
      return result.text.includes('状态：completed') ? result : null
    }, 60000, 'download completion')
    assert.match(final.text, /SHA-256：.*校验通过/)
    const outputPath = join(DL, 'payload.bin')
    assert.ok(existsSync(outputPath), 'downloaded file exists')
    assert.equal(createHash('sha256').update(await readFile(outputPath)).digest('hex'), payloadHash)
    console.log('✓ add → completed → 校验通过、原子落盘')

    // 4) list
    const list = await tool('downloadx_list').execute({ status: 'completed' }, {})
    assert.match(list.text, /共 1 个任务/)
    assert.match(list.text, new RegExp(taskId.slice(0, 8)))
    console.log('✓ list')

    // 5) 慢速下载 → 暂停 / 继续 / 取消
    const slow = await tool('downloadx_add').execute({ url: `http://127.0.0.1:${FILE_PORT}/slow.bin`, filename: 'slow.bin' }, {})
    const slowId = slow.text.match(/任务 ID：([0-9a-f]{32})/)[1]
    await waitFor(async () => {
      const current = await tool('downloadx_get').execute({ task_id: slowId }, {})
      return /状态：downloading/.test(current.text) ? current : null
    }, 20000, 'slow download starts')
    const paused = await tool('downloadx_control').execute({ action: 'pause', task_id: slowId }, {})
    assert.match(paused.text, /已暂停/)
    const resumed = await tool('downloadx_control').execute({ action: 'resume', task_id: slowId }, {})
    assert.match(resumed.text, /已继续/)
    const canceled = await tool('downloadx_control').execute({ action: 'cancel', task_id: slowId }, {})
    assert.match(canceled.text, /已取消/)
    await tool('downloadx_control').execute({ action: 'remove', task_id: slowId }, {})
    console.log('✓ control pause/resume/cancel/remove')

    // 6) 引擎级操作：stats / history / 错误路径
    const stats = await tool('downloadx_engine').execute({ action: 'stats' }, {})
    assert.match(stats.text, /全局统计/)
    assert.match(stats.text, /完成 1/)
    const history = await tool('downloadx_engine').execute({ action: 'history', limit: 5 }, {})
    assert.match(history.text, /下载历史/)
    await assert.rejects(() => tool('downloadx_get').execute({ task_id: 'deadbeefdeadbeefdeadbeefdeadbeef' }, {}), /找不到对应任务/)
    await assert.rejects(
      () => tool('downloadx_add').execute({ url: 'not-a-url' }, {}),
      /DownloadX 拒绝了请求|连不上|HTTP/,
    )
    console.log('✓ stats / history / 错误路径')

    // 7) stop（本实例拉起的引擎）
    const stopped = await tool('downloadx_engine').execute({ action: 'stop' }, {})
    assert.match(stopped.text, /已结束/)
    await waitFor(async () => {
      const health = await fetch(`http://127.0.0.1:${ENGINE_PORT}/api/health`).then(
        () => false,
        () => true,
      )
      return health
    }, 10000, 'engine stops')
    console.log('✓ stop 结束引擎进程')

    // 8) 模拟桌面宿主：PATH 里没有 python 时，插件靠绝对路径 / pip --user 的 downloadx.exe 发现启动方式
    const savedPath = process.env.PATH
    process.env.PATH = 'C:\\Windows\\system32;C:\\Windows'
    try {
      const { ctx: ctx2, registry: registry2 } = makeCtx()
      apply(ctx2, {
        baseUrl: 'http://127.0.0.1:8736',
        autoStart: true,
        stateDir: join(ROOT, 'state2'),
        downloadDir: join(ROOT, 'dl2'),
        startTimeoutMs: 30000,
      })
      assert.equal(registry2.size, 6)
      await mkdir(join(ROOT, 'state2'), { recursive: true })
      await mkdir(join(ROOT, 'dl2'), { recursive: true })
      const startedDown = await registry2.get('downloadx_engine').execute({ action: 'start' }, {})
      assert.match(startedDown.text, /已就绪/)
      const health = await fetch('http://127.0.0.1:8736/api/health').then((response) => response.json())
      assert.equal(health.app, 'DownloadX')
      await registry2.get('downloadx_engine').execute({ action: 'stop' }, {})
      console.log('✓ 无 PATH 环境下自动发现启动器')
    } finally {
      process.env.PATH = savedPath
    }
  } finally {
    server.close()
  }
  console.log('integration ok')
}

main().catch((error) => {
  console.error('integration failed:', error)
  process.exitCode = 1
})

