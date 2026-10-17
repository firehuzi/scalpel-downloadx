# 路线图：v0.2 → v1.0

## 原则

1. **按任务书 §1.2 的优先级**：可靠性 > 数据完整性 > 断点恢复 > 性能 > 协议数量 > AI 能力 > 视觉效果。
2. **每阶段独立可交付**：一个阶段做完就能用、能测、能回滚；不追求"功能数"。
3. **引擎永远不依赖 LLM**（任务书 §1.1）：AI 只做控制层，任何 AI 能力下线都不影响下载。
4. **不做**：网盘破解、DRM 绕过。这两项不是"下载能力"，是绕过别人的访问控制——不在路线内。

## 总览

| 阶段 | 内容 | 依赖 | 规模 | 状态 |
| --- | --- | --- | --- | --- |
| **P1** | 本地 API 鉴权 + 浏览器扩展 | 无 | 小 | ✅ 已完成 |
| **P2** | 媒体能力补全（视频解析深化） | yt-dlp / ffmpeg（已有） | 中 | 待做 |
| **P3** | BT / Magnet / DHT / P2P | `libtorrent 2.1.1`（cp312 win_amd64 wheel 已验证） | 大 | 待做 |
| **P4** | 账号 + 云同步 | 自建服务端 | 大 | 待定（与"本地优先"有张力） |
| **P5** | LLM 对话面板 | 现有 15 个 Agent 工具 + 可选 DeepSeek Key | 中 | 待做 |

---

## P1 — 本地 API 鉴权 + 浏览器扩展 ✅

**为什么先做这个**：扩展是下载器最经典的入口（在链接上右键就下），而它要调用本地 API，
就绕不开"跨域 + 鉴权"——正好把 API 目前"回环地址即权限"这个缺口一起补上。

**交付**：

- `api_token` 配置：留空 = 不鉴权（本地工具常态）；设置后 `/api/*`、`/agent/*` 必须带
  `Authorization: Bearer <token>`（或 `X-DownloadX-Token`），`/api/health` 免鉴权。
- 只有配置了令牌才放开 CORS（避免任意网页 drive-by 调用本地 API）；`OPTIONS` 预检返回 204。
- **WebSocket 单独校验**（升级请求不经过 HTTP 中间件）：`/api/ws?token=…`。
- `downloadx token` / `--rotate` / `--clear`。
- 扩展（MV3，`extension/`）：右键链接/页面 → 加入下载；弹窗看速度/队列、粘贴链接、配服务地址与令牌。

**验收**：无令牌 401 / 正确令牌 200 / 预检 204 + CORS / WS 无令牌被拒 / 扩展清单为合法 MV3。
（`tests/test_api.py` + `tests/test_cli.py`，7 个用例）

---

## P2 — 媒体能力补全

现状：已经能把 YouTube / B 站等交给 yt-dlp，X 交给 `download-x-video`，并内置了 B 站 412 的四个请求头。

**已完成：HLS 与 DASH 原生下载**（`network/hls.py`、`network/dash.py` + `_run_stream`）——
两条协议共用同一套"流轨"机制：按分辨率/码率选轨 → 分片并发下载（上限 8）→
**按分片断点续传**（已完成分片写进任务元数据，恢复时还会以磁盘实物为准）→ 按索引拼接 →
需要时用 ffmpeg `-c copy` 合并/重封装（不转码）。

* **HLS**：master 选变体、EXT-X-MAP init 段；加密流（EXT-X-KEY）、直播（无 ENDLIST）、
  EXT-X-BYTERANGE 明确拒绝。
* **DASH**：SegmentTemplate（`$Number$` / `$Time$` / `$RepresentationID$` / SegmentTimeline）、
  SegmentList、SegmentBase；视频+音频双轨分别下载后 mux；`ContentProtection`（DRM）拒绝。
  清单按"时长/分段时长"推算出的**尾部分片常常并不存在**（实测 ffmpeg 生成的 MPD 就是如此），
  尾段 404 按"流结束"处理，从计划里摘掉而不是让任务失败。

测试覆盖：解析（含 Timeline / SegmentList / DRM 拒绝）、拼接字节比对、暂停后续传不重下、
用 ffmpeg 现场生成真 HLS / 真 DASH 再合并，并用 ffprobe 验证音视频轨、容器确实是 mp4。

**已完成：播放列表批量**

* 引擎：`POST /api/tasks/batch` —— 每条链接一个独立任务（可单独暂停/重试/看进度），
  单条坏链接不影响其余（返回 `created` / `failed` 两份清单）。
* Skill：`fetch_video.py --playlist` 从"丢给 yt-dlp 一次下完"改成
  **先 flat 枚举条目 → 逐条执行 → 状态写进 `.downloadx-playlist.json`**。
  重跑同一条命令只补失败/新增条目；`--redo` 才全部重下；`--playlist --inspect` 只列条目。

**已完成：站点档案（cookie / 代理）**

* `fetch_video.py --site-set bilibili.com --cookies <file> --proxy <url> --site-args "…"`
  按域名保存档案（后缀匹配，最长优先），之后跑该站点的链接自动带上。
* 档案存在引擎状态目录的 `sites.json`，`--site-list` / `--site-remove` 管理；
  命令行 `--proxy` 覆盖档案里的设置。
* 解决的真实问题：B 站 1080P+ 必须登录（而 `--cookies-from-browser` 在这台机器上不可用），
  每次手打参数容易漏；X 和部分站点需要代理。

**已完成：站点适配回归（`check_sites.py`）**

* 几秒钟探一遍 YouTube / Bilibili / X / HLS / DASH：可用性、最高画质、格式数；
  B 站会顺带报告"有没有配 cookies"，X 需要一条真实推文（`--x-url`）才实测、否则标记跳过。
* 只做 simulate 探测，不下载；退出码 0/1 可直接接定时任务当告警。
* 实测（2026-10-17）：YouTube 2160p / 33 格式、B 站 480p（未配 cookies）、HLS、DASH 均可用，
  全程 7.4 秒。

**已完成：字幕 / 章节 / 弹幕（`--extras`）**

* 字幕：yt-dlp 手动 + 自动字幕转 srt，**单独跑一遍**——字幕失败（YouTube 429）不会把已下好的视频判失败。
* 章节：从 info JSON 提取，写 `<视频>.chapters.txt`（`HH:MM:SS 标题`）。
* 弹幕：B 站 `comment.bilibili.com/<cid>.xml`，raw deflate 解压后写 `<视频>.danmaku.xml`；
  非 B 站自动跳过。实测热门视频 600 条弹幕、63 KB。
* 实测（2026-10-17）：YouTube 短片拿到 2 个 srt + 3 条章节；B 站短片拿到 600 条弹幕。

**已完成：DASH 补充**

* **多 Period**：按顺序把各 Period 的分片拼到同一条轨上；不同 Period 的初始化段不同时明确报错
  （那种情况拼接会出错，交给 yt-dlp 更稳）。
* **`$Bandwidth$` 模板**：除 `$Number$` / `$Time$` / `$RepresentationID$` 外补齐。
* **dynamic（直播）MPD 拒绝**：之前会当成静态去下，现在直接给可读错误。

缺口（按价值排序）：

1. **弹幕格式转换**：目前是原始 XML（DanDanPlay 等播放器可直接吃）；可选转 ASS 内嵌。
2. **DRM 之外的加密方案**（如 SAMPLE-AES 之外的私有方案）：明确不在当前范围。

**验收**：三个站点（YouTube / B 站 / X）各跑通 1080P（或站点上限）+ 失败自动重试 + 结果哈希记录。

---

## P3 — BT / Magnet / DHT / P2P

**可行性已验证**：`libtorrent 2.1.1` 有 `cp312-win_amd64` wheel，`pip download` 实测可拉取。
所以这是一个"大阶段"，不是研究项目。

**spike 已完成（2026-10-17）→ 见 [BT_SPIKE.md](BT_SPIKE.md)**：8 MB 种子、本机做种+下载、
中途暂停后换新 session 续传。结论：

* 能装能跑（`pip install --target`，全局 site-packages 会被杀软拦）。
* 同 session `pause()/resume()` 无损；**跨 session 只恢复 hash 已验证的分片**（实测 261 块 → 129 块）。
* **必须 `save_resume_data(flush_disk_cache)`**：不加会产出损坏文件（实测校验不通过）。
* 因此集成形态应是"独立子进程（GPL 边界）+ 不承诺字节级续传"。

设计要点：

- 新 `mode = "torrent"`，由 `TorrentWorker` 驱动（与 `DownloadWorker` 平级）。
- 复用已有：状态机（queued/downloading/paused/completed/failed）、优先级队列、全局限速、
  事件总线、历史记录、鉴权 API。
- BT 没有 Range/分片，但有 **piece 校验**——正好对应"数据完整性"这一优先级：piece 哈希即校验。
- 语义差异要想清楚：
  - "完成"= 所有 piece 校验通过（可选继续做种）；
  - "暂停"= 保存 resume data（`.fastresume`），恢复时校验未完成 piece；
  - 上传/做种要能关（默认下载完即停，符合"下载器"定位）；
  - 磁盘预分配策略（sparse vs full）要可配。
- 依赖风险：libtorrent 体积（~10MB wheel）、它自己的线程模型、GPL 许可（本项目 MIT，
  动态链接/独立进程调用需确认合规——最稳是**独立进程 + 本地接口**，避免许可传染）。

**验收**：磁力链与 `.torrent` 各跑通；暂停/恢复不丢进度；piece 校验失败能定位并重下；限速生效。

---

## P4 — 账号 + 云同步（待定）

这两项与"本地优先"天然有张力，所以要明确边界：

- **账号**：只解决"多设备/远程访问时的身份"（Bearer 令牌 → 多用户、每用户独立状态目录），
  不做社交、不做订阅。
- **云同步**：只同步**任务元数据**（不传文件），且作为可选旁路；本地状态仍是唯一事实来源，
  断网时完全可用。
- 依赖：一个最小服务端（Postgres + 对象存储可选）。若不愿意维护服务端，这一阶段可以长期搁置。

**验收**：两台设备看到同一批任务状态（不含文件）；服务端不可用时本地不受影响。

---

## P5 — LLM 对话面板

现状：`/agent/tool` 已经有 15 个确定性工具，`/agent/nl` 有规则解析 + 可选 LLM。

缺口：UI 里没有"对话"入口。做法是在面板加一个聊天框 → 调 `/agent/nl`（planner=auto）
→ 展示工具调用与结果。要求：

- 引擎与工具层不依赖模型（已有约束）；
- 无 Key 时自动用规则解析，功能不缩水到不可用；
- 每次调用显示"调了哪个工具、参数是什么"，避免"AI 黑箱"。

**验收**：无 Key 时自然语言能完成"下载/暂停/查状态"；有 Key 时能处理更模糊的表述；两者都不影响手动操作。

---

## 明确排除

| 项 | 原因 |
| --- | --- |
| 网盘破解 | 绕过网盘限速/会员机制，属于规避访问控制 |
| DRM 绕过 | 同上 |
| 浏览器插件（上架商店） | P1 交付的是开发者模式扩展；上架需要账号与审核，与功能无关，按需再说 |
