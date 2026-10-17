"""DASH（.mpd）解析：选音视频轨、展开分片 URL。

支持：

* ``SegmentTemplate``（``$Number$`` / ``$Time$`` / ``$RepresentationID$``，含 ``SegmentTimeline``）
* ``SegmentList``（显式列出的分片）
* ``SegmentBase``（单文件 + 索引 → 整文件就是一个可用 mp4）

拒绝：``ContentProtection``（DRM）。多 Period 只取第一个（VOD 常态）。
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse


class DashError(Exception):
    pass


@dataclass
class DashTrack:
    kind: str  # video | audio
    urls: list[str] = field(default_factory=list)
    init: str = ""
    height: int = 0
    bandwidth: int = 0
    duration: float = 0.0

    @property
    def label(self) -> str:
        if self.kind == "video":
            return f"{self.height}p@{self.bandwidth // 1000}kbps" if self.height else f"{self.bandwidth // 1000}kbps"
        return f"audio@{self.bandwidth // 1000}kbps"


def looks_like_dash(url: str, content_type: str = "") -> bool:
    path = urlparse(url or "").path.lower()
    return path.endswith(".mpd") or "dash+xml" in (content_type or "").lower()


_DUR_RE = re.compile(
    r"^P(?:(?P<days>[\d.]+)D)?(?:T(?:(?P<hours>[\d.]+)H)?(?:(?P<minutes>[\d.]+)M)?"
    r"(?:(?P<seconds>[\d.]+)S)?)?$"
)


def parse_duration(value: str | None) -> float:
    """ISO-8601 时长（PT1H2M3.5S）→ 秒。"""
    if not value:
        return 0.0
    match = _DUR_RE.match(value.strip())
    if not match:
        return 0.0
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node if _local(child.tag) == name]


def _first(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _expand(template: str, *, rep_id: str = "", number: int | None = None, time: int | None = None) -> str:
    out = template.replace("$RepresentationID$", rep_id)
    if time is not None:
        out = out.replace("$Time$", str(time))
    if number is not None:
        def repl(match: re.Match[str]) -> str:
            width = match.group(1)
            return str(number).zfill(int(width)) if width else str(number)

        out = re.sub(r"\$Number(?:%0(\d+)d)?\$", repl, out)
    return out


def _base_chain(node: ET.Element, inherited: str) -> str:
    """把 BaseURL 逐层拼起来（DASH 允许每层都写相对 BaseURL）。"""
    base = inherited
    child = _first(node, "BaseURL")
    if child is not None and (child.text or "").strip():
        base = urljoin(base, child.text.strip())
    return base


def _timeline(segment_timeline: ET.Element) -> list[tuple[int, int]]:
    """展开 SegmentTimeline 为 [(time, duration), ...]。"""
    runs: list[tuple[int, int]] = []
    cursor = 0
    for item in _children(segment_timeline, "S"):
        start = int(item.get("t")) if item.get("t") is not None else cursor
        duration = int(item.get("d") or 0)
        repeat = int(item.get("r") or 0)
        if duration <= 0:
            continue
        for _ in range(repeat + 1):
            runs.append((start, duration))
            start += duration
        cursor = start
    return runs


def _track_from_representation(
    rep: ET.Element, *, kind: str, base: str, rep_id: str, period_duration: float
) -> DashTrack | None:
    base = _base_chain(rep, base)
    bandwidth = int(rep.get("bandwidth") or 0)
    height = int(rep.get("height") or 0)
    track = DashTrack(kind=kind, height=height, bandwidth=bandwidth, duration=period_duration)

    template = _first(rep, "SegmentTemplate")
    segment_list = _first(rep, "SegmentList")
    segment_base = _first(rep, "SegmentBase")

    if template is not None:
        media = template.get("media") or ""
        if not media:
            return None
        init_tmpl = template.get("initialization") or ""
        if init_tmpl:
            track.init = urljoin(base, _expand(init_tmpl, rep_id=rep_id))
        timescale = float(template.get("timescale") or 1)
        start_number = int(template.get("startNumber") or 1)
        timeline = _first(template, "SegmentTimeline")

        if timeline is not None:
            runs = _timeline(timeline)
            for time_value, _ in runs:
                track.urls.append(urljoin(base, _expand(media, rep_id=rep_id, time=time_value)))
        else:
            duration_attr = float(template.get("duration") or 0)
            if duration_attr <= 0 or period_duration <= 0:
                raise DashError("SegmentTemplate 缺少 duration 且没有 SegmentTimeline，无法推算分片")
            seg_seconds = duration_attr / timescale
            count = max(1, math.ceil(period_duration / seg_seconds))
            for i in range(count):
                track.urls.append(
                    urljoin(base, _expand(media, rep_id=rep_id, number=start_number + i))
                )
        return track if track.urls else None

    if segment_list is not None:
        init = _first(segment_list, "Initialization")
        if init is not None and init.get("sourceURL"):
            track.init = urljoin(base, init.get("sourceURL") or "")
        for item in _children(segment_list, "SegmentURL"):
            media = item.get("media")
            if media:
                track.urls.append(urljoin(base, media))
        return track if track.urls else None

    if segment_base is not None:
        # 单文件 + 索引：整个文件就是一个可用的 mp4
        target = base
        if not target:
            return None
        track.urls.append(target)
        return track

    # 没有分片信息的 Representation（例如远程 BaseURL 直接指向文件）
    if base and base != urlparse(base).geturl():
        track.urls.append(base)
        return track
    return None


def parse(text: str, base_url: str, max_height: int | None = None) -> list[DashTrack]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DashError(f"MPD 解析失败：{exc}") from exc
    if _local(root.tag) != "MPD":
        raise DashError("不是 DASH MPD 文档")

    if _first(root, "ContentProtection") is not None:
        raise DashError("带 ContentProtection（DRM）的流不在支持范围")

    mpd_duration = parse_duration(root.get("mediaPresentationDuration"))
    base = _base_chain(root, base_url)

    periods = _children(root, "Period")
    if not periods:
        raise DashError("MPD 里没有 Period")
    period = periods[0]
    period_base = _base_chain(period, base)
    period_duration = parse_duration(period.get("duration")) or mpd_duration
    if period_duration <= 0:
        raise DashError("无法确定时长（mediaPresentationDuration 缺失）")

    tracks: list[DashTrack] = []
    for adaptation in _children(period, "AdaptationSet"):
        if _first(adaptation, "ContentProtection") is not None:
            raise DashError("带 ContentProtection（DRM）的流不在支持范围")
        mime = (adaptation.get("mimeType") or "").lower()
        content_type = (adaptation.get("contentType") or "").lower()
        if mime.startswith("video") or content_type == "video":
            kind = "video"
        elif mime.startswith("audio") or content_type == "audio":
            kind = "audio"
        else:
            continue

        adaptation_base = _base_chain(adaptation, period_base)
        # SegmentTemplate 也可能写在 AdaptationSet 上，Representation 继承
        inherited_template = _first(adaptation, "SegmentTemplate")
        reps = _children(adaptation, "Representation")
        if not reps:
            continue
        candidates: list[DashTrack] = []
        for rep in reps:
            if inherited_template is not None and _first(rep, "SegmentTemplate") is None:
                # 把 AdaptationSet 的模板挂到 rep 上（浅拷贝一个即可）
                rep.append(inherited_template)
            track = _track_from_representation(
                rep,
                kind=kind,
                base=adaptation_base,
                rep_id=rep.get("id") or "",
                period_duration=period_duration,
            )
            if track is not None:
                candidates.append(track)
        if not candidates:
            continue
        if kind == "video":
            usable = candidates
            if max_height:
                capped = [t for t in candidates if not t.height or t.height <= max_height]
                usable = capped or candidates
            tracks.append(max(usable, key=lambda t: (t.height, t.bandwidth)))
        else:
            tracks.append(max(candidates, key=lambda t: t.bandwidth))

    if not tracks:
        raise DashError("MPD 里没有可下载的音视频轨")
    return tracks
