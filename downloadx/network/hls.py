"""HLS（.m3u8）解析。

只做下载器该做的事：

* 主播放列表（master）→ 按分辨率/码率选一个变体；
* 媒体播放列表（media）→ 取出分片列表与 init 段；
* **加密流（EXT-X-KEY）与直播（没有 ENDLIST）明确拒绝** —— 前者涉及 DRM/密钥，
  后者不是"下载"的语义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse


class HlsError(Exception):
    pass


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    resolution: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        if self.resolution:
            return f"{self.resolution[0]}x{self.resolution[1]}@{self.bandwidth // 1000}kbps"
        return f"{self.bandwidth // 1000}kbps"


@dataclass
class HlsSegment:
    index: int
    uri: str
    duration: float = 0.0


@dataclass
class MediaPlaylist:
    segments: list[HlsSegment]
    init_uri: str | None = None
    target_duration: float = 0.0
    is_live: bool = False

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(value):
        out[match.group(1)] = match.group(2).strip().strip('"')
    return out


def looks_like_hls(url: str, content_type: str = "") -> bool:
    path = urlparse(url or "").path.lower()
    ctype = (content_type or "").lower()
    return path.endswith((".m3u8", ".m3u")) or "mpegurl" in ctype


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base_url: str) -> list[Variant]:
    variants: list[Variant] = []
    pending: tuple[int, tuple[int, int] | None] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attrs(line.split(":", 1)[1])
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
            except ValueError:
                bandwidth = 0
            resolution = None
            res = attrs.get("RESOLUTION", "")
            if "x" in res.lower():
                try:
                    width, height = res.lower().split("x", 1)
                    resolution = (int(width), int(height))
                except ValueError:
                    resolution = None
            pending = (bandwidth, resolution)
        elif line.startswith("#"):
            continue
        elif pending is not None:
            variants.append(
                Variant(uri=urljoin(base_url, line), bandwidth=pending[0], resolution=pending[1])
            )
            pending = None
    if not variants:
        raise HlsError("主播放列表里没有可用的变体")
    return variants


def pick_variant(variants: list[Variant], max_height: int | None = None) -> Variant:
    """挑最高画质；给了 ``max_height`` 就挑不超过它的最高一档。"""
    if not variants:
        raise HlsError("主播放列表里没有可用的变体")
    usable = variants
    if max_height:
        capped = [v for v in variants if not v.resolution or v.resolution[1] <= max_height]
        usable = capped or variants
    return max(usable, key=lambda v: ((v.resolution[1] if v.resolution else 0), v.bandwidth))


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    segments: list[HlsSegment] = []
    init_uri: str | None = None
    target_duration = 0.0
    duration = 0.0
    endlist = False
    encrypted: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except ValueError:
                target_duration = 0.0
        elif line.startswith("#EXT-X-KEY:"):
            method = _attrs(line.split(":", 1)[1]).get("METHOD", "NONE").upper()
            if method != "NONE":
                encrypted = method
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _attrs(line.split(":", 1)[1])
            if attrs.get("URI"):
                init_uri = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise HlsError("EXT-X-BYTERANGE 分片暂不支持（请把 URL 交给 yt-dlp）")
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif line.startswith("#"):
            continue
        else:
            segments.append(HlsSegment(index=len(segments), uri=urljoin(base_url, line), duration=duration))
            duration = 0.0

    if encrypted:
        raise HlsError(f"HLS 加密流（EXT-X-KEY METHOD={encrypted}）不在支持范围")
    if not segments:
        raise HlsError("播放列表里没有分片")
    return MediaPlaylist(
        segments=segments,
        init_uri=init_uri,
        target_duration=target_duration,
        is_live=not endlist,
    )


def summary(playlist: MediaPlaylist) -> dict[str, Any]:
    return {
        "segments": len(playlist.segments),
        "duration": round(playlist.duration, 2),
        "init": bool(playlist.init_uri),
        "live": playlist.is_live,
    }
