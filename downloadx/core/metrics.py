"""速度计量：滑动窗口 + 全局统计。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


class SpeedMeter:
    """基于滑动窗口的瞬时速度估算。

    窗口内保留 (时间戳, 累计字节) 采样点，速度 = (尾-首) 字节 / (尾-首) 时间。
    这比"固定间隔差值"更稳，短时抖动会被窗口抹平。
    """

    __slots__ = ("_samples", "_window", "_total")

    def __init__(self, window: float = 5.0) -> None:
        self._window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._total = 0

    @property
    def total(self) -> int:
        return self._total

    def add(self, nbytes: int, now: float | None = None) -> None:
        if nbytes <= 0:
            return
        now = time.monotonic() if now is None else now
        self._total += nbytes
        self._samples.append((now, self._total))
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def speed(self, now: float | None = None) -> float:
        """返回 bytes/s。"""
        now = time.monotonic() if now is None else now
        self._trim(now)
        if len(self._samples) < 2:
            return 0.0
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        return (b1 - b0) / dt

    def reset(self) -> None:
        self._samples.clear()

    def snapshot(self, now: float | None = None) -> dict[str, float | int]:
        return {"bytes": self._total, "speed": self.speed(now)}


@dataclass
class Stats:
    """全局统计快照。"""

    active_tasks: int = 0
    queued_tasks: int = 0
    paused_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    canceled_tasks: int = 0
    total_tasks: int = 0
    download_speed: float = 0.0
    total_downloaded_bytes: int = 0
    total_remaining_bytes: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


def format_bytes(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def format_speed(bps: float) -> str:
    return f"{format_bytes(bps)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    if seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60:02d}m"


def format_duration(seconds: float | None) -> str:
    return format_eta(seconds)
