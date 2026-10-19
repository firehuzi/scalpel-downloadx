---
name: downloadx
description: Local-first, agent-callable downloader for resumable HTTP/HTTPS, YouTube/Bilibili/X media, and verified file transfers.
version: 0.2.0
author: firehuzi
tags:
  - download
  - downloader
  - agent
  - video
  - file-transfer
---

# DownloadX

DownloadX is a local-first download engine, presented here as an agent skill. It does not reimplement downloading — it locates the engine already installed on the host and forwards your arguments to it.

## What it does
- Resumable multi-connection HTTP/HTTPS downloads with real offset resume
- Queue, priority, bandwidth cap, and background downloads that outlive the current command or session
- SHA-256 verification with atomic rename — a partial file is never handed back as the result
- Native HLS/DASH segment download, resume, and remux to mp4
- Video and audio from YouTube, Bilibili, X/Twitter and other sites (routed to yt-dlp or download-x-video)

## Why a skill, not a prompt
The engine runs entirely independent of any LLM. The skill is a thin, deterministic wrapper, so no model is ever in the download path. That keeps transfers reproducible and auditable.

## Full instructions
See [skill/downloadx/SKILL.md](skill/downloadx/SKILL.md) for the complete command surface, the invariants you must not break, and the connection-tuning guidance.

## License
MIT. Free to use, self-hosted, no account required.
