# 本地 API 参考

默认地址：`http://127.0.0.1:8720`。只监听回环地址，无鉴权（本地工具定位）。
启动：`downloadx serve` 或 `python -m downloadx serve`。

交互式文档：启动后访问 `/docs`（FastAPI 自带 Swagger UI）。

---

## 1. 任务接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：版本、运行时长、下载目录、状态目录 |
| GET | `/api/tasks` | 列出任务。`?status=downloading`、`?order=created\|priority\|updated` |
| POST | `/api/tasks` | 新建下载任务 |
| POST | `/api/tasks/batch` | 批量入队：每条链接一个独立任务，单条失败不影响其余 |
| GET | `/api/tasks/{id}` | 单任务详情 |
| PATCH | `/api/tasks/{id}` | 修改 `priority` / `connections` / `speed_limit` |
| DELETE | `/api/tasks/{id}` | 移除任务。`?delete_file=true` 连文件一起删 |
| POST | `/api/tasks/{id}/pause` | 暂停（保留进度） |
| POST | `/api/tasks/{id}/resume` | 继续（断点续传） |
| POST | `/api/tasks/{id}/cancel` | 取消（保留 `.dxpart`，可再续传） |
| POST | `/api/tasks/{id}/retry` | 重试。`?reset=true` 丢弃进度从零重下 |
| POST | `/api/tasks/pause-all` | 暂停全部 |
| POST | `/api/tasks/resume-all` | 恢复全部 |

### 新建任务

```http
POST /api/tasks
Content-Type: application/json

{
  "url": "https://example.com/big.zip",
  "save_dir": "D:\\Downloads",
  "filename": "big.zip",
  "priority": 0,
  "connections": 16,
  "effective_connections": 16,
  "speed_limit": 5242880,
  "sha256": "e3b0c442...",
  "start": true
}
```

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 必填 | 仅支持 `http://` / `https://` |
| `save_dir` | string | 全局下载目录 | |
| `filename` | string | 由 URL / Content-Disposition 推断 | 显式指定后不再跟随服务端 |
| `priority` | int 0-99 | 5 | 越小越优先 |
| `connections` | int 1-32 | 8 | 分片连接数 |
| `speed_limit` | int | 0 | 单任务限速 bytes/s，0 不限速 |
| `sha256` | string | 无 | 完成后校验，不匹配则任务失败 |
| `start` | bool | true | false 表示只入队不开始 |

### 任务对象（响应）

```json
{
  "id": "3f9a1c2b...",
  "url": "https://example.com/big.zip",
  "filename": "big.zip",
  "save_path": "D:\\Downloads\\big.zip",
  "status": "downloading",
  "mode": "segmented",
  "priority": 5,
  "connections": 16,
  "total_size": 104857600,
  "downloaded_size": 41943040,
  "percent": 40.0,
  "remaining_size": 62914560,
  "segments_total": 8,
  "segments_done": 3,
  "speed": 5242880.0,
  "eta": 12.0,
  "resumable": true,
  "error": null,
  "retry_count": 0,
  "sha256": null,
  "verified": null,
  "plan_reason": "8 分片并发下载",
  "allowed_actions": ["paused", "verifying", "failed", "canceled", "queued"]
}
```

`allowed_actions` 直接来自状态机的合法流转表 —— 前端不需要自己维护"这个状态能点什么"。
`effective_connections` 是本轮实际并发：被服务端限流时会被引擎自动调低
（例如 `16 → 1`），而 `connections` 始终是你配置的值，不会被引擎改写。

---

## 2. 统计 / 历史 / 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/stats` | 全局统计（见下） |
| GET | `/api/history` | 历史记录，`?limit=50&offset=0` |
| GET | `/api/config` | 当前配置（含环境变量覆盖结果） |
| PATCH | `/api/config` | 修改 `download_dir` / `max_concurrent_tasks` / `global_speed_limit` |
| POST | `/api/probe` | 只探测链接，不入队 |
| GET | `/api/events` | 最近事件（环形缓冲，`?limit=50`） |

`/api/stats` 响应节选：

```json
{
  "active_tasks": 2, "queued_tasks": 5, "paused_tasks": 1,
  "completed_tasks": 12, "failed_tasks": 1, "canceled_tasks": 0,
  "total_tasks": 21,
  "download_speed": 10485760.0,
  "total_remaining_bytes": 2147483648,
  "status_counts": {
    "queued": 5, "downloading": 2, "paused": 1, "verifying": 0,
    "completed": 12, "failed": 1, "canceled": 0
  },
  "queue": {
    "pending": 5, "active": 2, "max_concurrent": 3, "globally_paused": false,
    "bandwidth": { "global_rate": 0, "task_limits": {} }
  },
  "scheduler": { "running": 2, "started_total": 21, "finished_total": 19 },
  "connections": { "limit": 32, "in_use": 12, "available": 20, "peak_in_use": 18 },
  "network_online": true,
  "version": "0.2.0",
  "app": "DownloadX"
}
```

---

## 3. WebSocket 事件流

`ws://127.0.0.1:8720/api/ws`

连接建立后立刻收到一条 `snapshot`（全量任务 + 统计），之后持续收到增量事件；
空闲时每 3 秒推一条 `stats` 心跳。

```json
{ "type": "task_progress", "task_id": "3f9a1c2b", "ts": 1770000002.1,
  "data": { "percent": 40.0, "downloaded_size": 41943040,
            "total_size": 104857600, "speed": 5242880.0, "eta": 12.0 } }
```

| 事件类型 | 触发时机 | 关键字段 |
| --- | --- | --- |
| `snapshot` | 连接建立 | `tasks[]`, `stats` |
| `stats` | 空闲心跳（3s） | 同 `/api/stats` |
| `task_added` | 新建任务 | `url`, `filename` |
| `task_probed` | 探测完成 | `size`, `resumable`, `mode`, `plan_reason` |
| `task_started` | 开始传输 | `mode`, `connections`, `total_size` |
| `task_progress` | 传输中（限流 0.3s/次） | `percent`, `speed`, `eta` |
| `segment_retry` | 分片重试 | `index`, `attempt`, `delay`, `error` |
| `task_throttled` | 被服务端限流，自动降并发 | `connections`, `previous`, `reason` |
| `task_ramp_started` | 大文件开始并发自校准 | `connections`, `warmup_seconds`, `probe_seconds` |
| `task_ramp_kept` | 自校准结论：并行有收益，保留并发 | `connections`, `baseline`, `ramped`, `gain` |
| `task_ramp_down` | 自校准结论：并行没收益，退回单连接 | `connections`, `previous`, `baseline`, `ramped`, `gain` |
| `probe_retry` | 探测重试 | `attempt`, `delay`, `error` |
| `segment_done` | 分片完成 | `index`, `received` |
| `task_verifying` | 进入校验 | `sha256_expected` |
| `task_completed` | 完成 | `save_path`, `size`, `sha256`, `elapsed` |
| `task_paused` / `task_canceled` / `task_failed` | 暂停 / 取消 / 失败 | `reason` / `error`, `code` |
| `task_recovered` | 崩溃恢复 | `percent`, `requeued` |
| `task_invalidated` | 远端文件变了，分片作废 | `reason` |
| `task_removed` | 移除 | `delete_file` |
| `config_updated` / `network_changed` | 配置变更 / 网络状态 | 对应字段 |

前端不需要轮询进度：连上 WS 就有全量快照，之后增量更新。

---

## 4. 错误响应

统一格式 `{"detail": "..."}`，状态码语义：

| 状态码 | 含义 | 典型场景 |
| --- | --- | --- |
| 400 | 参数错误 | URL 协议不支持、状态过滤值非法 |
| 404 | 任务不存在 | id 写错、任务已被移除 |
| 409 | 状态冲突 | 恢复一个已完成的正常任务、运行中改连接数 |
| 500 | 服务器内部错误 | 未预期异常（会带异常类型） |

```json
{ "detail": "状态 completed 无法恢复" }
```

---

## 4.5 访问令牌

默认**不鉴权** —— 服务只监听 `127.0.0.1`，这是本地工具的常态。

设置 `api_token` 之后（`downloadx token` 或 `downloadx token --rotate`）：

| 规则 | 说明 |
| --- | --- |
| 保护范围 | 所有 `/api/*` 与 `/agent/*` |
| 免鉴权 | `/api/health`（探活用） |
| 携带方式 | `Authorization: Bearer <token>`，或 `X-DownloadX-Token: <token>` |
| WebSocket | `/api/ws?token=<token>`（浏览器无法给 WS 设请求头） |
| CORS | **只有启用令牌后才放开**，避免任意网页 drive-by 调用本地 API |
| 生效 | 改完令牌需重启服务 |

```powershell
downloadx token           # 查看（没有就生成一个）
downloadx token --rotate  # 轮换，旧的立即失效
downloadx token --clear   # 关闭鉴权
```

---

## 5. 命令行等价物

| REST | CLI |
| --- | --- |
| `POST /api/tasks` | `downloadx add <url>` |
| `GET /api/tasks` | `downloadx list` |
| `GET /api/stats` | `downloadx stats` |
| `POST /api/tasks/{id}/pause` | `downloadx pause <id>` |
| `POST /api/tasks/{id}/retry?reset=true` | `downloadx retry <id> --reset` |
| `DELETE /api/tasks/{id}?delete_file=true` | `downloadx rm <id> --delete-file` |
| `POST /api/probe` | `downloadx probe <url>` |
| `PATCH /api/config` | `downloadx config --set-max-concurrent 5` |

CLI 会自动判断本机是否已有服务：有就连过去（多端共享同一个引擎），
没有就自己起一个前台引擎边下边显示进度。
