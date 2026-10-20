# dsh-downloadx

把本机「刃研社-DownloadX」下载引擎接进 DeepSeek Harness 的插件：模型可以像调用其他工具一样
新建下载、查进度、暂停/继续/取消、限速、校验 SHA-256；下载本身在独立的本地引擎里跑，
不经过模型，DSH 退出后也继续。

> 本插件只是一层确定性的桥：**模型是控制层，DownloadX 引擎才是下载引擎。**

## 它做什么

- 把 DownloadX 的 Agent API（`/agent/tool`，15 个工具）压缩注册成 6 个 DSH 原生工具；
- 在 DSH 侧边栏加一个「下载器」面板：直接 iframe 嵌入 DownloadX 自带 Web UI（引擎没启动时显示
  「启动 DownloadX 引擎」按钮，点一下由插件拉起引擎再加载 UI）；
- 按需自动拉起 `python -m downloadx serve`（detached，DSH 退出后下载继续），下次启动复用同一服务；
- 健康检查、端口被占用/引擎启动失败时给出可执行的排查提示；
- 注册两个只读/引导用 HTTP 路由：`GET /api/downloadx/info`（面板查状态）、
  `POST /api/downloadx/open`（面板启动引擎），以及 `GET /api/downloadx/health`（诊断）。

## 前置条件

1. **Python 3.10+**（`python --version` 可用）；
2. **downloadx 已安装**：在 [scalpel-downloadx](https://github.com/firehuzi/scalpel-downloadx) 仓库里
   `pip install -e .`，或本仓库的发布包；验证：`python -m downloadx --version`；
3. 视频下载另需 `yt-dlp` 与 `ffmpeg`（720P 以上必须 ffmpeg 合并音视频）；缺失时工具会如实报错，
   不会静默安装。

> 插件不假设 `python` 在 DSH 宿主进程的 PATH 里（桌面应用实测会出现 `spawn python ENOENT`）：
> 它会自动发现 `%APPDATA%\Python\Python3xx\Scripts\downloadx.exe`、常见 Python 安装目录等启动方式，
> 逐个用 `--version` 验证；也可以在配置里写死 `python` 解释器路径。

## 安装

### 方式 A：DSH 侧边栏（推荐，桌面端）

1. 打开 DSH 侧边栏 → **Plugins** → 安装本地插件包（Install from file / 填 tgz 路径）；
2. 填这个绝对路径（或把 tgz 拷到任意位置）：
   `D:\Projects\bot\downloadx-dsh\dist\dsh-downloadx-0.1.0.tgz`；
3. 安装后 DSH 会热重组（HMR）并加载插件；若没看到「下载器」入口，刷新页面或重启应用。
4. 验证：新会话里问「用 downloadx_engine 看看引擎状态」，或直接点侧边栏「下载器」。

### 方式 B：命令行（需要先退出 DSH 桌面应用）

使用 DSH 自带 CLI（不是 PATH 上那个旧版全局 CLI）：

```powershell
$cli = "C:\Users\xheih\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"

# web profile（或用 --profile desktop 装进桌面端 profile）
& $cli plugin --profile web add "D:\path\to\dsh-downloadx-0.1.0.tgz"

# 卸载
& $cli plugin --profile web remove dsh-downloadx
```

桌面端 profile 的包操作由桌面应用控制：完全退出应用后再用 CLI 安装，或直接用侧边栏 Plugins 页面。

## 工具

| 工具 | 用途 |
|---|---|
| `downloadx_add` | 新建 HTTP/HTTPS（含 HLS/DASH）下载任务：url、filename、save_dir（绝对路径）、priority、connections、speed_limit、sha256、start。视频网站页面不要用本工具（引擎不路由 yt-dlp，会存成网页） |
| `downloadx_list` | 按状态列出任务（进度/速度/文件名），回答“下载到哪了” |
| `downloadx_get` | 单个任务详情：进度、连接、可续传、保存路径、SHA-256 校验、错误；task_id 支持唯一前缀 |
| `downloadx_control` | pause / resume / cancel / retry / remove / set_priority / set_speed_limit / pause_all / resume_all；task_id 支持唯一前缀 |
| `downloadx_probe` | 只探测 URL（大小、是否支持断点续传、文件名），不下载 |
| `downloadx_engine` | status / start / stop / stats / history；一般无需手动调用（其他工具会自动拉起引擎）。只会结束本插件实例启动的引擎 |

工具结果都是紧凑的中文文本，任务 ID 取自引擎，可直接回填给 `downloadx_get` / `downloadx_control`。

## 侧边栏「下载器」面板

插件同时是一个 DSH 客户端插件：侧边栏会出现「下载器」入口，面板内嵌 DownloadX 的 Web UI
（任务列表、进度条、暂停/继续、限速设置等）。行为：

- 打开面板时询问宿主 `/api/downloadx/info`：引擎在跑就直接加载 UI；没在跑显示启动按钮；
- 「启动 DownloadX 引擎」→ `POST /api/downloadx/open`（由宿主插件拉起引擎，最长等 30s）；
- 「刷新」重新检查状态并重载 iframe；「在浏览器打开」把 UI 提到独立浏览器窗口；
- 「地址」可把面板指向别的 DownloadX 端口（写进 localStorage `dsh-downloadx.ui-url`，留空恢复默认 8720）；
- 在没有宿主 HTTP 路由的环境（如部分 Electron 壳以 file:// 加载界面）会自动退化为直接 iframe 默认地址。

> 面板只是 DownloadX 原生 Web UI 的容器，不重复实现界面；下载/队列/校验的权威状态始终在引擎里。

## 配置

默认零配置即可用。需要覆盖时在 profile 的 `cordis.patch.yml` 里按行 id `downloadx` 改 config：

```yaml
- id: downloadx
  name: dsh-downloadx
  config:
    baseUrl: http://127.0.0.1:8720   # 引擎地址（改端口也改这里）
    autoStart: true                  # 引擎未运行时是否自动拉起
    python: C:\Program Files\Python312\python.exe   # 可选；缺省自动发现（pip --user 的 downloadx.exe / 常见安装目录）
    stateDir: D:\DownloadX\state     # 可选：透传给 downloadx serve --state-dir
    downloadDir: D:\Downloads        # 可选：透传给 downloadx serve --download-dir
    token: ""                        # 可选：引擎配置了 api_token 时填访问令牌
    startTimeoutMs: 30000            # 自动拉起的最长等待
    requestTimeoutMs: 15000          # 单次工具调用超时
```

也支持环境变量：`DOWNLOADX_BASE_URL`、`DOWNLOADX_PYTHON`、`DOWNLOADX_TOKEN`。

## 排查

| 现象 | 处理 |
|---|---|
| `找不到可用的 DownloadX 启动方式` | 工具结果会列出尝试过的候选与各自报错。装依赖 `pip install -e D:\Projects\scalpel-downloadx`，或在配置里写死 `python: C:\Program Files\Python312\python.exe` |
| `无法启动 DownloadX 引擎`（工具结果里带引擎日志尾部） | 按提示先手动运行 `python -m downloadx serve --port 8720` 看真实报错；常见原因是 `downloadx` 未安装、端口被占用 |
| 端口被其它程序占用 | 换 `baseUrl` 端口，或结束占用进程 |
| `连不上 DownloadX 引擎` | 引擎中途退出；插件会自动重启一次，仍失败则看引擎日志 `%TEMP%\dsh-downloadx-engine-8720.log` |
| 已有服务在运行（pid=…） | 引擎状态目录是单写者；插件会等待/清理已崩溃实例的残留锁，不要手动起第二个实例 |
| 下载完成但文件是 `.dxpart` | 未完成/未通过校验的数据永远不会被当成最终文件；查任务 `error`/`verified` |

插件不主动结束不是它启动的引擎进程（避免误杀）；需要停服务请到终端 `Ctrl+C` 或任务管理器结束对应的 python 进程。

## 安全

- 引擎默认只监听 `127.0.0.1`，无鉴权；本插件同样只连回环地址；
- 诊断路由（`/api/downloadx/health`）只读、无副作用，不返回令牌；
- 下载目录写入权限 = 当前用户权限：不要让它下载到系统目录，也不要使用来源不明的 URL。
- 下载器明确不做网盘破解 / DRM 绕过。

## 开发

```powershell
node --check lib/index.js
node ..\test\unit.mjs         # 离线单测（不需要 DSH / 引擎）
npm pack                       # 生成 tgz
```

插件刻意**不导入任何 `@deepseek-ai/*` 包**：第三方插件在 profile 里的官方包解析不可靠，只用 Node 内置模块
（`child_process` / `fs` / `os` / `path` / 全局 `fetch`）。工具定义是注册表可接受的纯对象
（`parameters` = 原始 JSON Schema，`output = { schema, render }`，`execute(args, exec)`）。

## License

MIT
