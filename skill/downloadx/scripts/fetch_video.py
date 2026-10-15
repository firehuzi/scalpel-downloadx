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
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
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


def main() -> int:
    parser = argparse.ArgumentParser(description="按站点分流下载视频/音频/直链")
    parser.add_argument("url")
    parser.add_argument("--dir", default=None, help="保存目录，默认用 DownloadX 的下载目录")
    parser.add_argument("--name", default=None, help="另存文件名（仅直链下载有效）")
    parser.add_argument("--quality", default="best",
                        help="best（默认）/ worst / 具体高度如 1080、720")
    parser.add_argument("--audio", action="store_true", help="只要音频（m4a）")
    parser.add_argument("--playlist", action="store_true", help="允许下载整个合集/播放列表")
    parser.add_argument("--inspect", action="store_true", help="只列格式，不下载")
    parser.add_argument("--cookies", default=None, help="cookies.txt 路径（B 站 1080P+ 需要）")
    parser.add_argument("--cookies-from-browser", default=None,
                        help="从浏览器读 cookie（本机 Edge/Chrome 会因 app-bound 加密失败）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args, passthrough = parser.parse_known_args()
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    elif passthrough:
        parser.error(f"无法识别的参数：{passthrough}（额外参数请写在 `--` 之后）")

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
    extra: list[str] = []
    if "bilibili" in host or "b23.tv" in host:
        extra += bilibili_args(args.cookies)
    elif args.cookies:
        extra += ["--cookies", args.cookies]
    if args.cookies_from_browser:
        extra += ["--cookies-from-browser", args.cookies_from_browser]
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

    summary = {
        "url": args.url,
        "site": host,
        "path": str(produced) if produced else None,
        "size": produced.stat().st_size if produced else None,
        "sha256": sha256_of(produced) if produced else None,
        "quality": args.quality,
        "audio_only": args.audio,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"\n完成：{summary['path']}")
        print(f"  大小  : {summary['size']} 字节")
        print(f"  SHA-256: {summary['sha256']}")
    return 0 if produced else 1


if __name__ == "__main__":
    raise SystemExit(main())
