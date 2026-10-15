# Agent API：把下载器当成一套工具

这一层的存在理由是任务书的第一原则：

> **AI 是控制层，不是下载引擎。** 引擎必须能完全脱离 LLM 独立运行。

结构：

```
  LLM / 自动化脚本 / 另一个 Agent
            │  ① 读工具清单 GET /agent/manifest
            │  ② 调用工具   POST /agent/tool
            ▼
      agent_api（薄封装，纯函数式，无模型）
            ▼
      DownloadManager（引擎）
```

引擎本身不知道"Agent"的存在；`agent_api` 也只是把 `DownloadManager` 的方法
翻译成带 schema 的工具。**没有任何一条路径需要模型参与。**

---

## 1. 工具清单

```http
GET /agent/manifest
```

```json
{
  "app": "DownloadX",
  "version": "0.1.0",
  "principles": [
    "下载引擎独立运行，LLM 不是必需依赖",
    "所有工具调用都是确定性的，可脱机执行",
    "任务状态机严格校验，非法流转会被拒绝"
  ],
  "tools": [
    { "name": "add_download", "description": "...", "parameters": { "..." : "..." },
      "returns": "下载任务对象", "side_effects": true }
  ],
  "openai_tools": [
    { "type": "function", "function": { "name": "add_download", "description": "..." } }
  ]
}
```

`openai_tools` 就是 OpenAI function-calling 格式，可以**原样**塞进 `tools=[...]`，
直接给支持工具调用的模型使用。

### 15 个工具

| 工具 | 参数 | 副作用 | 说明 |
| --- | --- | --- | --- |
| `add_download` | `url`, `save_dir?`, `filename?`, `priority?`, `connections?`, `speed_limit?`, `sha256?`, `start?` | 是 | 新建任务 |
| `list_downloads` | `status?`, `order?`, `limit?` | 否 | 列出任务 |
| `get_download` | `task_id` | 否 | 单任务详情 |
| `pause_download` | `task_id` | 是 | 暂停（保留进度） |
| `resume_download` | `task_id` | 是 | 续传 |
| `cancel_download` | `task_id` | 是 | 取消（保留 `.dxpart`） |
| `retry_download` | `task_id`, `reset?` | 是 | 重试（`reset=true` 从零） |
| `remove_download` | `task_id`, `delete_file?` | 是 | 移除任务 |
| `set_priority` | `task_id`, `priority` | 是 | 调整优先级 |
| `set_speed_limit` | `task_id`, `speed_limit` | 是 | 单任务限速 |
| `pause_all` | — | 是 | 暂停全部 |
| `resume_all` | — | 是 | 恢复全部 |
| `get_stats` | — | 否 | 全局统计 |
| `get_history` | `limit?` | 否 | 历史记录 |
| `probe_url` | `url`, `headers?` | 否 | 只探测不下载 |

`side_effects` 让上层 Agent 自己决定"要不要先问用户"。

---

## 2. 调用工具

```http
POST /agent/tool
Content-Type: application/json

{ "name": "add_download",
  "arguments": { "url": "https://example.com/a.zip", "priority": 0, "connections": 16 } }
```

```json
{
  "ok": true,
  "tool": "add_download",
  "result": { "id": "3f9a1c2b...", "status": "queued", "mode": "segmented" }
}
```

返回值直接是引擎的任务对象，Agent 不需要再查一次。

错误约定：未知工具 / 缺参数 → `400`；任务不存在 → `404`；状态冲突 → `400`（带原因）。

```bash
curl -s http://127.0.0.1:8720/agent/tool \
  -H "Content-Type: application/json" \
  -d '{"name":"get_stats","arguments":{}}'
```

---

## 3. 自然语言接口（可选 LLM，默认规则）

```http
POST /agent/nl
{ "text": "把那个大文件暂停，其他继续", "execute": true, "planner": "auto" }
```

```json
{
  "input": "把那个大文件暂停，其他继续",
  "planner": "rules",
  "executed": true,
  "tool_calls": [ { "name": "pause_download", "arguments": { "task_id": "..." }, "ok": true } ],
  "results": [ { "ok": true, "tool": "pause_download", "result": { "status": "paused" } } ],
  "reply": "big.zip 现在是 paused"
}
```

### `planner` 取值

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 有可用 LLM 就先用 LLM 解析，失败或未配置就退回规则 |
| `rules` | 强制规则解析（离线、确定性、零 token 成本） |
| `llm` | 只用 LLM 解析 |

### 规则解析器能处理什么

- URL 识别 + 意图词匹配（下载 / 暂停 / 继续 / 取消 / 重试 / 删除 / 列表 / 统计 / 历史 / 探测）
- 任务定位：8-32 位十六进制 id 前缀，或按文件名子串模糊匹配（"暂停 big.zip"）
- 参数抽取：`优先级 2`、`用 16 个连接`、`限速 2MB/s`、`从零重下`、`连文件一起删`
- "全部 / 所有" → `pause_all` / `resume_all`
- 听不懂时返回空 `tool_calls` 并回复"没听懂要做什么"，**绝不猜着执行**

```bash
downloadx nl "下载 https://example.com/a.zip 优先级 0"
downloadx nl "现在有哪些任务"
downloadx nl "暂停 big.zip"
downloadx nl "看一下统计" --json
```

### 接 LLM（可选）

```powershell
$env:DOWNLOADX_LLM_API_KEY  = "sk-..."                       # 或 DEEPSEEK_API_KEY
$env:DOWNLOADX_LLM_BASE_URL = "https://api.deepseek.com"     # 默认值
$env:DOWNLOADX_LLM_MODEL    = "deepseek-chat"                # 默认值
```

模型只做一件事：把一句话翻译成 `tool_calls`。它**没有**下载能力，也不需要理解
分片、Range、续传。翻译失败（网络不通、返回格式不对）自动退回规则解析，用户无感。

---

## 4. 一个完整的 Agent 循环（示例）

```python
import time
import httpx

BASE = "http://127.0.0.1:8720"
api = httpx.Client(base_url=BASE, timeout=30)

# 1) 拿到 OpenAI 格式的工具定义，交给模型
tools = api.get("/agent/openai-tools").json()

# 2) 模型决定调用 add_download 后，执行它
api.post("/agent/tool", json={
    "name": "add_download",
    "arguments": {"url": "https://example.com/iso", "sha256": "e3b0..."},
})

# 3) 等它下完（生产环境建议订阅 /api/ws 拿实时事件）
while True:
    result = api.post("/agent/tool", json={
        "name": "list_downloads", "arguments": {"status": "downloading"},
    }).json()
    if not result["result"]["count"]:
        break
    time.sleep(2)

# 4) 汇报
print(api.post("/agent/tool", json={"name": "get_stats", "arguments": {}}).json())
```

---

## 5. 设计红线（改这一层时必须守住）

1. **`agent_api` 里不许出现下载逻辑**：它只能调用 `DownloadManager`。
2. **不许让模型决定"怎么下"**：分片数、Range、重试、校验都由引擎决定。
3. **不许让模型成为必需依赖**：没有 Key、没有网络时，全部功能仍可用。
4. **副作用工具必须显式标记**：`side_effects=true`，由上层决定是否需要二次确认。
