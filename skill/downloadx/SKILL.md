---
name: downloadx
description: Download files and media on this machine — resumable multi-connection HTTP/HTTPS downloads plus video/audio from YouTube, Bilibili, X/Twitter and other sites (routed to yt-dlp or download-x-video as appropriate). Use when a download is large, slow, flaky, interrupted, needs integrity checking, must keep running after the current command ends, or when the user asks to download a video, audio, or a file from a link, and when they ask about download progress, queue, or bandwidth. Not for fetching a small page or API response (ordinary HTTP tooling is fine).
---

# DownloadX

The engine already exists on this machine and is built for exactly this: a real downloader
instead of a one-shot fetch. This skill is a thin wrapper around it — the wrapper never
reimplements downloading, it only finds the engine and forwards your arguments.

## When it is worth using

A plain fetch is fine for a small file over a healthy connection. Use the engine when any of
these holds:

- the file is large (roughly >100 MB) or the connection is slow / behind a flaky mirror;
- the transfer must survive a dropped connection, an interrupted command, or a reboot;
- the user wants progress, a queue, priorities, a speed limit, or a background download;
- the bytes must be verified (the user gave a hash, or a bad file would cost real time).

## Entry point

Always go through the wrapper. It locates the engine, picks an interpreter that actually has
the dependencies, and passes every remaining argument through unchanged:

```bash
python <skill-dir>/scripts/dx.py <downloadx arguments...>
```

`python <skill-dir>/scripts/dx.py doctor` prints the engine path, interpreter, and version it
resolved. Run it first whenever a command behaves unexpectedly.

If the project ever moves, point the wrapper at it once with `DOWNLOADX_HOME=<path>` instead of
hardcoding a new location.

## The shapes worth knowing

```bash
# one file, watch it finish (foreground: no service needed)
dx.py add <url> --wait

# several files at once — they queue; lower --priority number runs first, 0 is highest
dx.py add <url1> <url2> --priority 2 --conn 2

# must outlive this command/turn: start the service, then enqueue and return
dx.py serve                     # foreground server + web UI on 127.0.0.1:8720
dx.py add <url> --no-wait       # in service mode `add` returns as soon as it is queued

# integrity matters: pass the expected hash
dx.py add <url> --sha256 <hex>

# what happened?
dx.py get <task-id>             # status / percent / speed / sha256 / verified / error
dx.py history --limit 20
dx.py stats
```

Add `--json` to any command for machine-readable output.

## Invariants you must not break

- **Incomplete data is never the file.** Downloads write to `<name>.dxpart` and are atomically
  renamed only after the optional SHA-256 matches. Treat `.dxpart` as "not done", never as the
  result to hand back to the user.
- **One writer per state directory.** A second process on the same `--state-dir` refuses to
  start and points at the running service. Do not work around it by copying the state dir;
  use `--server http://127.0.0.1:8720` to talk to the running engine, or `--local` to force a
  foreground engine when you know no service is running.
- **A failed task is not a lost download.** `retry` resumes from the bytes already on disk
  (`--reset` re-downloads from zero). Prefer `retry` over re-`add` after an interruption.
- **Do not put an LLM in the loop.** `dx.py nl` exists for human convenience; for scripts and
  agent-to-agent use, call deterministic commands or the HTTP/agent tools instead.

## Connection count: assume 1 until measured otherwise

Mirrors and CDNs commonly rate-limit *per connection*, so extra connections can make a download
several times slower without producing any error. Two flavours, two responses:

- **Visible** (`HTTP 429/503/504`): the engine notices by itself, narrows concurrency, and reports
  `task_throttled`.
- **Silent** (no error, just slower): for files of **24 MB or more** the engine measures it. It
  runs one connection first to get a baseline (priming excluded, so TLS and TCP slow start do not
  deflate the number), then opens the configured concurrency and compares aggregate throughput.
  Less than a 1.15x gain means parallelism buys nothing and it narrows back to a single
  connection — reported as `task_ramp_down` (or `task_ramp_kept`) and readable as
  `effective_connections` on the task.

What that means for a given download:

- **24 MB or more**: let the engine settle it. Leave `--conn` at the default; it converges within
  a few seconds of measurement (during which it is still downloading, just not in parallel).
- **Under 24 MB**: no measurement happens, so your up-front choice is final. Use `--conn 1` unless
  you know the host rewards parallelism.
- **Known throttling host** (CN mirrors): pass `--conn 1` or `2` up front anyway — the calibration
  would reach the same answer, but you save it the retries and the measurement time.

Evidence from this machine:

- interleaved A/B against one CDN: 1 connection moved a 10 MB file in 2.9–14 s while 2 and 4
  connections needed 58–95 s for the same bytes, no errors either way;
- same CDN, engine-measured on 100 MB: one connection did 5.10 MB/s, eight aggregated 3.95 MB/s
  (23% *worse*) → the engine reverted to one connection;
- a CN mirror answered HTTP 429 at 4 and 16 connections while 1 connection held 10.4 MB/s;
- loopback: 8 connections reached 96 MB/s against 49 MB/s for one — so parallelism is genuinely
  worth keeping where the host allows it.

`--conn` is never rewritten by the engine. Report `effective_connections`, not `connections`,
when explaining how fast a download actually ran.

For a host you use repeatedly, and for files under the 24 MB measurement floor, settle it with the
ruler — interleaving the runs (1, 2, 4, 1, 2, 4), because throughput from this network drifted 40x
in one session for an unchanged setting:

```bash
python <engine-dir>/scripts/bench_conn.py <url> --connections 1,2,4 --rounds 3
```

## Going further

- Full command surface and flags: [references/cli.md](references/cli.md)
- Driving it from another agent or a script over HTTP (REST + WebSocket + tool manifest):
  [references/agent-api.md](references/agent-api.md)
- Slow, stalled, or failed downloads, and where state lives:
  [references/troubleshooting.md](references/troubleshooting.md)
- Video/audio from YouTube, Bilibili, X and other sites — tool choice, required flags, quality
  ceilings, cookie reality: [references/video-sites.md](references/video-sites.md)
- This machine's AI workbench (`我的AI助手`, `127.0.0.1:8765`) hosts this skill in its Skill 库 and
  exposes the panel as a one-click app on port 8720. One command pushes skill changes to the file
  copies, the workbench, and the archive: `python <engine-dir>/scripts/sync_skill.py`.

## Video and audio from sites

One entry point routes by URL; do not hand-assemble yt-dlp flags per session:

```bash
python <skill-dir>/scripts/fetch_video.py <url>                  # best quality
python <skill-dir>/scripts/fetch_video.py <url> --quality 1080    # cap the height
python <skill-dir>/scripts/fetch_video.py <url> --audio           # audio only (m4a, no re-encode)
python <skill-dir>/scripts/fetch_video.py <url> --inspect         # list formats, download nothing
python <skill-dir>/scripts/fetch_video.py <url> -- --write-subs   # extra yt-dlp flags, verbatim
```

| URL | Goes to | Why |
| --- | --- | --- |
| YouTube, Bilibili, Vimeo, Twitch, AcFun, 抖音, niconico… | yt-dlp | the only thing that can extract them |
| X / Twitter status | the `download-x-video` skill | purpose-built, cookie-free, self-updating |
| direct media link (`.mp4`, `.zip`, `.iso`…) | the DownloadX engine | resume, queue, checksum are its job |
| `.m3u8` / `.mpd` | yt-dlp | the engine does not parse stream manifests |

Four things that otherwise cost an hour each:

- **Bilibili answers HTTP 412 unless four headers are sent together** — `Referer` + `Origin` +
  `Accept-Language: zh-CN` + a desktop Chrome UA. Referer alone, and Referer plus UA, both still
  412. The wrapper applies all four.
- **The default is one video, not the collection.** Pass `--playlist` when a whole list is
  genuinely wanted: it enumerates the entries first, then downloads them one at a time with a
  state file (`.downloadx-playlist.json`) in the output directory. Re-running the same command
  only retries failed/new entries; `--redo` forces everything again.
- **Without login cookies Bilibili caps at 480P**; YouTube 4K works without cookies.
  `--cookies-from-browser` does not work on this Windows box (app-bound encryption, yt-dlp
  issue #7271) — export `cookies.txt` and pass `--cookies`.
- **ffmpeg is mandatory above 720P** (separate video/audio streams). It is present here; the
  wrapper warns instead of failing silently when it is missing.

Never install tools silently. If yt-dlp, ffmpeg, aria2c, or BBDown is missing, report it and let
the user decide.
