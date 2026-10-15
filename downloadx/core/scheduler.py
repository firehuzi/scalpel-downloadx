"""调度器：决定"现在跑哪几个任务"，并管理每个任务的运行轮次。

它是引擎里唯一知道"并发上限"的地方：

* 任务队列出队 -> 拿到运行名额 -> 起一个运行轮次
* 暂停/取消 -> 给运行中的轮次发信号（协作式停止，保住已下载数据）
* 轮次结束 -> 结算状态、通知外部、唤醒主循环
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..config import Config
from ..queue.task_queue import TaskQueue
from .download_task import DownloadTask
from .download_worker import CancelRequested, DownloadWorker, PauseRequested, WorkerControl
from .events import EventBus
from .state_machine import TaskStatus

log = logging.getLogger("downloadx.scheduler")

RunnerFactory = Callable[[DownloadTask, WorkerControl], DownloadWorker]


class Scheduler:
    def __init__(
        self,
        *,
        config: Config,
        queue: TaskQueue,
        bus: EventBus,
        runner_factory: RunnerFactory,
        get_task: Callable[[str], DownloadTask | None],
        on_finished: Callable[[DownloadTask, TaskStatus], None],
    ) -> None:
        self.config = config
        self.queue = queue
        self.bus = bus
        self.runner_factory = runner_factory
        self.get_task = get_task
        self.on_finished = on_finished

        self._controls: dict[str, WorkerControl] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._running = False
        self._started_tasks = 0
        self._finished_tasks = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._loop(), name="downloadx-scheduler")

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        self._running = False
        self.wake()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None

        # 让运行中的任务优雅停下（保住进度），超时则强制取消
        for control in list(self._controls.values()):
            control.request_pause()
        if self._runners:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._runners.values()), return_exceptions=True),
                    timeout=drain_timeout,
                )
            except asyncio.TimeoutError:
                for runner in list(self._runners.values()):
                    runner.cancel()
                await asyncio.gather(*list(self._runners.values()), return_exceptions=True)
        self._controls.clear()
        self._runners.clear()

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------
    def submit(self, task: DownloadTask) -> None:
        """把任务放进待跑队列。"""
        self.queue.push(task.id, task.priority)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def is_running(self, task_id: str) -> bool:
        return task_id in self._runners

    @property
    def running_ids(self) -> list[str]:
        return list(self._runners)

    def control_of(self, task_id: str) -> WorkerControl | None:
        return self._controls.get(task_id)

    def request_pause(self, task_id: str) -> bool:
        control = self._controls.get(task_id)
        if control is None:
            if task_id in self.queue:
                self.queue.remove(task_id)
                return True
            return False
        control.request_pause()
        return True

    def request_cancel(self, task_id: str) -> bool:
        control = self._controls.get(task_id)
        if control is None:
            if task_id in self.queue:
                self.queue.remove(task_id)
                return True
            return False
        control.request_cancel()
        return True

    async def wait_idle(self, timeout: float = 30.0) -> bool:
        """等待所有运行中的轮次结束（测试 / 优雅退出用）。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._runners and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return not self._runners

    def set_max_concurrent(self, value: int) -> None:
        self.queue.max_concurrent = max(1, int(value))
        self.config.max_concurrent_tasks = self.queue.max_concurrent
        self.wake()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        while self._running:
            try:
                spawned = self._spawn_ready()
                if not spawned and not self.queue.can_start():
                    pass
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 调度循环必须永远活着
                log.exception("调度循环异常：%s", exc)
                await asyncio.sleep(0.5)

    def _spawn_ready(self) -> int:
        spawned = 0
        while self.queue.can_start():
            task_id = self.queue.pop()
            if task_id is None:
                break
            task = self.get_task(task_id)
            if task is None:
                continue
            self._spawn(task)
            spawned += 1
        return spawned

    def _spawn(self, task: DownloadTask) -> None:
        control = WorkerControl()
        self._controls[task.id] = control
        self.queue.mark_active(task.id)
        runner = asyncio.create_task(self._run(task, control), name=f"dx-task-{task.id}")
        self._runners[task.id] = runner
        self._started_tasks += 1

    async def _run(self, task: DownloadTask, control: WorkerControl) -> None:
        final_status = task.status
        try:
            if not task.can_transition(TaskStatus.DOWNLOADING):
                raise RuntimeError(f"状态 {task.status} 不允许开始下载")
            task.transition(TaskStatus.DOWNLOADING)
            self.bus.publish("task_queued_to_run", task.id, priority=task.priority)

            worker = self.runner_factory(task, control)
            await worker.run()

            if control.cancel.is_set():
                final_status = task.transition(TaskStatus.CANCELED)
            elif control.pause.is_set():
                final_status = task.transition(TaskStatus.PAUSED)
            else:
                final_status = task.status

        except asyncio.CancelledError:
            if not task.is_terminal:
                try:
                    final_status = task.transition(TaskStatus.PAUSED)
                    task.error = task.error or "进程退出，任务已暂停"
                except Exception:
                    final_status = task.status
            raise
        except (PauseRequested,):
            final_status = task.transition(TaskStatus.PAUSED)
        except (CancelRequested,):
            final_status = task.transition(TaskStatus.CANCELED)
        except Exception as exc:
            message = getattr(task, "error", None) or f"{type(exc).__name__}: {exc}"
            try:
                final_status = task.transition(TaskStatus.FAILED, error=message)
            except Exception:
                final_status = task.status
            task.error = message
            log.warning("任务 %s 失败：%s", task.id, message)
        finally:
            self._runners.pop(task.id, None)
            self._controls.pop(task.id, None)
            self.queue.mark_idle(task.id)
            self._finished_tasks += 1
            try:
                self.on_finished(task, final_status)
            except Exception as exc:
                log.exception("任务收尾回调异常：%s", exc)
            self.wake()

    def stats(self) -> dict[str, int]:
        return {
            "running": len(self._runners),
            "started_total": self._started_tasks,
            "finished_total": self._finished_tasks,
        }
