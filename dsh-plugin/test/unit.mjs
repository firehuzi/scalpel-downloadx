/**
 * 离线单元测试：不启动 DSH、不需要 DownloadX 引擎。
 * 校验插件注册的工具契约（数量/名称/参数 schema/output/execute）与格式化函数，
 * 并在引擎不可用时验证工具给出的是可执行的中文错误而不是崩溃。
 */
import assert from 'node:assert/strict'
import { apply, normalizeConfig, formatBytes, formatSpeed, formatEta, __internal } from '../lib/index.js'

const ALLOWED_SCHEMA_KEYS = new Set([
  'type',
  'oneOf',
  'properties',
  'required',
  'additionalProperties',
  'items',
  'enum',
  'const',
  'description',
  'title',
  'default',
  'examples',
])

function walkSchema(node, path, issues) {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    issues.push(`${path}: schema node must be an object`)
    return
  }
  for (const key of Object.keys(node)) {
    if (!ALLOWED_SCHEMA_KEYS.has(key)) issues.push(`${path}: unsupported keyword ${key}`)
  }
  if (node.type !== undefined && !['object', 'array', 'string', 'number', 'integer', 'boolean', 'null'].includes(node.type)) {
    issues.push(`${path}: bad type ${node.type}`)
  }
  if (node.type === 'object') {
    if (typeof node.additionalProperties !== 'boolean') issues.push(`${path}: object needs boolean additionalProperties`)
    if (!node.properties || typeof node.properties !== 'object') issues.push(`${path}: object needs properties`)
    for (const [key, child] of Object.entries(node.properties ?? {})) walkSchema(child, `${path}.properties.${key}`, issues)
    for (const key of node.required ?? []) {
      if (!(key in (node.properties ?? {}))) issues.push(`${path}: required ${key} not declared`)
    }
  }
  if (node.type === 'array') walkSchema(node.items, `${path}.items`, issues)
}

const defs = []
const errors = []
const ctx = {
  tools: {
    register(def) {
      defs.push(def)
    },
  },
  get() {
    return undefined
  },
  logger: {
    info: () => {},
    warn: (message) => errors.push(message),
    error: (message) => errors.push(message),
  },
  on: () => {},
}

apply(ctx, { baseUrl: 'http://127.0.0.1:8739', autoStart: false, python: 'python' })

assert.equal(defs.length, 6, `expected 6 tools, got ${defs.length}`)
const names = defs.map((def) => def.name)
assert.deepEqual(
  names,
  ['downloadx_add', 'downloadx_list', 'downloadx_get', 'downloadx_control', 'downloadx_probe', 'downloadx_engine'],
)
for (const def of defs) {
  assert.ok(def.description && def.description.length > 10, `${def.name}: description`)
  const issues = []
  assert.equal(def.parameters.type, 'object', `${def.name}: parameters root must be object`)
  walkSchema(def.parameters, `${def.name}.parameters`, issues)
  walkSchema(def.output.schema, `${def.name}.output`, issues)
  assert.deepEqual(issues, [], `${def.name}: schema issues`)
  assert.equal(typeof def.output.render, 'function', `${def.name}: render`)
  assert.equal(typeof def.execute, 'function', `${def.name}: execute`)
  const rendered = def.output.render({}, { ok: true, text: 'hello' })
  assert.deepEqual(rendered, [{ type: 'text', text: 'hello' }], `${def.name}: render output`)
}

// 参数校验：非法参数必须给出可读错误
const add = defs.find((def) => def.name === 'downloadx_add')
await assert.rejects(() => add.execute({}, {}), /非空的 url/)
const control = defs.find((def) => def.name === 'downloadx_control')
await assert.rejects(() => control.execute({ action: 'pause' }, {}), /需要 task_id/)
await assert.rejects(() => control.execute({ action: 'nope' }, {}), /action 必须是/)
const list = defs.find((def) => def.name === 'downloadx_list')
await assert.rejects(() => list.execute({ limit: 0 }, {}), /limit/)

// 引擎不可用 + autoStart=false：应给出“未运行”的可执行提示
await assert.rejects(() => list.execute({}, {}), /未运行/)

// 配置合并
const config = normalizeConfig({ baseUrl: 'http://127.0.0.1:9911/', startTimeoutMs: 1234 })
assert.equal(config.baseUrl, 'http://127.0.0.1:9911')
assert.equal(config.port, 9911)
assert.equal(config.startTimeoutMs, 1234)
assert.equal(config.autoStart, true)
assert.throws(() => normalizeConfig({ baseUrl: 'ftp://x' }), /baseUrl/)

// 格式化
assert.equal(formatBytes(0), '0 B')
assert.equal(formatBytes(1536), '1.50 KB')
assert.equal(formatBytes(2 * 1024 * 1024), '2.00 MB')
assert.equal(formatSpeed(0), '0 B/s')
assert.equal(formatEta(65), '1m05s')
assert.equal(__internal.percentText({ total_size: 200, downloaded_size: 50 }), '25.0%')
const line = __internal.taskLine({ id: '46d2fc2844634d56923a16f730ae9a37', status: 'downloading', total_size: 2 * 1024 * 1024, downloaded_size: 1024 * 1024, speed: 1024 * 500, eta: 2, filename: 'payload.bin' }, 0)
assert.match(line, /46d2fc28/)
assert.match(line, /downloading/)
assert.match(line, /50\.0%/)

console.log(`unit ok: ${defs.length} tools, ${names.join(', ')}`)
if (errors.length > 0) console.log('logger warnings:', errors)

