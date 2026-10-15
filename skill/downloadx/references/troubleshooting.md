# When a download misbehaves

Diagnose from the task record first — it carries the state, the error, and the retry count:

```bash
dx.py get <task-id> --json
dx.py history --limit 20
dx.py stats
```

## Where state lives

`%LOCALAPPDATA%\DownloadX` by default:

- `tasks/<id>.json` — one file per task, rewritten atomically (tmp + rename);
- `tasks/index.json` — task order;
- `history.jsonl` — append-only finished records, newest last;
- `downloadx.lock` — single-instance lock (pid + host + heartbeat).

Reading these in PowerShell needs `-Encoding UTF8`, otherwise Chinese fields look corrupted.
If reading with Python, open with `encoding="utf-8"`.

## Symptom → cause

**"状态目录被占用" / lock error.** A service (or another foreground command) already owns that
state dir. Do not delete the lock by hand — talk to the running engine with
`--server http://127.0.0.1:8720`, or pass `--local` only when you know nothing is running. The lock
expires on its own if the owner died without cleaning up (heartbeat older than 30s).

**Lots of `segment_retry` involving HTTP 429 / 503.** The host is throttling by concurrency. Lower
`--conn`; the engine already narrows its own concurrency on those statuses and reports it as
`task_throttled` (`/api/tasks/{id}` shows `effective_connections`). Expect the first seconds of an
aggressive run to be slower — that is the discovery cost, not a bug.

**Download is slower than a plain single-connection fetch, but nothing errors.** The host is
throttling per connection silently. For files of 24 MB or more the engine measures this itself:
look for `task_ramp_down` in the event stream (gain < 1.15x) and `effective_connections` in the
task record — it will have reverted to one connection on its own. If you see `task_ramp_kept`
instead, parallelism genuinely was faster and the bottleneck is elsewhere. For files under the
24 MB floor there is no measurement, so pass `--conn 1` yourself. Note the baseline deliberately
discards its priming window; if you re-measure by hand, do the same, or TLS/slow start will make a
single connection look far slower than it is.

**Failed with "服务端忽略了 Range 请求，续传不可用".** The server ignores Range, so the task ran in
`single` mode and cannot resume. Nothing is broken; a retry re-downloads from zero.

**`mode: "streaming"`.** The server sent no `Content-Length`, so progress is unknown and resume is
impossible — this is an HTTP-level limit, not a defect. `probe` tells you this before committing.

**Failed with "连接提前结束：分片 x 只收到 a/b 字节".** A truncated body. It is retryable, and the
retry resumes at the byte offset already written, so nothing is re-downloaded.

**`error_code: "checksum_mismatch"`.** The bytes arrived but the SHA-256 did not match, so the
engine deleted the `.dxpart` on purpose — a wrong file must never reach the final path. Re-check the
expected hash, and compare `probe` output with the previous run: if `size` or `etag` changed, the
remote file was replaced and the old hash cannot match.

**`task_invalidated`.** On resume the engine noticed the remote file changed (size / ETag /
Last-Modified) and discarded the partial bytes instead of stitching two different files together.

**`task_recovered`.** A previous process died mid-download; the restarted engine read the
checkpoint and requeued the task. Progress was not lost.

**A task sits in `queued` and nothing happens.** Nothing is running the engine. Foreground commands
start and stop it; a queued task only advances while a process is alive. Start `serve`, or run the
task with `--wait`.

**Download is slow but the network looks fine.** Check `stats` for the connection pool and the
global speed limit — a leftover `DOWNLOADX_GLOBAL_SPEED_LIMIT` or a per-task `--limit` is easy to
forget. `dx.py config` shows the effective configuration.

**Interrupted with Ctrl+C.** That is a clean pause: partial bytes stay on disk and the task shows
as paused. Continue with `dx.py retry <task-id>` (or `resume`).

**The file never appears at its final path.** It is still `<name>.dxpart`. Only a verified,
complete download gets renamed into place.

## Escalation

If a task fails twice the same way with a plausible hash and a working URL, capture
`dx.py get <task-id> --json`, `dx.py history --limit 5`, and the server's response headers
(`dx.py probe <url> --json`), then report those three — that is enough to tell apart throttling,
protocol limits, and a genuine engine bug.
