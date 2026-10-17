#!/usr/bin/env python3
"""站点适配回归：定期回答"这些站现在还能不能下、最高什么画质"。

它只做**探测**（yt-dlp 的 simulate 模式 / 抓播放列表首行），不下载媒体，
所以几十秒就能跑完，适合定期执行：

* 每个站点输出 可用性 + 最高画质 + 格式数；
* B 站会顺带报告"有没有配 cookies"（未登录上限 480P）；
* X 需要一条真实推文 URL（``--x-url``）才能实测，否则只确认工具就绪（标记为跳过）；
* HLS / DASH 用公开测试流验证引擎能不能解析清单。

退出码：全部可用 = 0；有任何站点失败 = 1（可直接接到定时任务里当告警）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_video as fv  # noqa: E402  复用解释器探测 / B 站请求头 / X 脚本定位

YOUTUBE_PROBE = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
BILIBILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular?ps=3&pn=1"
HLS_PROBE = (
    "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_4x3/"
    "bipbop_4x3_variant.m3u8"
)
DASH_PROBE = "https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd"


def _http(url: str, timeout: float = 20.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": fv.UA_CHROME})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception:
        return 0, b""


def _ytdlp_info(python: str, url: str, extra: list[str], timeout: float) -> dict:
    command = [python, "-m", "yt_dlp", "--simulate", "--no-warnings", "-J", *extra, url]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"yt-dlp 超时（>{timeout:.0f}s）")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detail[-1][:180] if detail else f"yt-dlp 退出码 {result.returncode}")
    return json.loads(result.stdout.decode("utf-8", "replace") or "{}")


def _quality(info: dict) -> dict:
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    heights = [f["height"] for f in formats if isinstance(f.get("height"), int)]
    return {
        "title": info.get("title"),
        "formats": len(formats),
        "max_height": max(heights) if heights else None,
        "duration": info.get("duration"),
    }


# ----------------------------------------------------------------------
# 各站点检查
# ----------------------------------------------------------------------
def check_youtube(python: str, timeout: float, x_url: str | None = None) -> dict:
    info = _ytdlp_info(python, YOUTUBE_PROBE, ["--no-playlist"], timeout)
    return {"ok": True, **_quality(info)}


def check_bilibili(python: str, timeout: float, x_url: str | None = None) -> dict:
    status, body = _http(BILIBILI_POPULAR)
    if status != 200:
        return {"ok": False, "error": f"B 站热门榜不可达（HTTP {status}）"}
    items = (json.loads(body.decode("utf-8")) or {}).get("data", {}).get("list") or []
    if not items:
        return {"ok": False, "error": "热门榜没有返回条目"}
    bvid = items[0]["bvid"]
    info = _ytdlp_info(python, f"https://www.bilibili.com/video/{bvid}", fv.bilibili_args(None), timeout)
    profile = fv.site_profile("www.bilibili.com")
    has_cookies = bool(profile and (profile[1] or {}).get("cookies"))
    result = {"ok": True, "url": f"https://www.bilibili.com/video/{bvid}", "has_cookies": has_cookies,
              **_quality(info)}
    if not has_cookies:
        result["note"] = "未配置 cookies：只有 480P；用 --site-set bilibili.com --cookies <file> 解锁 1080P+"
    return result


def check_x(python: str, timeout: float, x_url: str | None = None) -> dict:
    script = fv.find_x_video_script()
    if script is None:
        return {"ok": False, "error": "没装 download-x-video skill"}
    if not x_url:
        return {"ok": True, "skipped": True,
                "note": "未提供测试推文（--x-url），只确认 download-x-video 就绪"}
    command = [sys.executable, str(script), "inspect", x_url, "--json"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "inspect 超时"}
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "error": detail[-1][:180] if detail else "inspect 失败"}
    data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    return {"ok": True, "variants": len(data.get("variants") or data.get("formats") or [])}


def check_hls(python: str, timeout: float, x_url: str | None = None) -> dict:
    status, body = _http(HLS_PROBE)
    if status != 200 or not body.startswith(b"#EXTM3U"):
        return {"ok": False, "error": f"HLS 测试流不可用（HTTP {status}）"}
    variants = body.count(b"#EXT-X-STREAM-INF")
    return {"ok": True, "variants": variants, "note": f"Apple 示例流，{variants} 档变体"}


def check_dash(python: str, timeout: float, x_url: str | None = None) -> dict:
    status, body = _http(DASH_PROBE)
    if status != 200 or b"<MPD" not in body:
        return {"ok": False, "error": f"DASH 测试流不可用（HTTP {status}）"}
    return {"ok": True, "bytes": len(body), "note": f"Akamai 示例流，清单 {len(body)} 字节"}


CHECKS = {
    "youtube": check_youtube,
    "bilibili": check_bilibili,
    "x": check_x,
    "hls": check_hls,
    "dash": check_dash,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="站点适配回归检查")
    parser.add_argument("--sites", default=",".join(CHECKS), help="逗号分隔，默认全部")
    parser.add_argument("--x-url", default=None, help="一条真实推文 URL，用于实测 X")
    parser.add_argument("--timeout", type=float, default=90.0, help="单站点超时秒数")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()

    python, version = fv.pick_python()
    names = [name.strip() for name in args.sites.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        parser.error(f"未知站点：{', '.join(unknown)}（可选：{', '.join(CHECKS)}）")

    started = time.time()
    results: list[dict] = []
    for name in names:
        begin = time.time()
        try:
            outcome = CHECKS[name](python, args.timeout, args.x_url)
        except Exception as exc:  # 单站点失败不影响其它
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        outcome.update({"site": name, "seconds": round(time.time() - begin, 1)})
        results.append(outcome)

    failed = [item for item in results if not item.get("ok")]
    if args.json:
        print(json.dumps({"yt_dlp": version, "elapsed": round(time.time() - started, 1),
                          "results": results}, ensure_ascii=False))
    else:
        print(f"yt-dlp {version}   共 {len(results)} 个站点，用时 {time.time() - started:.1f}s\n")
        print(f"{'站点':<10}{'结果':<8}{'最高画质':<10}{'格式数':<8}备注")
        for item in results:
            mark = "跳过" if item.get("skipped") else ("可用" if item.get("ok") else "失败")
            height = item.get("max_height")
            quality = f"{height}p" if height else "-"
            note = item.get("note") or item.get("error") or item.get("title") or ""
            print(f"{item['site']:<10}{mark:<8}{quality:<10}{str(item.get('formats', '-')):<8}{note[:60]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
