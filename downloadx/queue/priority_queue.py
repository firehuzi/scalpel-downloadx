"""优先级队列（同优先级 FIFO，支持运行时改优先级与移除）。

用"堆 + 惰性删除"实现：改动/删除只标记，真正的清理发生在 pop 时。
这样任意时刻都能 O(1) 地改优先级，O(log n) 地取出最优任务。
"""

from __future__ import annotations

import heapq
import itertools
from typing import Iterable


class PriorityQueue:
    """数字越小优先级越高（0 = 最高）。"""

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, str]] = []
        self._entries: dict[str, tuple[int, int]] = {}
        self._counter = itertools.count()
        self._removed: set[str] = set()

    # ---- 写 -----------------------------------------------------------
    def push(self, task_id: str, priority: int = 5) -> bool:
        if task_id in self._entries or task_id in self._removed:
            return False
        priority = int(priority)
        seq = next(self._counter)
        self._entries[task_id] = (priority, seq)
        heapq.heappush(self._heap, (priority, seq, task_id))
        return True

    def _compact(self) -> None:
        """丢弃已移除记录，并在堆顶被改过优先级时重建。"""
        if len(self._heap) > 4 * max(1, len(self._entries)) + 32:
            self._heap = [
                (prio, seq, tid)
                for tid, (prio, seq) in self._entries.items()
            ]
            heapq.heapify(self._heap)

    def pop(self) -> str | None:
        while self._heap:
            priority, seq, task_id = heapq.heappop(self._heap)
            entry = self._entries.get(task_id)
            if entry is None or entry != (priority, seq):
                continue  # 已被移除或改过优先级：跳过旧记录
            del self._entries[task_id]
            self._removed.discard(task_id)
            self._compact()
            return task_id
        return None

    def peek(self) -> str | None:
        for priority, seq, task_id in self._heap:
            if self._entries.get(task_id) == (priority, seq):
                return task_id
        return None

    def remove(self, task_id: str) -> bool:
        entry = self._entries.pop(task_id, None)
        if entry is None:
            return False
        self._removed.add(task_id)
        return True

    def set_priority(self, task_id: str, priority: int) -> bool:
        entry = self._entries.get(task_id)
        if entry is None:
            return False
        priority = int(priority)
        if entry[0] == priority:
            return True
        seq = next(self._counter)
        self._entries[task_id] = (priority, seq)
        heapq.heappush(self._heap, (priority, seq, task_id))
        return True

    # ---- 读 -----------------------------------------------------------
    def __contains__(self, task_id: object) -> bool:
        return task_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def priority_of(self, task_id: str) -> int | None:
        entry = self._entries.get(task_id)
        return None if entry is None else entry[0]

    def ids(self) -> list[str]:
        """按优先级顺序返回（稳定的展示顺序）。"""
        return [tid for tid, _ in sorted(self._entries.items(), key=lambda kv: kv[1])]

    def clear(self) -> None:
        self._heap.clear()
        self._entries.clear()
        self._removed.clear()

    def extend(self, items: Iterable[tuple[str, int]]) -> None:
        for task_id, priority in items:
            self.push(task_id, priority)
