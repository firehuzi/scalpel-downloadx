# Driving DownloadX over HTTP

Use this when the work is not a single shell command: long-running monitoring, batch control,
several agents sharing one queue, or progress that must be observed while it happens.

The engine is agent-native: the engine never needs a model, and every tool below is a
deterministic function call. The optional LLM layer only translates sentences into those calls.

## Start the service

```bash
dx.py serve --port 8720
```

Then talk to `http://127.0.0.1:8720`. Only a running service accepts concurrent clients; a
foreground `add` holds an exclusive lock on its state directory.

## REST

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | version, uptime, directories |
| GET | `/api/tasks?status=&order=` | task list (`order`: created / priority / updated) |
| POST | `/api/tasks` | create; body accepts `url`, `save_dir`, `filename`, `priority`, `connections`, `speed_limit`, `sha256`, `start` |
| GET | `/api/tasks/{id}` | one task; watch `percent`, `speed`, `eta`, `effective_connections`, `sha256`, `verified`, `error` |
| PATCH | `/api/tasks/{id}` | change `priority` / `connections` / `speed_limit` |
| DELETE | `/api/tasks/{id}?delete_file=` | remove |
| POST | `/api/tasks/{id}/pause` · `/resume` · `/cancel` · `/retry?reset=` | control |
| POST | `/api/tasks/pause-all` · `/resume-all` | bulk |
| GET | `/api/stats` | totals, queue, connection pool, bandwidth |
| GET | `/api/history?limit=&offset=` | finished records |
| POST | `/api/probe` | inspect a URL before committing to it |
| GET | `/api/config`, PATCH `/api/config` | read / change global settings |

Errors: `400` bad argument or illegal state change, `404` unknown task, `409` conflicting state.
Body is always `{"detail": "..."}`.

## WebSocket

`ws://127.0.0.1:8720/api/ws` — a full `snapshot` on connect, then incremental events, plus a
`stats` heartbeat when idle. Polling is unnecessary.

Events worth handling: `task_progress` (throttled to ~3/s per task), `task_started`,
`segment_retry` (transient trouble), `task_throttled` (the engine lowered its own concurrency —
`connections` dropped from `previous`), `task_verifying`, `task_completed`, `task_paused`,
`task_canceled`, `task_failed`, `task_recovered` (crash recovery requeued it), `task_invalidated`
(the remote file changed, partial bytes were discarded), `task_removed`.

Large downloads (≥24 MB) also emit the concurrency-calibration trio: `task_ramp_started`
(`connections`, `warmup_seconds`, `probe_seconds`), then either `task_ramp_kept` or
`task_ramp_down` with `baseline`, `ramped`, and `gain` (aggregate throughput divided by the
single-connection baseline). Read `effective_connections` — not `connections` — to report the
concurrency actually used.

## Agent tools

`GET /agent/manifest` returns the tool schemas; `GET /agent/openai-tools` returns the same set in
OpenAI function-calling format, ready to hand to a model as `tools=[...]`. Invoke with
`POST /agent/tool {"name": ..., "arguments": {...}}`.

| Tool | Side effects | Purpose |
| --- | --- | --- |
| `add_download` | yes | Create a task (`url`, `save_dir`, `filename`, `priority`, `connections`, `speed_limit`, `sha256`, `start`) |
| `list_downloads` | no | Filter by `status`, `order`, `limit` |
| `get_download` | no | One task |
| `pause_download` / `resume_download` / `cancel_download` | yes | Per-task control |
| `retry_download` | yes | Retry; `reset: true` restarts from zero |
| `remove_download` | yes | Remove; `delete_file: true` also deletes the file |
| `set_priority` / `set_speed_limit` | yes | Retarget scheduling while it runs |
| `pause_all` / `resume_all` | yes | Bulk control |
| `get_stats` / `get_history` | no | Reporting |
| `probe_url` | no | Inspect before downloading |

Every response is `{"ok": true, "tool": "...", "result": {...}}`, and `result` is the engine's own
task object — no follow-up query needed. Tools marked "yes" carry `side_effects: true` in the
manifest so a caller can decide whether to confirm with the user first.

## Natural language

`POST /agent/nl {"text": "把那个大文件暂停", "execute": true, "planner": "auto"}`.
`planner` is `auto` (LLM if configured, else rules), `rules` (offline, deterministic), or `llm`.
The rules parser handles URL extraction, intent words, task lookup by id prefix or filename
substring, and parameters like `优先级 2` / `限速 2MB/s` / `从零重下`. When it understands nothing
it returns an empty `tool_calls` list rather than guessing.

Prefer the deterministic tools over `nl` for anything scripted.

## Minimal client

```python
import time, httpx

api = httpx.Client(base_url="http://127.0.0.1:8720", timeout=30)
task = api.post("/api/tasks", json={"url": url, "sha256": expected}).json()

while True:
    current = api.get(f"/api/tasks/{task['id']}").json()
    if current["status"] in ("completed", "failed", "canceled"):
        break
    time.sleep(1)

assert current["status"] == "completed" and current["verified"] is not False
```
