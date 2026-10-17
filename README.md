# 刃研社-DownloadX v0.1

Windows 优先、跨平台设计、本地优先、无广告、低资源占用、**可被 Agent 调用**的下载器。

> 代码里的标识仍是 `downloadx`（Python 包名、`%LOCALAPPDATA%\DownloadX` 状态目录、CLI 命令名），
> 「刃研社-DownloadX」是它的正式名称。改名会让已有任务记录和已装 skill 对不上，所以两者刻意分开。

仓库 <https://github.com/firehuzi/scalpel-downloadx> · 许可 [MIT](LICENSE) · Python ≥ 3.10 ·
依赖只有 5 个（aiohttp / httpx / fastapi / uvicorn / pydantic）

它不是迅雷的复刻，目标是一个 **可靠、可扩展、可被程序调用的本地下载基础设施**：

```
用户 / LLM / 脚本
        │  自然语言 · URL · 工具调用
        ▼
  Download Agent（api/agent_api）
        │  确定性工具调用
        ▼
  Download Engine（core + queue + network + storage）   ←── 完全独立，不需要 LLM
        ▼
   本地文件
```

**核心约束：AI 是控制层，不是下载引擎。** 关掉网络、删掉 API Key、断开一切模型，
下载引擎照常工作（测试里就是这么跑的）。

---

## 快速开始

```powershell
# 1. 装依赖（只有 5 个）
cd outputs\DownloadX
pip install -r requirements.txt

# 2. 方式 A：启动服务 + 内置 Web UI
python -m downloadx serve
#   打开 http://127.0.0.1:8720/

# 3. 方式 B：直接下，像 wget 一样（不需要服务）
python -m downloadx add https://example.com/big.zip
python -m downloadx add https://example.com/big.zip --conn 16 --priority 0 --limit 5

# 4. 安装成命令行工具（可选）
pip install -e .
downloadx serve
```

视频/音频（YouTube、B 站、X 等）走额外工具链：**yt-dlp** 负责解析、**ffmpeg** 负责合并
（720P 以上一定是音视频分离的）。两者缺失时 skill 只报告、不自动安装：

```powershell
python -m pip install -U yt-dlp     # 解析
winget install Gyan.FFmpeg          # 合并（或自行放置 ffmpeg 到 PATH）
```

常用命令：

```powershell
downloadx add <url>                  # 前台下载，带进度条
downloadx add <url> --no-wait        # 只入队（交给常驻服务下载）
downloadx list --status downloading  # 列出下载中的任务
downloadx get <task_id>              # 单任务详情
downloadx pause|resume|cancel <id>   # 暂停 / 继续 / 取消
downloadx retry <id> [--reset]       # 重试（默认断点续传，--reset 从零重下）
downloadx rm <id> [--delete-file]    # 移除任务
downloadx stats | history | probe <url> | config
downloadx nl "把那个大文件暂停，其他继续"     # 自然语言（默认规则解析，可接 LLM）
downloadx tools                      # 列出 15 个可被 Agent 调用的工具
```

---

## MVP 能力（任务书 §2 全部覆盖）

| 能力 | 状态 | 实现位置 |
| --- | --- | --- |
| HTTP / HTTPS / URL 下载 | ✅ | `network/http_client.py` |
| 多连接分片下载 | ✅ | `core/segment.py` + `core/download_worker.py` |
| HTTP Range 协商（含"服务端假装支持"的识别） | ✅ | `network/range_request.py` |
| 断点续传（真实偏移续传） | ✅ | `core/download_worker.py` + `storage/segment_store.py` |
| 暂停 / 恢复 / 取消 | ✅ | `core/scheduler.py` 协作式停止 |
| 重试（区分临时错误与永久错误） | ✅ | `network/retry.py` |
| 下载队列 + 优先级 | ✅ | `queue/priority_queue.py` |
| 速度显示 / 剩余时间估算 | ✅ | `core/metrics.py` |
| 文件大小 / 已完成大小 / 状态 | ✅ | `core/download_task.py` |
| 下载历史 / 错误记录 | ✅ | `storage/metadata_store.py` |
| SHA-256 校验（校验通过才改名成最终文件） | ✅ | `storage/checksum.py` |
| 崩溃恢复（重启自动续传） | ✅ | `storage/checkpoint.py` + `core/download_manager.py` |
| 带宽控制（全局 + 单任务） | ✅ | `queue/bandwidth_controller.py` |
| 自适应并发（可见限流 429/503 自动降级 + 静默限速靠自校准发现） | ✅ | `core/download_worker.py` |
| HLS / DASH 流媒体（分片下载 + 按分片续传 + 合并成 mp4） | ✅ | `network/hls.py`、`network/dash.py` + `core/download_worker.py` |
| 本地 API + Web UI + Agent API | ✅ | `api/`、`ui/index.html` |

分阶段推进的项见 [docs/ROADMAP.md](docs/ROADMAP.md)：浏览器扩展与 API 鉴权已完成（P1），
媒体能力补全（P2）、BT / Magnet / DHT（P3）、账号与云同步（P4）、LLM 对话（P5）依次排期。

**明确不做**：网盘破解、DRM 绕过 —— 绕过访问控制不是下载功能。

---

## 关键设计取舍

**1. 未完成的数据永不等于最终文件。**
所有下载先落到 `<文件名>.dxpart`，只有 SHA-256 校验通过后，才用 `os.replace`
原子改名成最终文件。断电、崩溃、校验失败都不会留下"看起来下好了其实是坏文件"的结果。

**2. 续传靠偏移，不靠"续传 API"。**
每个分片失败后，用 `Range: bytes=<已收字节>-<结束>` 重新请求。所以暂停、断网、
换网络、进程被杀，恢复逻辑都是同一条路径。

**3. 探测必须可信。**
服务端返回 404/500 时，绝不把错误响应体的 `Content-Length` 当成文件大小
（这个坑在开发中真实踩到过，已修）。HEAD 不被允许时才回退到 `Range: bytes=0-0` 验证。

**4. 默认 `Accept-Encoding: identity`。**
下载器必须拿原始字节，一旦被 gzip 解压，Range 偏移和进度就全错。

**5. 重启后先比对远端是否变了。**
恢复任务前用大小 / ETag / Last-Modified 判断文件是否被替换；变了就丢弃旧分片重下，
宁可重下也不要拼出一个坏文件。

**6. "连接数越多越快"是错的，而且引擎现在会自己发现这件事。**
实测中科大镜像在 4/16 连接下直接返回 429；CacheFly 则是在 8 连接时聚合吞吐反而比
单连接低 23%（3.95 vs 5.10 MB/s）——后者不报错，属于静默按连接限速。
引擎的处理分两层：收到 429/503/504 就立刻收紧并发；对静默限速则在**同一个任务内**
先单连接量一段、再放开量一段（预热后采样，排除 TLS 慢启动），聚合吞吐没有 15% 以上
提升就退回单连接，并把结论写进 `plan_reason`。真机实测：8 连接被判"没收益"，自动回到 1。

---

## 项目结构

```
DownloadX/
├── downloadx/
│   ├── core/        # 引擎：任务、状态机、调度、执行、事件、计量
│   ├── network/     # HTTP 客户端、连接池、Range、重试、网络监视
│   ├── storage/     # 文件写入、分片账本、元数据、检查点、校验、单实例锁
│   ├── queue/       # 优先级队列、令牌桶限速、任务队列
│   ├── api/         # local_api（REST+WebSocket）、agent_api（工具调用）
│   ├── ui/          # 单文件 Web UI（离线可用，无 CDN）
│   ├── cli.py       # 命令行（前台下载 / 连服务两种模式）
│   └── config.py    # 配置（默认值 -> config.json -> 环境变量）
├── extension/       # 浏览器扩展（MV3）：右键链接直接交给本机引擎
├── tests/           # 127 个测试，含可复现的本地 HTTP 服务
├── skill/           # 配套 Codex skill（薄封装，见下）
├── docs/            # 架构 / API / Agent API / 测试说明
└── scripts/         # 联调冒烟 + 压测脚本
```

规模：49 个 Python 文件、约 7.7k 行（含测试与脚本）、UI 单文件 440 行。

---

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 分层、真实数据回路、状态机、并发与可靠性机制
- [docs/API.md](docs/API.md) —— 本地 REST 接口、WebSocket 事件、错误码
- [docs/AGENT_API.md](docs/AGENT_API.md) —— 15 个工具、自然语言层、如何用 LLM 驱动
- [docs/TESTING.md](docs/TESTING.md) —— 怎么跑测试、测试服务覆盖了哪些恶劣情况
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md) —— 实测速度：回环上限、外网、国内镜像、多连接收益

---

## 配套 Codex skill

`skill/downloadx/` 是这个项目的 Codex skill：让任意会话一句话就能用它下载，而不用先找到
引擎在哪。它**只做薄封装**，不含任何下载逻辑，永远指向引擎本体（避免两份实现漂移）。

```
skill/downloadx/
├── SKILL.md                  # 什么时候用、怎么用、哪些不变量不能破
├── scripts/dx.py             # 定位引擎 + 挑解释器 + 原样转发参数
├── scripts/fetch_video.py    # 视频/音频分流：yt-dlp / X skill / 引擎，按 URL 自动选
└── references/               # cli.md / agent-api.md / troubleshooting.md / video-sites.md
```

视频站点由 `fetch_video.py` 分流：YouTube、Bilibili 等交给 yt-dlp（B 站会自动补上躲开
HTTP 412 的四个请求头），X/Twitter 交给已有的 `download-x-video` skill，直链媒体文件交回
引擎（续传 + 队列 + 校验）。工具缺失时只报告、不自动安装。

需要登录或走代理的站点用**站点档案**记住一次即可：
`fetch_video.py --site-set bilibili.com --cookies <file>`，之后同域名的链接自动带上。

---

## 同步到「我的AI助手」中台

`scripts/install_to_assistant.py` 把下载能力装进本机的 AI 中台（默认 `D:\Projects\我的AI助手`）：

| 写什么 | 内容 |
| --- | --- |
| Skill 库 | 「下载工具 DownloadX」——SKILL.md 正文 + 视频站点分流那份 reference，中台里的模型据此知道怎么调用 |
| 应用广场 | 「下载器面板 DownloadX」——本地应用，端口 8720，一键启停 |

```powershell
python scripts\install_to_assistant.py --check    # 只报告现状
python scripts\install_to_assistant.py            # 同步技能 + 应用
python scripts\install_to_assistant.py --update   # 技能已存在时覆盖（技能改动后重跑）
python scripts\install_to_assistant.py --no-app   # 只同步技能
```

中台开了登录之后 API 需要密码；不给 `--password` 时脚本自动退回**直接写 `data/hub.db`**
（写前自动备份 `hub.db.bak-<时间戳>-downloadx`，并同步写 `data/skills/<id>.md` 镜像）。
两条路都实测过：应用行里的启动命令能真的拉起面板（`/api/health` 返回 `DownloadX 0.1.0`），
页面刷新即可见、不需要重启中台。

注意面板和命令行共用同一个引擎状态目录（`%LOCALAPPDATA%\DownloadX`），而引擎是单实例：
如果命令行正跑着前台下载，中台点「启动」不会成功（中台把子进程输出丢掉了，所以界面上
看不出原因）。这时要么先在命令行那边结束，要么让两边都连已启动的服务。

**这里（项目内）是源**，改完用一条命令同步到所有副本（Codex 加载目录 + 归档目录）：

```powershell
python scripts\sync_skill.py            # 同步到 ~\.codex\skills\downloadx 与 D:\Projects\skill\downloadx
python scripts\sync_skill.py --check    # 只报告差异，不写文件
python scripts\sync_skill.py --prune    # 顺带删除目标里多余的文件（精确镜像）
```

同步脚本按相对路径逐文件复制并校验 sha256 —— 直接 `Copy-Item -Recurse` 一个文件夹到
同名已存在目录会套出一层 `downloadx\downloadx\`（踩过，会让 skills 列表里出现两条同名 skill）。

三份副本的分工：

| 位置 | 角色 |
| --- | --- |
| `outputs/DownloadX/skill/downloadx/` | **源**，只改这里 |
| `~\.codex\skills\downloadx\` | Codex 实际加载的那份，重启后生效 |
| `D:\Projects\skill\downloadx\` | 归档 / 分发副本 |

用法（在任意会话里）：

```powershell
python "$env:USERPROFILE\.codex\skills\downloadx\scripts\dx.py" add <url> --wait --json
python "$env:USERPROFILE\.codex\skills\downloadx\scripts\dx.py" doctor   # 引擎/解释器解析诊断
```

引擎被挪走时用 `DOWNLOADX_HOME=<路径>` 指一次，不用改任何代码。

---

## 配置

优先级：**环境变量 > `config.json` > 内置默认值**。

```powershell
$env:DOWNLOADX_DOWNLOAD_DIR       = "D:\Downloads"
$env:DOWNLOADX_MAX_CONCURRENT_TASKS = "5"
$env:DOWNLOADX_GLOBAL_SPEED_LIMIT = "2097152"   # 2 MiB/s，0 表示不限速
$env:DOWNLOADX_VERIFY_SHA256      = "true"
$env:DOWNLOADX_RAMP_ENABLED       = "true"      # 并发自校准（大文件才触发）
python -m downloadx serve
```

并发自校准的相关参数（默认值即可，通常不用动）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `DOWNLOADX_RAMP_ENABLED` | true | 是否启用自校准 |
| `DOWNLOADX_RAMP_MIN_TOTAL_SIZE` | 25165824 | 小于 24 MB 的任务不做校准（不值得为测量花时间） |
| `DOWNLOADX_RAMP_WARMUP_SECONDS` | 2.0 | 单连接测量窗口（其中前 25% 用于预热，不计入速率） |
| `DOWNLOADX_RAMP_WARMUP_BYTES` | 4194304 | 单连接测量窗口的字节上限 |
| `DOWNLOADX_RAMP_PROBE_SECONDS` | 4.0 | 放开并发后的对比窗口 |
| `DOWNLOADX_RAMP_GAIN_THRESHOLD` | 1.15 | 放开后吞吐至少要是单连接的 1.15 倍，否则退回单连接 |

默认状态目录：`%LOCALAPPDATA%\DownloadX`（任务元数据、`history.jsonl`、单实例锁）。
下载文件不会放进状态目录。

---

## 已知边界

- 服务只监听本地回环，没有鉴权 —— 它是本地工具，不要往公网暴露。
- 单实例锁是"心跳 + 过期接管"，不依赖 Windows 专有 API，跨平台一致。
- 流式模式（服务端不给 `Content-Length`）无法断点续传，这是 HTTP 协议本身的限制。
- 视频站点只做**分流**（交给 yt-dlp / 已有的 X skill），引擎自己不做解析；本机没有的工具不会自动安装。
- BT / Magnet / 账号 / 云同步 / LLM 对话不在 v0.1 内，按 [docs/ROADMAP.md](docs/ROADMAP.md) 分阶段推进。

---

## 许可证

[MIT](LICENSE) © 2026 刃研社 (SCALPEL LAB)

下载行为与内容版权由使用者自行负责：本项目只提供工具，不绕过 DRM、付费墙、地域限制或账号限制。
