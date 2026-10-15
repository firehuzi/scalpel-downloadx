# DownloadX CLI reference

Invoke through the wrapper: `python <skill-dir>/scripts/dx.py <command> [options]`.
Anything after `dx.py` is forwarded verbatim to `python -m downloadx`.

## Global options

Usable before **or** after the command name.

| Option | Meaning |
| --- | --- |
| `--server <url>` | Talk to a running service instead of starting a foreground engine |
| `--local` | Force foreground mode even if a service is reachable |
| `--state-dir <path>` | Where task metadata, history, and the single-instance lock live |
| `--download-dir <path>` | Save destination for this invocation |
| `--json` | Machine-readable output |

With no `--state-dir`, state goes to `%LOCALAPPDATA%\DownloadX` and files to `%USERPROFILE%\Downloads`.

## Commands

| Command | Purpose |
| --- | --- |
| `serve [--host H] [--port P] [--log-level L]` | Foreground service + web UI (default `127.0.0.1:8720`) |
| `add <url>... [opts]` | Create download task(s) |
| `list [--status S] [--sort created\|priority\|updated]` | Show tasks (`ls` works too) |
| `get <task-id>` | One task in detail |
| `pause` / `resume` / `cancel <task-id>` | Task control |
| `retry <task-id> [--reset] [--wait]` | Retry; `--reset` discards partial bytes |
| `rm <task-id> [--delete-file]` | Remove task; optionally delete the file too |
| `pause-all` / `resume-all` | Bulk control |
| `stats` | Totals, speed, connection pool, bandwidth |
| `history [--limit N]` | Completed / failed / canceled records |
| `probe <url>` | Size, filename, resumability — without downloading |
| `config [--set-download-dir P] [--set-max-concurrent N] [--set-global-limit MBps]` | Read or change settings |
| `nl "<text>" [--plan-only]` | Natural-language front end (rules-based by default) |
| `tools` | The 15 deterministic tools the agent API exposes |

### `add` options

| Option | Meaning |
| --- | --- |
| `--dir <path>` | Save directory for this task |
| `--name <file>` | Rename (only with a single URL) |
| `--priority <0-99>` | Lower runs first; default 5 |
| `--conn <1-32>` | Segment connections; see the connection-count guidance in SKILL.md |
| `--limit <MB/s>` | Per-task speed limit; `0` = unlimited |
| `--sha256 <hex>` | Verify after download; mismatch fails the task |
| `--wait` / `--no-wait` | Wait for completion / return immediately |

Waiting defaults: foreground mode (no service) waits; service mode returns as soon as the task is
queued. That asymmetry is the most common surprise — pass the flag explicitly when it matters.

`--json` prints exactly one JSON document per URL, but *which* document depends on `--wait`:

- `--json --wait` → the **final** task state (`status`, `sha256`, `verified`, `elapsed`, `error`);
- `--json` alone → the snapshot taken when the task was queued (`status: "queued"`, no size yet).

So a script must pass `--wait` if it intends to read the outcome, and must check `status` either
way. Reading `sha256` from a queued snapshot always yields `null`.

## Examples

```bash
# Straight download, watch it finish
dx.py add https://example.com/big.iso --wait

# Mirror host known to throttle: stay at 1-2 connections
dx.py add https://mirrors.ustc.edu.cn/debian.iso --conn 1 --wait

# Queue three files, do not block
dx.py add <u1> <u2> <u3> --no-wait --priority 3

# Full path for the file, then verify
dx.py add https://example.com/a.zip --dir D:\staging --name a.zip --sha256 <hex> --json
dx.py get 3f9a1c2b        # first 8 chars of the id are enough for humans, full id for scripts

# Interrupted yesterday? Keep the bytes.
dx.py list --status failed
dx.py retry <task-id> --wait

# Free the disk after moving the file into place
dx.py rm <task-id> --delete-file
```

## Environment variables

Every config field can be overridden as `DOWNLOADX_<FIELD>` (uppercase). The ones worth knowing:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOWNLOADX_DOWNLOAD_DIR` | `~/Downloads` | Save destination |
| `DOWNLOADX_STATE_DIR` | `%LOCALAPPDATA%\DownloadX` | State directory |
| `DOWNLOADX_MAX_CONCURRENT_TASKS` | 3 | How many tasks run at once |
| `DOWNLOADX_MAX_CONNECTIONS_PER_TASK` | 8 | Ceiling for `--conn` |
| `DOWNLOADX_GLOBAL_SPEED_LIMIT` | 0 | Bytes/s across all tasks, 0 = unlimited |
| `DOWNLOADX_MIN_SEGMENT_SIZE` | 4194304 | Below this, a file is not split |
| `DOWNLOADX_VERIFY_SHA256` | true | Compute SHA-256 before finalizing |
| `DOWNLOADX_MAX_RETRIES` | 5 | Per-segment retry ceiling |
| `DOWNLOADX_HOST` / `DOWNLOADX_PORT` | 127.0.0.1 / 8720 | Service bind |
| `DOWNLOADX_RAMP_ENABLED` | true | Concurrency self-calibration for large downloads |
| `DOWNLOADX_RAMP_MIN_TOTAL_SIZE` | 25165824 | Files below 24 MB skip calibration |
| `DOWNLOADX_RAMP_GAIN_THRESHOLD` | 1.15 | Parallel must beat single-connection by this much, or the engine reverts to one connection |

Precedence: environment > `config.json` in the state dir > built-in defaults.

For the optional LLM planner behind `nl`: `DOWNLOADX_LLM_API_KEY` (or `DEEPSEEK_API_KEY`),
`DOWNLOADX_LLM_BASE_URL`, `DOWNLOADX_LLM_MODEL`. Without a key, `nl` still works — it falls back to
rules and never needs the network.

## Statuses and exit codes

`queued` · `downloading` · `paused` · `verifying` · `completed` · `failed` · `canceled`

Exit codes: `0` success, `1` error (message on stderr), `130` interrupted with Ctrl+C — in which
case partial bytes are already on disk and `retry` resumes them.
