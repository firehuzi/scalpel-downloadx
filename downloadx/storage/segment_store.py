"""分片账本：规划分片、按元数据恢复分片、计算整体进度。

这里是"断点续传"的事实来源 —— 只要每个分片的 ``received`` 是准的，
任何时候重启都能从正确偏移继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..core.segment import (
    Segment,
    SegmentPlan,
    all_done,
    build_plan,
    total_received,
)
from ..config import Config


@dataclass
class SegmentProgress:
    received: int
    total: int | None
    done_segments: int
    total_segments: int

    @property
    def percent(self) -> float:
        if not self.total:
            return 0.0
        return min(100.0, self.received / self.total * 100.0)

    @property
    def complete(self) -> bool:
        return self.total is not None and self.received >= self.total

    @property
    def remaining(self) -> int | None:
        return None if self.total is None else max(0, self.total - self.received)

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "total": self.total,
            "percent": round(self.percent, 4),
            "done_segments": self.done_segments,
            "total_segments": self.total_segments,
        }


class SegmentStore:
    """无状态工具类：把 config + task 元数据映射成分片表。"""

    def __init__(self, config: Config) -> None:
        self.config = config

    def plan(
        self,
        *,
        total_size: int | None,
        connections: int,
        resumable: bool,
    ) -> SegmentPlan:
        return build_plan(
            total_size=total_size,
            connections=max(1, min(connections, self.config.max_connections_per_task)),
            min_segment_size=self.config.min_segment_size,
            resumable=resumable,
        )

    # ---- 元数据 <-> 分片 ----------------------------------------------
    @staticmethod
    def load(task_data: dict[str, Any]) -> list[Segment]:
        raw = task_data.get("segments") or []
        segments: list[Segment] = []
        for item in raw:
            try:
                segments.append(Segment.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        segments.sort(key=lambda s: s.index)
        return segments

    @staticmethod
    def dump(segments: Sequence[Segment]) -> list[dict[str, Any]]:
        return [s.to_dict() for s in segments]

    @staticmethod
    def progress(segments: Sequence[Segment], total_size: int | None) -> SegmentProgress:
        received = total_received(segments)
        if total_size is None and segments:
            # streaming 模式：end 记录的是"已写入长度 - 1"
            total_size = segments[0].end + 1
        return SegmentProgress(
            received=received,
            total=total_size,
            done_segments=sum(1 for s in segments if s.is_done),
            total_segments=len(segments),
        )

    @staticmethod
    def reset(segments: Sequence[Segment]) -> None:
        """从头重下：清空所有分片进度。"""
        for segment in segments:
            segment.reset()

    @staticmethod
    def is_complete(segments: Sequence[Segment]) -> bool:
        return all_done(segments)
