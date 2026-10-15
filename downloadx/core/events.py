"""进程内事件总线：引擎发事件，UI / API / 日志订阅。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class Event:
    type: str
    task_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "task_id": self.task_id, "data": self.data, "ts": self.ts}


class EventBus:
    """极简发布订阅。慢订阅者不会阻塞引擎（队列满则丢最旧事件）。"""

    def __init__(self, recent: int = 200, queue_size: int = 512) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._recent: deque[Event] = deque(maxlen=recent)
        self._queue_size = queue_size

    # -- 订阅 -----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def recent(self, limit: int = 50) -> list[Event]:
        if limit <= 0:
            return []
        return list(self._recent)[-limit:]

    # -- 发布 -----------------------------------------------------------
    def publish(self, type: str, task_id: str | None = None, **data: Any) -> Event:
        event = Event(type=type, task_id=task_id, data=data)
        self._recent.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # 丢弃最旧的一条
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event

    def publish_many(self, events: Iterable[Event]) -> None:
        for event in events:
            self.publish(event.type, event.task_id, **event.data)
