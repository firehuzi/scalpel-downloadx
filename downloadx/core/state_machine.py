"""任务状态机。

状态流转必须显式声明，禁止随意赋值 —— 这是"稳定性优先"的第一道闸门。

    queued ──▶ downloading ──▶ verifying ──▶ completed
      ▲            │  ▲            │
      │            ▼  │            ▼
      │         paused ┘         failed ──▶ queued (重试)
      └──────────  cancel / 重新入队
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Iterable


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    def __str__(self) -> str:  # 便于日志/JSON 输出
        return self.value


class InvalidTransitionError(ValueError):
    """非法状态流转。"""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        super().__init__(f"非法状态流转: {current} -> {target}")
        self.current = current
        self.target = target


#: 已结算状态：不占用任何下载资源，等待用户处置（可再次入队）。
#: 注意 FAILED 也在其中 —— 它虽然可以重试，但已经"停下来了"，
#: 等待逻辑与历史记录都不应该继续等它。
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}
)

#: 正在占用资源的"活跃"状态（崩溃恢复时需要重新排队）。
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DOWNLOADING, TaskStatus.VERIFYING}
)

TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.DOWNLOADING, TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.CANCELED}
    ),
    TaskStatus.DOWNLOADING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.VERIFYING,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
            TaskStatus.QUEUED,
        }
    ),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELED, TaskStatus.FAILED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED, TaskStatus.QUEUED}
    ),
    TaskStatus.COMPLETED: frozenset({TaskStatus.QUEUED}),  # 允许"重新下载"
    TaskStatus.FAILED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.CANCELED}
    ),
    TaskStatus.CANCELED: frozenset({TaskStatus.QUEUED}),  # 允许"复活重下"
}


def is_terminal(status: TaskStatus | str) -> bool:
    return TaskStatus(status) in TERMINAL_STATUSES


def is_active(status: TaskStatus | str) -> bool:
    return TaskStatus(status) in ACTIVE_STATUSES


def can_transition(current: TaskStatus | str, target: TaskStatus | str) -> bool:
    return TaskStatus(target) in TRANSITIONS[TaskStatus(current)]


def allowed_transitions(current: TaskStatus | str) -> Iterable[TaskStatus]:
    return sorted(TRANSITIONS[TaskStatus(current)], key=lambda s: s.value)


class StateMachine:
    """带监听器的状态机；每次成功流转都会通知订阅者。"""

    __slots__ = ("_status", "_listeners")

    def __init__(self, initial: TaskStatus | str = TaskStatus.QUEUED) -> None:
        self._status = TaskStatus(initial)
        self._listeners: list[Callable[[TaskStatus, TaskStatus], None]] = []

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def is_terminal(self) -> bool:
        return self._status in TERMINAL_STATUSES

    def can(self, target: TaskStatus | str) -> bool:
        return can_transition(self._status, target)

    def to(self, target: TaskStatus | str) -> TaskStatus:
        target = TaskStatus(target)
        if target is self._status:
            return self._status
        if not can_transition(self._status, target):
            raise InvalidTransitionError(self._status, target)
        previous, self._status = self._status, target
        for listener in list(self._listeners):
            listener(previous, target)
        return self._status

    def on_change(self, listener: Callable[[TaskStatus, TaskStatus], None]) -> None:
        self._listeners.append(listener)
