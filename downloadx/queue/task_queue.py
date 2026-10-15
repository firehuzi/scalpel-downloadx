"""任务队列：把"谁能下载"和"现在跑几个"这两件事收在一处。"""

from __future__ import annotations

from .bandwidth_controller import BandwidthController
from .priority_queue import PriorityQueue


class TaskQueue:
    def __init__(self, bandwidth: BandwidthController, max_concurrent: int = 3) -> None:
        self.bandwidth = bandwidth
        self.max_concurrent = max(1, int(max_concurrent))
        self._pending = PriorityQueue()
        self._active: set[str] = set()
        self._paused_global = False

    # ---- 入队 / 出队 ---------------------------------------------------
    def push(self, task_id: str, priority: int = 5) -> bool:
        return self._pending.push(task_id, priority)

    def pop(self) -> str | None:
        if not self.can_start():
            return None
        return self._pending.pop()

    def remove(self, task_id: str) -> bool:
        self._active.discard(task_id)
        self.bandwidth.clear_task(task_id)
        return self._pending.remove(task_id)

    def set_priority(self, task_id: str, priority: int) -> bool:
        return self._pending.set_priority(task_id, priority)

    # ---- 活跃集合 -----------------------------------------------------
    def mark_active(self, task_id: str) -> None:
        self._pending.remove(task_id)
        self._active.add(task_id)

    def mark_idle(self, task_id: str) -> None:
        self._active.discard(task_id)

    @property
    def active(self) -> set[str]:
        return set(self._active)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def can_start(self) -> bool:
        return (not self._paused_global) and len(self._active) < self.max_concurrent

    # ---- 全局暂停 -----------------------------------------------------
    @property
    def globally_paused(self) -> bool:
        return self._paused_global

    def pause_all(self) -> None:
        self._paused_global = True

    def resume_all(self) -> None:
        self._paused_global = False

    # ---- 观测 ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._pending)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._pending or task_id in self._active

    @property
    def pending(self) -> PriorityQueue:
        return self._pending

    def pending_ids(self) -> list[str]:
        return self._pending.ids()

    def clear(self) -> None:
        self._pending.clear()
        self._active.clear()

    def stats(self) -> dict[str, object]:
        return {
            "pending": len(self._pending),
            "active": len(self._active),
            "max_concurrent": self.max_concurrent,
            "globally_paused": self._paused_global,
            "bandwidth": self.bandwidth.snapshot(),
        }
