# 视频站点下载：工具选择与踩过的坑

## 先看这张表

| URL 类型 | 用什么 | 关键点 |
| --- | --- | --- |
| YouTube / youtu.be | yt-dlp | 高画质音视频分离，必须有 ffmpeg 合并 |
| Bilibili / b23.tv | yt-dlp | **必须同时带四个请求头**，否则一律 412 |
| X / Twitter 状态链接 | `download-x-video` skill 的 `x_video.py` | 免 cookie、免账号、带自更新；yt-dlp 兜底 |
| 其它 yt-dlp 站点（AcFun / 抖音 / Vimeo / Twitch / niconico…） | yt-dlp | 视站点可能需要 cookie |
| 直链 `.mp4/.mkv/.zip/.iso/...` | **DownloadX 引擎** | 续传、队列、限速、SHA-256 是它的主场 |
| `.m3u8` / `.mpd` 流清单 | yt-dlp | 引擎不解析流清单，别硬塞给它 |

统一入口（自动分流，额外参数写在 `--` 之后原样交给 yt-dlp）：

```bash
python <skill-dir>/scripts/fetch_video.py <url> [--quality 1080] [--audio] [--inspect] [--json]
```

## Bilibili：HTTP 412 的解法（实测）

四个头**必须一起给**，少任何一个都过不去：

```
--add-header Referer:https://www.bilibili.com
--add-header Origin:https://www.bilibili.com
--add-header Accept-Language:zh-CN,zh;q=0.9
--user-agent <桌面版 Chrome UA>
```

| 尝试 | 结果 |
| --- | --- |
| 默认 | HTTP 412 Precondition Failed |
| 只加 Referer | 412 |
| Referer + 桌面 UA | 412 |
| 四个头齐全 | ✅ 列出 9 种格式 |

清晰度上限：**未登录只有 480P**（av01/hvc1/avc1 三种编码各一档，外加 360P）。
1080P 需要登录 cookie，4K/8K 需要大会员。

`--cookies-from-browser edge|chrome` 在**这台机器上不可用**：报
`Could not copy Chrome cookie database`（Chrome/Edge 的 app-bound 加密，yt-dlp issue #7271）。
要用高清晰度只能导出 `cookies.txt`，然后**一次性记进站点档案**：

```bash
python fetch_video.py --site-set bilibili.com --cookies C:/path/cookies.txt
python fetch_video.py <B站链接>          # 之后自动带上 cookie
```

站点档案按域名后缀匹配（`api.bilibili.com` 优先于 `bilibili.com`），
存在引擎状态目录的 `sites.json` 里；`--site-list` 查看、`--site-remove` 删除，
还可以用 `--site-args "..."` 给某个站点追加 yt-dlp 参数、`--proxy` 指定代理。

想要 4K/8K/杜比/弹幕/字幕，专业工具是 **BBDown**（未安装，见下方"可选升级"）。

## YouTube

本机实测**可以直连**（HTTP 200，4K60 都能解析），不需要代理。

- 720P 以上一定是音视频分离的两条流 → 没有 ffmpeg 就合不出成品；
- 年龄限制、会员、私有视频需要 `--cookies`；
- 播放列表/合集：`fetch_video.py` 默认 `--no-playlist`（避免手滑下一整列）。要整列加 `--playlist`：
  它先 flat 枚举条目，再**逐条**下载，状态写进输出目录的 `.downloadx-playlist.json`；
  重跑同一条命令只补失败/新增条目，`--redo` 才全部重下。`--playlist --inspect` 只列条目不下载。
- 纯音频：`--audio` 走 `-f ba/b -x --audio-format m4a`，不重新编码。

## X / Twitter

交给 `download-x-video` skill：它用 FxTwitter 公共元数据 + `video.twimg.com` CDN，
免 cookie 且按像素面积→码率选最高画质，还带自更新。**不要在 downloadx 里重复实现一遍**，
两条路径会给出不同结果、互相打架。`fetch_video.py` 会在三个 skills 根目录里找它的脚本，
找不到才退回 yt-dlp。

## 什么时候让引擎下载、什么时候让 yt-dlp 下载

用 **DownloadX 引擎**：

- 你手里已经有直链（尤其大文件）：续传（跨进程崩溃）、优先级队列、限速、SHA-256 校验；
- yt-dlp 自己下载反复中断：先 `-g` 拿到直链，再 `dx.py add <直链> --wait` 交给引擎续传；
- 需要和别的下载共享队列/历史/带宽时。

用 **yt-dlp**：

- HLS/DASH、需要签名或 cookie 的站点，引擎做不了；
- 需要字幕、弹幕、章节、缩略图、只取音频、批量播放列表。

## 可选升级（**不要自动安装**，装前问用户）

| 工具 | 什么时候值得装 |
| --- | --- |
| aria2c | 让 yt-dlp 用 `--downloader aria2c` 多连接拉 CDN 直链（注意：会踩"按连接限速"的坑，见 `PERFORMANCE.md`） |
| BBDown | B 站 4K/8K/杜比/弹幕/字幕，专业得多 |
| gallery-dl | 图集/图片站（Pixiv、Twitter 图集等） |
| lux | Go 写的备用下载器，跨平台单文件 |

本 skill 的纪律：缺什么就报什么，绝不静默 `pip install` / `winget install`。

## 边界

只下载公开、且用户有权限的内容。不绕过 DRM、付费墙、地域限制或账号限制。
平台条款与版权由使用者自己负责。

## 环境事实（2026-10-15 实测）

| 项 | 状态 |
| --- | --- |
| yt-dlp | 2026.06.09，装在 `C:\Program Files\Python312`，用 `python -m yt_dlp` 调用 |
| `yt-dlp.exe` 在 PATH | **没有**（所以别写 `yt-dlp ...`） |
| ffmpeg / ffprobe | `C:\ffmpeg\ffmpeg-master-latest-win64-gpl-shared\bin` |
| aria2c / you-get / lux / BBDown / gallery-dl | 都没有 |
| YouTube 直连 | 通（4K 可解析） |
| Bilibili | 通，但必须四个头 |
| x.com | 时通时不通（先 000 后 200）；实际下载走 CDN + FxTwitter，不受影响 |
| 多解释器 | 系统 3.12 装了 yt-dlp，另有 3.14 与 hermes venv；`fetch_video.py` 会自动挑对的那个 |
