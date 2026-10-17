"""下载任务的数据模型（可持久化的唯一事实来源）。

字段设计原则：**任何时刻从磁盘读回这份 JSON，都必须能继续下载**。
所以进度、分片表、探测结果、重试次数、校验值全在模型里。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..network.range_request import guess_filename, sanitize_filename
from ..storage.segment_store import SegmentProgress, SegmentStore
from ..version import PART_SUFFIX, SCHEMA_VERSION
from .segment import Segment
from .state_machine import (
    StateMachine,
    TaskStatus,
    allowed_transitions,
    is_terminal,
)


def new_task_id() -> str:
    return uuid.uuid4().hex


@dataclass
class DownloadTask:
    # ---- 基本字段（任务书 §4）------------------------------------------
    id: str
    url: str
    filename: str
    save_path: str
    status: TaskStatus = TaskStatus.QUEUED
    total_size: int | None = None
    downloaded_size: int = 0
    created_at: float = field(default_factory=time.time)

    # ---- 调度 ----------------------------------------------------------
    priority: int = 5
    connections: int = 8
    speed_limit: int = 0  # bytes/s，0 表示不限速

    # ---- 传输细节 ------------------------------------------------------
    mode: str = "segmented"  # segmented | single | streaming
    resumable: bool = False
    accept_ranges: bool = False
    segments: list[Segment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    final_url: str = ""
    etag: str = ""
    last_modified: str = ""
    content_type: str = ""

    # ---- 时间线 --------------------------------------------------------
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    elapsed: float = 0.0  # 累计实际下载耗时（不含排队/暂停）

    # ---- 错误与重试 ----------------------------------------------------
    error: str | None = None
    error_code: str | None = None
    retry_count: int = 0
    attempts: int = 0

    # ---- 校验 ----------------------------------------------------------
    expected_sha256: str | None = None
    sha256: str | None = None
    verified: bool | None = None

    # ---- 流式协议（HLS / DASH）：每条流轨一个 dict ----------------------
    #: 形如 ``[{"kind": "video", "init": "…", "urls": [...], "done": [...]}]``。
    #: 分片索引即该轨内的顺序；``done`` 是已完成的分片下标（持久化，用于断点续传）。
    stream_tracks: list[dict[str, Any]] = field(default_factory=list)
    #: 所有轨的分片总数（用于百分比，避免每次重算）
    stream_count: int = 0

    # ---- 备注 ----------------------------------------------------------
    note: str = ""
    plan_reason: str = ""
    filename_locked: bool = False  # 用户显式指定文件名时，不再跟随服务端的猜测
    schema_version: int = SCHEMA_VERSION

    # 运行时对象，不参与序列化
    _machine: StateMachine = field(default=None, repr=False, compare=False, init=False)  # type: ignore[assignment]
    #: 本轮实际使用的并发连接数（被服务端限流时会被引擎自动调低，不持久化）
    effective_connections: int | None = field(default=None, repr=False, compare=False, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        self._machine = StateMachine(self.status)

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        url: str,
        *,
        save_dir: str | Path,
        filename: str | None = None,
        priority: int = 5,
        connections: int = 8,
        speed_limit: int = 0,
        headers: dict[str, str] | None = None,
        expected_sha256: str | None = None,
        note: str = "",
    ) -> "DownloadTask":
        safe_name = sanitize_filename(filename) if filename else guess_filename(url)
        save_dir = Path(save_dir)
        return cls(
            id=new_task_id(),
            url=url,
            filename=safe_name,
            save_path=str(save_dir / safe_name),
            priority=int(priority),
            connections=int(connections),
            speed_limit=int(speed_limit),
            headers=dict(headers or {}),
            expected_sha256=expected_sha256,
            note=note,
            filename_locked=bool(filename),
        )

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    @property
    def state(self) -> StateMachine:
        return self._machine

    def can_transition(self, target: TaskStatus | str) -> bool:
        return self._machine.can(target)

    def allowed(self) -> list[TaskStatus]:
        return allowed_transitions(self.status)

    def transition(self, target: TaskStatus | str, *, error: str | None = None) -> TaskStatus:
        target = TaskStatus(target)
        previous = self.status
        self.status = self._machine.to(target)
        now = time.time()
        self.updated_at = now
        if self.status is TaskStatus.DOWNLOADING and self.started_at is None:
            self.started_at = now
        if self.status is TaskStatus.COMPLETED:
            self.completed_at = now
        if self.status is TaskStatus.FAILED:
            self.error = error or self.error
        if self.status is TaskStatus.QUEUED and previous is TaskStatus.FAILED:
            self.error = None
            self.error_code = None
        return self.status

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @property
    def is_active(self) -> bool:
        return self.status in (TaskStatus.DOWNLOADING, TaskStatus.VERIFYING)

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def part_path(self) -> str:
        return self.save_path + PART_SUFFIX

    @property
    def progress(self) -> SegmentProgress:
        progress = SegmentStore.progress(self.segments, self.total_size)
        progress.received = max(progress.received, self.downloaded_size)
        if progress.total is None:
            progress.total = self.total_size
        return progress

    @property
    def percent(self) -> float:
        if self.mode in ("hls", "dash") and self.stream_count:
            return round(self.stream_done_count / self.stream_count * 100, 2)
        return round(self.progress.percent, 2)

    @property
    def stream_done_count(self) -> int:
        return sum(len(track.get("done") or []) for track in self.stream_tracks)

    @property
    def remaining(self) -> int | None:
        return self.progress.remaining

    def eta(self, speed: float) -> float | None:
        remaining = self.remaining
        if remaining is None or speed <= 0:
            return None
        return remaining / speed

    def sync_progress(self) -> int:
        """把分片进度汇总到 downloaded_size。"""
        self.downloaded_size = max(self.downloaded_size, sum(s.received for s in self.segments))
        return self.downloaded_size

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "save_path": self.save_path,
            "status": str(self.status),
            "total_size": self.total_size,
            "downloaded_size": self.downloaded_size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "priority": self.priority,
            "connections": self.connections,
            "effective_connections": self.effective_connections or self.connections,
            "speed_limit": self.speed_limit,
            "mode": self.mode,
            "resumable": self.resumable,
            "accept_ranges": self.accept_ranges,
            "segments": [s.to_dict() for s in self.segments],
            "headers": dict(self.headers),
            "final_url": self.final_url,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_type": self.content_type,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed": self.elapsed,
            "error": self.error,
            "error_code": self.error_code,
            "retry_count": self.retry_count,
            "attempts": self.attempts,
            "expected_sha256": self.expected_sha256,
            "sha256": self.sha256,
            "verified": self.verified,
            "note": self.note,
            "plan_reason": self.plan_reason,
            "filename_locked": self.filename_locked,
            "stream_tracks": [dict(track) for track in self.stream_tracks],
            "stream_count": self.stream_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadTask":
        segments: list[Segment] = []
        for item in data.get("segments") or []:
            try:
                segments.append(Segment.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        segments.sort(key=lambda s: s.index)
        task = cls(
            id=str(data["id"]),
            url=str(data["url"]),
            filename=str(data.get("filename") or "download"),
            save_path=str(data["save_path"]),
            status=TaskStatus(data.get("status", TaskStatus.QUEUED)),
            total_size=data.get("total_size"),
            downloaded_size=int(data.get("downloaded_size") or 0),
            created_at=float(data.get("created_at") or time.time()),
            priority=int(data.get("priority", 5)),
            connections=int(data.get("connections", 8)),
            speed_limit=int(data.get("speed_limit") or 0),
            mode=str(data.get("mode") or "segmented"),
            resumable=bool(data.get("resumable", False)),
            accept_ranges=bool(data.get("accept_ranges", False)),
            segments=segments,
            headers=dict(data.get("headers") or {}),
            final_url=str(data.get("final_url") or ""),
            etag=str(data.get("etag") or ""),
            last_modified=str(data.get("last_modified") or ""),
            content_type=str(data.get("content_type") or ""),
            updated_at=float(data.get("updated_at") or time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            elapsed=float(data.get("elapsed") or 0.0),
            error=data.get("error"),
            error_code=data.get("error_code"),
            retry_count=int(data.get("retry_count") or 0),
            attempts=int(data.get("attempts") or 0),
            expected_sha256=data.get("expected_sha256"),
            sha256=data.get("sha256"),
            verified=data.get("verified"),
            note=str(data.get("note") or ""),
            plan_reason=str(data.get("plan_reason") or ""),
            filename_locked=bool(data.get("filename_locked", False)),
            stream_tracks=list(data.get("stream_tracks") or _legacy_hls_track(data)),
            stream_count=int(data.get("stream_count") or _legacy_hls_count(data)),
        )
        return task

    # ------------------------------------------------------------------
    # 展示用快照（API / UI）
    # ------------------------------------------------------------------
    def to_public_dict(self, *, speed: float = 0.0, eta: float | None = None, extra: dict | None = None) -> dict[str, Any]:
        progress = self.progress
        payload: dict[str, Any] = {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "save_path": self.save_path,
            "status": str(self.status),
            "mode": self.mode,
            "priority": self.priority,
            "connections": self.connections,
            "speed_limit": self.speed_limit,
            "total_size": progress.total,
            "downloaded_size": progress.received,
            "percent": round(progress.percent, 2),
            "remaining_size": progress.remaining,
            "segments_total": self.stream_count if self.mode in ("hls", "dash") else progress.total_segments,
            "segments_done": self.stream_done_count if self.mode in ("hls", "dash") else progress.done_segments,
            "speed": round(speed, 2),
            "eta": eta,
            "resumable": self.resumable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed": self.elapsed,
            "error": self.error,
            "error_code": self.error_code,
            "retry_count": self.retry_count,
            "sha256": self.sha256,
            "expected_sha256": self.expected_sha256,
            "verified": self.verified,
            "note": self.note,
            "plan_reason": self.plan_reason,
            "filename_locked": self.filename_locked,
            "allowed_actions": [str(s) for s in self.allowed()],
        }
        if extra:
            payload.update(extra)
        return payload


def _legacy_hls_track(data: dict[str, Any]) -> list[dict[str, Any]]:
    """schema v1 的 ``hls_*`` 字段 -> v2 的单轨结构（读旧任务时兜底）。"""
    urls = [str(u) for u in (data.get("hls_playlist") or [])]
    if not urls:
        return []
    return [
        {
            "kind": "media",
            "init": str(data.get("hls_init") or ""),
            "urls": urls,
            "done": [int(i) for i in (data.get("hls_done") or [])],
        }
    ]


def _legacy_hls_count(data: dict[str, Any]) -> int:
    if data.get("stream_count"):
        return int(data["stream_count"])
    return int(data.get("hls_total") or 0) or len(data.get("hls_playlist") or [])
