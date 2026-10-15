"""分片模型与分片规划。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


class SegmentStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


@dataclass
class Segment:
    """一个字节区间 [start, end]（闭区间，绝对偏移）。"""

    index: int
    start: int
    end: int
    received: int = 0
    status: SegmentStatus = SegmentStatus.PENDING
    attempts: int = 0
    error: str | None = None

    # ---- 派生属性 -----------------------------------------------------
    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def remaining(self) -> int:
        return max(0, self.size - self.received)

    @property
    def next_offset(self) -> int:
        """下一条 Range 请求的起始偏移（续传关键）。"""
        return self.start + self.received

    @property
    def is_done(self) -> bool:
        return self.received >= self.size

    def mark_active(self) -> None:
        """本轮开始下载：把未完成的区间重新置为活跃。"""
        if self.is_done:
            self.status = SegmentStatus.DONE
        else:
            self.status = SegmentStatus.ACTIVE
        self.error = None
        self.attempts += 1

    def mark_done(self) -> None:
        self.received = self.size
        self.status = SegmentStatus.DONE
        self.error = None

    def mark_failed(self, error: str) -> None:
        self.status = SegmentStatus.FAILED
        self.error = error

    def mark_complete_unknown(self) -> None:
        """streaming 模式（总大小未知）：只标记完成，不改字节计数。"""
        self.status = SegmentStatus.DONE
        self.error = None

    def reset(self) -> None:
        self.received = 0
        self.status = SegmentStatus.PENDING
        self.error = None

    # ---- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "received": self.received,
            "status": str(self.status),
            "attempts": self.attempts,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            index=int(data["index"]),
            start=int(data["start"]),
            end=int(data["end"]),
            received=int(data.get("received", 0)),
            status=SegmentStatus(data.get("status", SegmentStatus.PENDING)),
            attempts=int(data.get("attempts", 0)),
            error=data.get("error"),
        )


def plan_segments(total_size: int, connections: int, min_segment_size: int) -> list[Segment]:
    """按总大小切分区间。

    - 小文件不分片（1 个连接即可，避免无意义的多连接开销）
    - 分片数 = clamp(total_size // min_segment_size, 1, connections)
    - 余数均摊到前 N 片，保证区间连续且不重叠
    """
    if total_size <= 0:
        return []
    connections = max(1, int(connections))
    min_segment_size = max(1, int(min_segment_size))
    count = max(1, min(connections, total_size // min_segment_size or 1))
    base, remainder = divmod(total_size, count)

    segments: list[Segment] = []
    offset = 0
    for i in range(count):
        size = base + (1 if i < remainder else 0)
        if size <= 0:
            continue
        segments.append(Segment(index=len(segments), start=offset, end=offset + size - 1))
        offset += size
    return segments


def total_received(segments: Sequence[Segment]) -> int:
    return sum(s.received for s in segments)


def all_done(segments: Sequence[Segment]) -> bool:
    return bool(segments) and all(s.is_done for s in segments)


def split_into_single(total_size: int) -> list[Segment]:
    """单连接模式：整个文件一片。"""
    if total_size <= 0:
        return []
    return [Segment(index=0, start=0, end=total_size - 1)]


@dataclass
class SegmentPlan:
    """分片方案 + 依据，便于诊断（写入任务元数据）。"""

    mode: str  # "segmented" | "single" | "streaming"
    segments: list[Segment] = field(default_factory=list)
    reason: str = ""

    @property
    def connection_count(self) -> int:
        return len(self.segments)


def build_plan(
    *,
    total_size: int | None,
    connections: int,
    min_segment_size: int,
    resumable: bool,
) -> SegmentPlan:
    """决定使用哪种下载模式。

    - 未知大小 / 服务端不支持 Range -> streaming（顺序追加，不续传）
    - 已知大小且支持 Range -> segmented（多连接 + 断点续传）
    """
    if total_size is None or total_size <= 0:
        return SegmentPlan(mode="streaming", segments=[], reason="服务端未提供 Content-Length")
    if not resumable:
        return SegmentPlan(
            mode="single",
            segments=split_into_single(total_size),
            reason="服务端不支持 Range 请求，无法断点续传",
        )
    segments = plan_segments(total_size, connections, min_segment_size)
    reason = f"{len(segments)} 分片并发下载" if len(segments) > 1 else "文件较小，单连接下载"
    return SegmentPlan(mode="segmented", segments=segments, reason=reason)
