#!/usr/bin/env python3
"""视频站点下载的确定性入口：按站点分流，套用踩过坑的参数，输出可校验的结果。

它不做解析算法——解析交给 yt-dlp / 现有 skill；它解决的是"每次都要记得那一堆参数"：

* B 站必须同时带 Referer + Origin + Accept-Language + 桌面 UA，否则一律 HTTP 412；
* 默认只下单个视频（`--no-playlist`），避免误触发几百集的合集；
* X/Twitter 交给已有的 download-x-video skill（免 cookie、带自更新）；
* 直接的媒体直链（.mp4 等）交给 DownloadX 引擎（可续传、可排队、带校验）。

用法::

    python fetch_video.py <url> [--dir D] [--quality 1080] [--audio] [--inspect] [--json]
    python fetch_video.py <url> --playlist            # 明确要整个合集
    python fetch_video.py <url> -- --write-subs       # `--` 之后的参数原样交给 yt-dlp
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DIRECT_MEDIA_SUFFIX = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".mp3", ".m4a",
                       ".flac", ".wav", ".zip", ".7z", ".rar", ".iso", ".pdf")
VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv", "acfun.cn",
    "douyin.com", "vimeo.com", "dailymotion.com", "twitch.tv", "nicovideo.jp",
)

SKILL_SIBLINGS = (Path(__file__).resolve().parents[1],)  # 本 skill 根
X_SKILL_CANDIDATES = (
    Path.home() / ".codex" / "skills" / "download-x-video" / "scripts" / "x_video.py",
    Path.home() / ".claude" / "skills" / "download-x-video" / "scripts" / "x_video.py",
    Path.home() / ".agents" / "skills" / "download-x-video" / "scripts" / "x_video.py",
)


# ----------------------------------------------------------------------
# 站点分流
# ----------------------------------------------------------------------
def host_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def is_x(url: str) -> bool:
    return host_of(url).endswith(("x.com", "twitter.com"))


def is_direct_media(url: str) -> bool:
    if is_x(url):
        return False
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith((".m3u8", ".mpd")):
        return False  # 流清单要交给 yt-dlp 解析
    return path.endswith(DIRECT_MEDIA_SUFFIX)


def find_x_video_script() -> Path | None:
    for candidate in X_SKILL_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def bilibili_args(cookies: str | None) -> list[str]:
    """B 站风控（HTTP 412）的解法：四个头必须一起给。

    实测：默认 412；只加 Referer 仍 412；Referer + 桌面 UA 仍 412；
    Referer + Origin + Accept-Language + 桌面 UA 通过。
    """
    args = [
        "--add-header", "Referer:https://www.bilibili.com",
        "--add-header", "Origin:https://www.bilibili.com",
        "--add-header", "Accept-Language:zh-CN,zh;q=0.9",
        "--user-agent", UA_CHROME,
    ]
    if cookies:
        args += ["--cookies", cookies]
    return args


def quality_selector(quality: str | None, audio_only: bool) -> str | None:
    if audio_only:
        return "ba/b"
    if not quality or quality == "best":
        return "bv*+ba/b"
    if quality == "worst":
        return "wv*+wa/w"
    if quality.isdigit():
        height = int(quality)
        return f"bv*[height<={height}]+ba/b[height<={height}]/b"
    raise SystemExit(f"无法理解的 --quality：{quality}（用 best / worst / 1080 这样的高度）")


# ----------------------------------------------------------------------
# 工具可用性
# ----------------------------------------------------------------------
def pick_python() -> tuple[str, str]:
    """挑一个真的装了 yt-dlp 的解释器。

    这台机器上有多个 Python（系统 3.12、她的 venv、WindowsApps 存根…），
    而 yt-dlp 只装在其中一个里，所以不能想当然用 sys.executable。
    """
    candidates = [sys.executable]
    for extra in (r"C:\Program Files\Python312\python.exe", shutil.which("python"),
                  shutil.which("python3")):
        if extra and extra not in candidates:
            candidates.append(extra)
    last_error = ""
    for python in candidates:
        try:
            result = subprocess.run([python, "-m", "yt_dlp", "--version"],
                                    capture_output=True, timeout=90)
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = f"{python}: {type(exc).__name__}"
            continue
        if result.returncode == 0:
            return python, result.stdout.decode("utf-8", "replace").strip()
        last_error = f"{python}: " + (result.stderr or b"").decode("utf-8", "replace").strip()[-80:]
    raise SystemExit(
        "找不到装了 yt-dlp 的 Python。请自己决定是否安装：`python -m pip install -U yt-dlp`"
        f"（本 skill 不会自动安装）。最后一次错误：{last_error}"
    )


def have_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def dx_wrapper() -> Path:
    return Path(__file__).resolve().parent / "dx.py"


def default_download_dir() -> Path:
    """与 DownloadX 引擎的下载目录保持一致，读不到就退回当前目录。"""
    try:
        result = subprocess.run([sys.executable, str(dx_wrapper()), "config", "--json"],
                                capture_output=True, timeout=120)
        if result.returncode == 0:
            data = json.loads(result.stdout.decode("utf-8", "replace"))
            if data.get("download_dir"):
                return Path(data["download_dir"])
    except Exception:
        pass
    return Path.cwd()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ----------------------------------------------------------------------
# 附加产物：字幕 / 章节 / B 站弹幕
# ----------------------------------------------------------------------
def chapters_text(info: dict) -> str:
    """把 yt-dlp info JSON 里的 chapters 变成 ``MM:SS  标题`` 的文本。"""
    lines: list[str] = []
    for chapter in info.get("chapters") or []:
        try:
            start = int(float(chapter.get("start_time") or 0))
        except (TypeError, ValueError):
            start = 0
        title = str(chapter.get("title") or "").strip()
        lines.append(f"{start // 3600:02d}:{start % 3600 // 60:02d}:{start % 60:02d}  {title}".rstrip())
    return "\n".join(lines)


def collect_subs(produced: Path) -> list[str]:
    """收集与视频同名的字幕文件。

    不能用 glob：yt-dlp 的文件名带 ``[id]``，glob 会把方括号当字符类，
    明明下好了却匹配不到。按前缀过滤才可靠。
    """
    return sorted(
        str(path)
        for path in produced.parent.iterdir()
        if path.suffix == ".srt" and path.name.startswith(produced.stem)
    )


def decompress_danmaku(data: bytes) -> bytes:
    """B 站弹幕接口返回 raw deflate（无 Content-Length 依赖），依次尝试三种封装。"""
    if data[:5] == b"<?xml":
        return data
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS, 16 + zlib.MAX_WBITS):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    raise ValueError("弹幕数据无法解压（既不是明文也不是 deflate/gzip）")


def bilibili_bvid(url: str) -> str | None:
    match = re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    return match.group(1) if match else None


def _bili_get(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": UA_CHROME,
        "Referer": "https://www.bilibili.com",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def fetch_danmaku(url: str, timeout: float = 20.0) -> bytes | None:
    """按 URL 里的 BV 号取第一 P 的弹幕 XML；非 B 站链接返回 None。"""
    bvid = bilibili_bvid(url)
    if not bvid:
        return None
    pages = json.loads(_bili_get(
        f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}&jsonp=0", timeout
    ))
    entries = (pages or {}).get("data") or []
    if not entries:
        raise ValueError(f"取不到分 P 信息：{bvid}")
    cid = entries[0]["cid"]
    return decompress_danmaku(_bili_get(f"https://comment.bilibili.com/{cid}.xml", timeout))


# ----------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------
def run_ytdlp(python: str, url: str, args: list[str]) -> int:
    return subprocess.run([python, "-m", "yt_dlp", *args, url]).returncode


def delegate_to_x_skill(script: Path, url: str, args: argparse.Namespace) -> int:
    command = [sys.executable, str(script), "inspect" if args.inspect else "download", url]
    if not args.inspect:
        command += ["--output-dir", str(args.dir)]
    if args.json:
        command.append("--json")
    print(f"→ 交给 download-x-video skill：{script}")
    return subprocess.run(command).returncode


def delegate_to_engine(url: str, args: argparse.Namespace) -> int:
    command = [sys.executable, str(dx_wrapper()), "add", url,
               "--dir", str(args.dir), "--wait"]
    if args.json:
        command.append("--json")
    if args.name:
        command += ["--name", args.name]
    print(f"→ 交给 DownloadX 引擎（可续传 / 有校验）：{url}")
    return subprocess.run(command).returncode


# ----------------------------------------------------------------------
# 播放列表：逐条执行 + 状态文件（重跑即续跑）
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 站点档案：按域名记住 cookie 文件 / 代理 / 额外参数
# ----------------------------------------------------------------------
def sites_path() -> Path:
    """站点档案与引擎共用同一个状态目录（换机器时会一起迁移）。"""
    override = os.environ.get("DOWNLOADX_STATE_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) / "DownloadX" if local else Path.home() / ".local" / "share" / "DownloadX"
    return base / "sites.json"


def load_sites() -> dict[str, dict]:
    path = sites_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {str(k).lower(): v for k, v in (data or {}).items() if isinstance(v, dict)}


def save_sites(sites: dict[str, dict]) -> Path:
    path = sites_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sites, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def match_site(host: str, sites: dict[str, dict]) -> tuple[str, dict] | None:
    """域名后缀匹配，取最长的那条（api.bilibili.com 优先于 bilibili.com）。"""
    host = (host or "").lower()
    best: tuple[str, dict] | None = None
    for pattern, profile in sites.items():
        if host == pattern or host.endswith("." + pattern):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, profile)
    return best


def site_profile(host: str) -> tuple[str, dict] | None:
    return match_site(host, load_sites())


def site_command(args) -> int | None:
    """处理 --site-list / --site-set / --site-remove；不是管理命令时返回 None。"""
    if getattr(args, "site_list", False):
        sites = load_sites()
        if not sites:
            print("（还没有站点档案）")
            return 0
        for pattern in sorted(sites):
            profile = sites[pattern]
            print(f"{pattern:<28} cookies={profile.get('cookies') or '-'}  "
                  f"proxy={profile.get('proxy') or '-'}  args={profile.get('args') or '-'}")
        print(f"\n档案文件：{sites_path()}")
        return 0

    if getattr(args, "site_remove", None):
        pattern = args.site_remove.strip().lower()
        sites = load_sites()
        if sites.pop(pattern, None) is None:
            print(f"没有这个站点：{pattern}", file=sys.stderr)
            return 1
        save_sites(sites)
        print(f"已删除站点档案：{pattern}")
        return 0

    if getattr(args, "site_set", None):
        pattern = args.site_set.strip().lower()
        sites = load_sites()
        profile = dict(sites.get(pattern) or {})
        if args.cookies:
            profile["cookies"] = str(Path(args.cookies).expanduser())
        if args.proxy:
            profile["proxy"] = args.proxy
        if args.site_args:
            profile["args"] = args.site_args.split()
        if not profile:
            print("至少要给 --cookies / --proxy / --site-args 之一", file=sys.stderr)
            return 1
        sites[pattern] = profile
        path = save_sites(sites)
        print(f"已保存站点档案：{pattern} -> {profile}\n档案文件：{path}")
        return 0
    return None


def enumerate_playlist(python: str, url: str, extra: list[str], passthrough: list[str]) -> list[dict]:
    """用 yt-dlp 的 flat 模式只列条目，不下载。"""
    command = [python, "-m", "yt_dlp", "--flat-playlist", "-J", "--no-warnings",
               *extra, *passthrough, url]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        sys.stderr.write((result.stderr or "")[-1500:])
        return []
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return []
    entries: list[dict] = []
    for item in data.get("entries") or []:
        target = str(item.get("webpage_url") or item.get("url") or "")
        if not target.startswith("http"):
            continue
        entries.append({
            "id": str(item.get("id") or target),
            "title": str(item.get("title") or ""),
            "url": target,
        })
    return entries


def load_state(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    state["updated"] = int(time.time())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run_playlist(args, python: str, extra: list[str], passthrough: list[str],
                 out_dir: Path, version: str, host: str) -> int:
    """逐条执行；状态存 .downloadx-playlist.json，重跑只补失败/新增条目。"""
    print(f"→ yt-dlp {version}  站点={host or '未知'}  播放列表模式（逐条执行）")
    entries = enumerate_playlist(python, args.url, extra, passthrough)
    if not entries:
        print("! 没有解析到任何条目（是不是把单个视频当成了播放列表？）", file=sys.stderr)
        return 1

    if args.inspect:
        for index, entry in enumerate(entries, 1):
            print(f"{index:>3}. {entry['id']:<18} {entry['title'][:70]}")
        return 0

    state_path = out_dir / ".downloadx-playlist.json"
    state = load_state(state_path)
    state.setdefault("source", args.url)
    records: dict[str, dict] = state.setdefault("entries", {})

    summary = {"source": args.url, "total": len(entries), "done": 0, "failed": 0,
               "skipped": 0, "entries": []}
    for index, entry in enumerate(entries, 1):
        key = entry["id"]
        record = records.get(key) or {}
        if record.get("status") == "done" and not args.redo:
            summary["skipped"] += 1
            continue

        child = [sys.executable, str(Path(__file__).resolve()), entry["url"],
                 "--dir", str(out_dir), "--quality", args.quality]
        if args.audio:
            child.append("--audio")
        if args.cookies:
            child += ["--cookies", args.cookies]
        if args.cookies_from_browser:
            child += ["--cookies-from-browser", args.cookies_from_browser]
        if getattr(args, "subs", False):
            child.append("--subs")
        if getattr(args, "chapters", False):
            child.append("--chapters")
        if getattr(args, "danmaku", False):
            child.append("--danmaku")
        print(f"[{index}/{len(entries)}] {entry['title'][:60] or entry['url']}")
        code = subprocess.run(child).returncode
        status = "done" if code == 0 else "failed"
        records[key] = {"title": entry["title"], "status": status}
        summary["entries"].append({"id": key, "title": entry["title"], "status": status})
        summary["done" if code == 0 else "failed"] += 1
        save_state(state_path, state)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"\n播放列表完成：成功 {summary['done']}，失败 {summary['failed']}，"
              f"跳过（已完成）{summary['skipped']}")
        failed = [item for item in summary["entries"] if item["status"] == "failed"]
        if failed:
            print("失败条目（重跑同一条命令即可只补这些）：")
            for item in failed:
                print(f"  - {item['title'][:60] or item['id']}")
        print(f"状态文件：{state_path}")
    return 0 if summary["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="按站点分流下载视频/音频/直链")
    parser.add_argument("url", nargs="?", help="视频 / 播放列表 / 直链 URL（站点档案命令可省略）")
    parser.add_argument("--dir", default=None, help="保存目录，默认用 DownloadX 的下载目录")
    parser.add_argument("--name", default=None, help="另存文件名（仅直链下载有效）")
    parser.add_argument("--quality", default="best",
                        help="best（默认）/ worst / 具体高度如 1080、720")
    parser.add_argument("--audio", action="store_true", help="只要音频（m4a）")
    parser.add_argument("--playlist", action="store_true", help="允许下载整个合集/播放列表")
    parser.add_argument("--redo", action="store_true",
                        help="播放列表模式下重下已完成的条目（默认只补失败/新增）")
    parser.add_argument("--subs", action="store_true", help="同时下载字幕并转成 srt")
    parser.add_argument("--chapters", action="store_true",
                        help="导出章节到 <视频>.chapters.txt")
    parser.add_argument("--danmaku", action="store_true",
                        help="下载 B 站弹幕到 <视频>.danmaku.xml（其它站点自动跳过）")
    parser.add_argument("--extras", action="store_true",
                        help="= --subs --chapters --danmaku（非 B 站自动跳过弹幕）")
    parser.add_argument("--inspect", action="store_true", help="只列格式，不下载")
    parser.add_argument("--cookies", default=None, help="cookies.txt 路径（B 站 1080P+ 需要）")
    parser.add_argument("--cookies-from-browser", default=None,
                        help="从浏览器读 cookie（本机 Edge/Chrome 会因 app-bound 加密失败）")
    parser.add_argument("--proxy", default=None,
                        help="代理，如 http://127.0.0.1:7890（覆盖站点档案里的设置）")
    parser.add_argument("--site-list", action="store_true", help="列出已保存的站点档案")
    parser.add_argument("--site-set", metavar="HOST",
                        help="为某个域名保存档案，如 bilibili.com")
    parser.add_argument("--site-remove", metavar="HOST", help="删除某个域名的档案")
    parser.add_argument("--site-args", default=None,
                        help="给该站点追加的 yt-dlp 参数（空格分隔），配合 --site-set 使用")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args, passthrough = parser.parse_known_args()
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    elif passthrough:
        parser.error(f"无法识别的参数：{passthrough}（额外参数请写在 `--` 之后）")

    handled = site_command(args)
    if handled is not None:
        return handled
    if not args.url:
        parser.error("缺少 URL（--site-list / --site-set / --site-remove 除外）")
    if args.extras:
        args.subs = args.chapters = True
        args.danmaku = True

    if args.dir:
        out_dir = Path(args.dir).expanduser()
    else:
        out_dir = default_download_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    args.dir = out_dir

    # 1) X/Twitter：交给专门的 skill（免 cookie，带自更新）
    if is_x(args.url):
        script = find_x_video_script()
        if script is not None:
            return delegate_to_x_skill(script, args.url, args)
        print("! 没找到 download-x-video skill，改用 yt-dlp 兜底（X 可能需要 cookie）",
              file=sys.stderr)

    # 2) 直接媒体直链：交给引擎（续传 / 队列 / 校验是它的主场）
    if is_direct_media(args.url):
        return delegate_to_engine(args.url, args)

    # 3) 其余交 yt-dlp
    python, version = pick_python()
    ffmpeg = have_ffmpeg()
    if ffmpeg is None and not args.inspect:
        print("! 找不到 ffmpeg：高画质通常是音视频分离的，缺它无法合并", file=sys.stderr)

    host = host_of(args.url)
    matched = site_profile(host)
    proxy = args.proxy or (matched[1].get("proxy") if matched else None)
    if matched:
        pattern, profile = matched
        cookie_note = f"，cookies={Path(profile['cookies']).name}" if profile.get("cookies") else ""
        print(f"→ 命中站点档案 {pattern}{cookie_note}")
        if not args.cookies and profile.get("cookies"):
            args.cookies = profile["cookies"]

    extra: list[str] = []
    if "bilibili" in host or "b23.tv" in host:
        extra += bilibili_args(args.cookies)
    elif args.cookies:
        extra += ["--cookies", args.cookies]
    if args.cookies_from_browser:
        extra += ["--cookies-from-browser", args.cookies_from_browser]
    if proxy:
        extra += ["--proxy", proxy]
    if matched and matched[1].get("args"):
        extra += [str(item) for item in matched[1]["args"]]
    if not args.playlist:
        extra.append("--no-playlist")

    selector = quality_selector(args.quality, args.audio)
    template = str(out_dir / "%(title).120B [%(id)s].%(ext)s")
    base = [
        "--newline",
        "--no-warnings",
        "-o", template,
        "-f", selector,
    ]
    if args.audio:
        base += ["-x", "--audio-format", "m4a"]
    else:
        base += ["--merge-output-format", "mp4"]
    if args.chapters:
        base.append("--write-info-json")

    if args.playlist:
        return run_playlist(args, python, extra, passthrough, out_dir, version, host)

    if args.inspect:
        print(f"→ yt-dlp {version}  站点={host or '未知'}  解释器={python}")
        return run_ytdlp(python, args.url, ["--no-warnings", "-F", *extra, *passthrough, "--"])

    print(f"→ yt-dlp {version}  站点={host or '未知'}  画质={args.quality}"
          + ("  仅音频" if args.audio else "") + f"  目录={out_dir}")
    # 最终文件路径写进临时文件（--print-to-file），这样进度可以实时流到终端，
    # 又不必去解析与进度条混在一起的标准输出。
    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False,
                                     encoding="utf-8") as handle:
        path_file = Path(handle.name)
    produced: Path | None = None
    try:
        command = [python, "-m", "yt_dlp", *base,
                   "--print-to-file", "after_move:filepath", str(path_file),
                   *extra, *passthrough, args.url]
        if args.json:
            # --json 约定「整段输出就是一个 JSON 文档」，所以这时才屏蔽进度
            result = subprocess.run(command, capture_output=True)
            if result.returncode != 0:
                sys.stderr.write((result.stderr or b"").decode("utf-8", "replace")[-2000:])
                return result.returncode
        else:
            result = subprocess.run(command)
            if result.returncode != 0:
                return result.returncode
        for line in reversed([ln.strip() for ln in path_file.read_text(encoding="utf-8").splitlines()
                              if ln.strip()]):
            if Path(line).is_file():
                produced = Path(line)
                break
    finally:
        path_file.unlink(missing_ok=True)

    if produced is None:
        print("! yt-dlp 正常退出但没有报告最终文件路径", file=sys.stderr)
        return 1

    # ---- 附加产物：字幕 / 章节 / 弹幕 ----------------------------------
    extras: dict[str, object] = {}
    if args.subs:
        # 字幕单独跑一遍：拉字幕失败（例如 YouTube 429）不该把已经下好的视频判失败
        subtitle_command = [
            python, "-m", "yt_dlp", "--skip-download", "--ignore-errors",
            "--write-subs", "--write-auto-subs", "--sub-langs", "zh.*,en.*",
            "--convert-subs", "srt", "-o", template, *extra, *passthrough, args.url,
        ]
        subtitle_result = subprocess.run(subtitle_command, capture_output=True)
        if subtitle_result.returncode != 0:
            detail = (subtitle_result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            extras["subs_error"] = detail[-1][:160] if detail else "字幕下载失败"
        extras["subs"] = collect_subs(produced)
    if args.chapters:
        info_path = produced.parent / (produced.stem + ".info.json")
        if info_path.exists():
            try:
                text = chapters_text(json.loads(info_path.read_text(encoding="utf-8")))
            finally:
                info_path.unlink(missing_ok=True)
            if text:
                chapter_path = produced.parent / (produced.stem + ".chapters.txt")
                chapter_path.write_text(text + "\n", encoding="utf-8")
                extras["chapters"] = str(chapter_path)
    if args.danmaku:
        try:
            data = fetch_danmaku(args.url)
            if data is None:
                extras["danmaku_skipped"] = "不是 B 站链接"
            else:
                danmaku_path = produced.parent / (produced.stem + ".danmaku.xml")
                danmaku_path.write_bytes(data)
                extras["danmaku"] = str(danmaku_path)
                extras["danmaku_bytes"] = len(data)
        except Exception as exc:
            extras["danmaku_error"] = f"{type(exc).__name__}: {exc}"

    summary = {
        "url": args.url,
        "site": host,
        "path": str(produced) if produced else None,
        "size": produced.stat().st_size if produced else None,
        "sha256": sha256_of(produced) if produced else None,
        "quality": args.quality,
        "audio_only": args.audio,
            "extras": extras,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"\n完成：{summary['path']}")
        print(f"  大小  : {summary['size']} 字节")
        print(f"  SHA-256: {summary['sha256']}")
        for name, value in extras.items():
            print(f"  {name:<8}: {value}")
    return 0 if produced else 1


if __name__ == "__main__":
    raise SystemExit(main())
