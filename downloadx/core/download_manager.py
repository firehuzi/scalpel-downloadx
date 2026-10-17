"""DownloadManager —— 下载引擎的对外唯一门面。

调用方（CLI / HTTP API / Agent 工具）只跟它打交道：

    manager = DownloadManager(Config.load())
    await manager.start()
    task = await manager.add("https://example.com/big.iso")
    await manager.pause(task.id)
    await manager.stop()

UI、LLM、Web 框架都不在这条链路上 —— 引擎可以完全独立运行。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..network.connection_pool import ConnectionPool
from ..network.http_client import HttpClient
from ..network.network_monitor import NetworkMonitor
from ..network.range_request import ResourceInfo
from ..queue.bandwidth_controller import BandwidthController
from ..queue.task_queue import TaskQueue
from ..storage.checkpoint import CheckpointManager
from ..storage.lock import SingleInstanceLock
from ..storage.metadata_store import MetadataStore
from ..storage.segment_store import SegmentStore
from ..version import APP_NAME, __version__
from .download_task import DownloadTask
from .download_worker import DownloadWorker, WorkerControl
from .events import EventBus
from .metrics import Stats
from .scheduler import Scheduler
from .state_machine import ACTIVE_STATUSES, TaskStatus, is_terminal

log = logging.getLogger("downloadx")


class TaskNotFound(KeyError):
    pass


class InvalidOperation(RuntimeError):
    pass


class DownloadManager:
    def __init__(self, config: Config | None = None, *, bus: EventBus | None = None) -> None:
        self.config = config or Config.load()
        self.config.ensure_dirs()
        self.bus = bus or EventBus()

        self.store = MetadataStore(self.config.state_dir)
        self.pool = ConnectionPool(
            limit=self.config.max_global_connections,
            limit_per_host=self.config.max_connections_per_host,
            connect_timeout=self.config.connect_timeout,
            sock_read_timeout=self.config.sock_read_timeout,
            user_agent=self.config.user_agent,
        )
        self.http = HttpClient(self.pool, chunk_size=self.config.chunk_size)
        self.bandwidth = BandwidthController(self.config.global_speed_limit)
        self.task_queue = TaskQueue(self.bandwidth, self.config.max_concurrent_tasks)
        self.segment_store = SegmentStore(self.config)
        self.checkpointer = CheckpointManager(self.store, self.config.checkpoint_interval)
        self.monitor = NetworkMonitor(
            self.pool,
            probe_urls=self.config.network_probe_urls,
            interval=self.config.network_probe_interval,
        )
        self.lock = SingleInstanceLock(self.config.state_dir)

        self._tasks: dict[str, DownloadTask] = {}
        self._speeds: dict[str, tuple[float, float]] = {}  # task_id -> (speed, ts)
        self._started = False
        self._accepting = True

        self.scheduler = Scheduler(
            config=self.config,
            queue=self.task_queue,
            bus=self.bus,
            runner_factory=self._make_worker,
            get_task=self.get,
            on_finished=self._on_task_finished,
        )
        self.bus.publish("manager_created", None, version=__version__)

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def start(self, *, recover: bool = True) -> None:
        if self._started:
            return
        self.store.ensure()
        await self.pool.start()

        if recover:
            self._recover_tasks()

        await self.checkpointer.start()
        await self.scheduler.start()
        if self.config.network_monitor:
            self.monitor.on_change(self._on_network_change)
            await self.monitor.start()
        self._started = True
        self.bus.publish("manager_started", None, tasks=len(self._tasks))

    async def stop(self) -> None:
        if not self._started:
            return
        self._accepting = False
        await self.monitor.stop()
        await self.scheduler.stop()
        await self.checkpointer.stop()
        self._persist_all()
        await self.pool.close()
        self._started = False
        self.bus.publish("manager_stopped", None)

    async def __aenter__(self) -> "DownloadManager":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # ==================================================================
    # 任务创建
    # ==================================================================
    async def add(
        self,
        url: str,
        *,
        save_dir: str | Path | None = None,
        filename: str | None = None,
        priority: int | None = None,
        connections: int | None = None,
        speed_limit: int = 0,
        headers: dict[str, str] | None = None,
        expected_sha256: str | None = None,
        note: str = "",
        start: bool = True,
    ) -> DownloadTask:
        if not self._accepting:
            raise InvalidOperation("管理器正在关闭，不再接受新任务")
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("只支持 http/https 链接")

        task = DownloadTask.create(
            url,
            save_dir=save_dir or self.config.download_dir,
            filename=filename,
            priority=self.config.default_priority if priority is None else priority,
            connections=connections or self.config.max_connections_per_task,
            speed_limit=speed_limit,
            headers=headers,
            expected_sha256=expected_sha256,
            note=note,
        )
        self._tasks[task.id] = task
        if task.speed_limit:
            self.bandwidth.set_task_rate(task.id, task.speed_limit)
        self.store.save_task(task.to_dict())
        self.bus.publish("task_added", task.id, url=url, filename=task.filename)

        if start:
            self.scheduler.submit(task)
        return task

    async def add_many(self, urls: Iterable[str], **kwargs: Any) -> list[DownloadTask]:
        tasks = []
        for url in urls:
            tasks.append(await self.add(url, **kwargs))
        return tasks

    # ==================================================================
    # 查询
    # ==================================================================
    def get(self, task_id: str) -> DownloadTask | None:
        return self._tasks.get(task_id)

    def require(self, task_id: str) -> DownloadTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task

    def list(
        self,
        *,
        status: str | TaskStatus | None = None,
        order: str = "created",
    ) -> list[DownloadTask]:
        tasks = list(self._tasks.values())
        if status is not None:
            want = TaskStatus(status)
            tasks = [t for t in tasks if t.status is want]
        if order == "priority":
            tasks.sort(key=lambda t: (t.priority, t.created_at))
        elif order == "updated":
            tasks.sort(key=lambda t: t.updated_at, reverse=True)
        else:
            tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def speed_of(self, task_id: str) -> float:
        entry = self._speeds.get(task_id)
        if entry is None:
            return 0.0
        speed, ts = entry
        if time.monotonic() - ts > 5.0:
            return 0.0
        return speed

    def public(self, task: DownloadTask) -> dict[str, Any]:
        speed = self.speed_of(task.id)
        eta = task.eta(speed) if task.is_active else None
        return task.to_public_dict(speed=speed, eta=eta)

    def snapshot(self, *, status: str | None = None, order: str = "created") -> list[dict[str, Any]]:
        return [self.public(t) for t in self.list(status=status, order=order)]

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {str(s): 0 for s in TaskStatus}
        stats = Stats()
        total_speed = 0.0
        for task in self._tasks.values():
            counts[str(task.status)] = counts.get(str(task.status), 0) + 1
            progress = task.progress
            if task.is_active:
                total_speed += self.speed_of(task.id)
            if not task.is_terminal and progress.remaining:
                stats.total_remaining_bytes += progress.remaining
            if task.status is TaskStatus.COMPLETED:
                stats.total_downloaded_bytes += progress.received

        stats.active_tasks = counts.get("downloading", 0) + counts.get("verifying", 0)
        stats.queued_tasks = counts.get("queued", 0)
        stats.paused_tasks = counts.get("paused", 0)
        stats.completed_tasks = counts.get("completed", 0)
        stats.failed_tasks = counts.get("failed", 0)
        stats.canceled_tasks = counts.get("canceled", 0)
        stats.total_tasks = len(self._tasks)
        stats.download_speed = total_speed

        return {
            **stats.to_dict(),
            "status_counts": counts,
            "queue": self.task_queue.stats(),
            "scheduler": self.scheduler.stats(),
            "connections": self.pool.stats(),
            "bandwidth": self.bandwidth.snapshot(),
            "network_online": self.monitor.online,
            "version": __version__,
            "app": APP_NAME,
        }

    def history(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.store.read_history(limit=limit, offset=offset)

    async def probe(self, url: str, headers: dict[str, str] | None = None) -> ResourceInfo:
        if not self._started:
            raise InvalidOperation("管理器未启动")
        return await self.http.probe(url, headers)

    # ==================================================================
    # 控制
    # ==================================================================
    async def pause(self, task_id: str) -> TaskStatus:
        task = self.require(task_id)
        if task.status is TaskStatus.PAUSED:
            return task.status
        if task.status not in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING):
            raise InvalidOperation(f"状态 {task.status} 无法暂停")
        if self.scheduler.is_running(task_id):
            self.scheduler.request_pause(task_id)
            await self._await_status(task, TaskStatus.PAUSED, timeout=3.0)
            return task.status
        # 还在排队：直接从队列里摘掉
        self.scheduler.request_pause(task_id)
        task.transition(TaskStatus.PAUSED)
        self._persist(task)
        self.bus.publish("task_paused", task.id, percent=task.percent, reason="用户暂停")
        return task.status

    async def resume(self, task_id: str) -> TaskStatus:
        task = self.require(task_id)
        if task.status in (TaskStatus.DOWNLOADING, TaskStatus.QUEUED, TaskStatus.VERIFYING):
            return task.status
        if task.status in (TaskStatus.COMPLETED,) and not self._completed_file_missing(task):
            raise InvalidOperation("任务已完成，无需恢复")
        if not task.can_transition(TaskStatus.QUEUED):
            raise InvalidOperation(f"状态 {task.status} 无法恢复")
        task.transition(TaskStatus.QUEUED)
        self._persist(task)
        self.scheduler.submit(task)
        self.bus.publish("task_requeued", task.id, percent=task.percent)
        return task.status

    async def cancel(self, task_id: str) -> TaskStatus:
        task = self.require(task_id)
        if task.status is TaskStatus.CANCELED:
            return task.status
        if task.status is TaskStatus.COMPLETED:
            raise InvalidOperation("已完成的任务无法取消")
        if self.scheduler.is_running(task_id):
            self.scheduler.request_cancel(task_id)
            await self._await_status(task, TaskStatus.CANCELED, timeout=3.0)
            return task.status
        self.scheduler.request_cancel(task_id)
        task.transition(TaskStatus.CANCELED)
        self._persist(task)
        self._record_history(task)
        self.bus.publish("task_canceled", task.id, percent=task.percent, reason="用户取消")
        return task.status

    async def retry(self, task_id: str, *, reset: bool = False) -> TaskStatus:
        task = self.require(task_id)
        if task.is_active:
            raise InvalidOperation("任务正在运行")
        if reset:
            self.segment_store.reset(task.segments)
            if task.mode == "streaming":
                task.segments = []
            task.downloaded_size = 0
            task.sha256 = None
            task.verified = None
            try:
                Path(task.part_path).unlink(missing_ok=True)
            except OSError:
                pass
        if not task.can_transition(TaskStatus.QUEUED):
            # COMPLETED -> QUEUED 已在状态机中放开，这里兜底清理
            pass
        task.retry_count += 1
        task.error = None
        task.error_code = None
        task.transition(TaskStatus.QUEUED)
        self._persist(task)
        self.scheduler.submit(task)
        self.bus.publish("task_retried", task.id, reset=reset, retry_count=task.retry_count)
        return task.status

    async def remove(self, task_id: str, *, delete_file: bool = False) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if self.scheduler.is_running(task_id) or task_id in self.task_queue:
            await self.cancel(task_id)
            await asyncio.sleep(0.05)

        # 临时分片文件始终清理；最终文件按参数决定
        targets = [Path(task.part_path)]
        if delete_file:
            targets.append(Path(task.save_path))
        for path in targets:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._tasks.pop(task_id, None)
        self._speeds.pop(task_id, None)
        self.checkpointer.detach(task_id)
        self.task_queue.remove(task_id)
        self.bandwidth.clear_task(task_id)
        self.store.delete_task(task_id)
        self.bus.publish("task_removed", task_id, delete_file=delete_file)
        return True

    async def set_priority(self, task_id: str, priority: int) -> int:
        task = self.require(task_id)
        task.priority = max(0, min(99, int(priority)))
        task.updated_at = time.time()
        self.scheduler.queue.set_priority(task_id, task.priority)
        self._persist(task)
        self.scheduler.wake()
        self.bus.publish("task_updated", task_id, priority=task.priority)
        return task.priority

    async def set_speed_limit(self, task_id: str, rate: int) -> int:
        task = self.require(task_id)
        task.speed_limit = max(0, int(rate))
        self.bandwidth.set_task_rate(task_id, task.speed_limit)
        self._persist(task)
        self.bus.publish("task_updated", task_id, speed_limit=task.speed_limit)
        return task.speed_limit

    async def set_connections(self, task_id: str, connections: int) -> int:
        task = self.require(task_id)
        if task.is_active:
            raise InvalidOperation("运行中的任务不支持修改连接数，请先暂停")
        task.connections = max(1, min(32, int(connections)))
        if task.total_size and task.resumable and task.mode == "segmented":
            # 重新规划分片：保留已完成部分，重新切分未完成区间
            self._replan_preserving_progress(task)
        self._persist(task)
        self.bus.publish("task_updated", task_id, connections=task.connections)
        return task.connections

    async def set_max_concurrent(self, value: int) -> int:
        self.scheduler.set_max_concurrent(value)
        self.config.save()
        self.bus.publish("config_updated", None, max_concurrent_tasks=self.task_queue.max_concurrent)
        return self.task_queue.max_concurrent

    async def set_global_speed_limit(self, rate: int) -> int:
        self.config.global_speed_limit = max(0, int(rate))
        self.bandwidth.set_global_rate(self.config.global_speed_limit)
        self.config.save()
        self.bus.publish("config_updated", None, global_speed_limit=self.config.global_speed_limit)
        return self.config.global_speed_limit

    async def set_download_dir(self, path: str | Path) -> str:
        target = Path(path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        self.config.download_dir = target
        self.config.save()
        self.bus.publish("config_updated", None, download_dir=str(target))
        return str(target)

    async def pause_all(self) -> int:
        self.task_queue.pause_all()
        count = 0
        for task in list(self._tasks.values()):
            if task.status in (TaskStatus.DOWNLOADING, TaskStatus.QUEUED):
                try:
                    await self.pause(task.id)
                    count += 1
                except InvalidOperation:
                    continue
        self._persist_all()
        return count

    async def resume_all(self) -> int:
        self.task_queue.resume_all()
        count = 0
        for task in list(self._tasks.values()):
            if task.status is TaskStatus.PAUSED:
                try:
                    await self.resume(task.id)
                    count += 1
                except InvalidOperation:
                    continue
        return count

    async def wait_for(self, task_id: str, timeout: float = 300.0) -> TaskStatus:
        """等待任务到达终态或暂停态（CLI / 测试用）。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            task = self.require(task_id)
            if is_terminal(task.status) or task.status is TaskStatus.PAUSED:
                return task.status
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待任务 {task_id} 超时")
            await asyncio.sleep(0.05)

    # ==================================================================
    # 内部
    # ==================================================================
    def _make_worker(self, task: DownloadTask, control: WorkerControl) -> DownloadWorker:
        self.checkpointer.attach(task.id, lambda: task.to_dict())
        self.bandwidth.set_task_rate(task.id, task.speed_limit)
        return DownloadWorker(
            task,
            config=self.config,
            http=self.http,
            bus=self.bus,
            control=control,
            segment_store=self.segment_store,
            bandwidth=self.bandwidth,
            checkpointer=self.checkpointer,
            on_progress=self._on_progress,
        )

    def _on_progress(self, task: DownloadTask, speed: float) -> None:
        self._speeds[task.id] = (speed, time.monotonic())

    async def _await_status(
        self, task: DownloadTask, target: TaskStatus, timeout: float = 2.0
    ) -> TaskStatus:
        """短暂等待状态落到目标值，让 API 返回的状态语义准确。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while task.status is not target and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        return task.status

    def _on_task_finished(self, task: DownloadTask, status: TaskStatus) -> None:
        self._persist(task)
        self.checkpointer.detach(task.id)
        if is_terminal(status):
            self._record_history(task)
        if status is TaskStatus.FAILED:
            self.bus.publish("task_state", task.id, status=str(task.status), error=task.error)

    def _record_history(self, task: DownloadTask) -> None:
        self.store.append_history(
            {
                "id": task.id,
                "url": task.url,
                "filename": task.filename,
                "save_path": task.save_path,
                "status": str(task.status),
                "total_size": task.total_size,
                "downloaded_size": task.downloaded_size,
                "elapsed": round(task.elapsed, 3),
                "sha256": task.sha256,
                "error": task.error,
                "created_at": task.created_at,
                "finished_at": time.time(),
            }
        )

    def _persist(self, task: DownloadTask) -> None:
        try:
            self.store.save_task(task.to_dict())
        except Exception as exc:
            log.warning("保存任务 %s 失败：%s", task.id, exc)

    def _persist_all(self) -> None:
        try:
            self.store.save_tasks([t.to_dict() for t in self._tasks.values()])
        except Exception as exc:
            log.warning("批量保存任务失败：%s", exc)

    def _completed_file_missing(self, task: DownloadTask) -> bool:
        return task.status is TaskStatus.COMPLETED and not Path(task.save_path).exists()

    def _replan_preserving_progress(self, task: DownloadTask) -> None:
        from .segment import plan_segments

        total = task.total_size or 0
        received = task.downloaded_size
        if received <= 0:
            task.segments = plan_segments(
                total, task.connections, self.config.min_segment_size
            )
            return
        # 已完成部分保持为一片，剩余区间重新切分
        from .segment import Segment, SegmentStatus

        done = Segment(index=0, start=0, end=received - 1, received=received)
        done.status = SegmentStatus.DONE
        rest = plan_segments(total - received, task.connections, self.config.min_segment_size)
        for seg in rest:
            seg.start += received
            seg.end += received
        task.segments = [done, *rest]
        for i, seg in enumerate(task.segments):
            seg.index = i

    # ---- 崩溃恢复 -----------------------------------------------------
    def _recover_tasks(self) -> None:
        recovered = 0
        requeued = 0
        for data in self.store.load_all_tasks():
            try:
                task = DownloadTask.from_dict(data)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("跳过损坏的任务记录：%s", exc)
                continue
            self._tasks[task.id] = task

            if task.status in ACTIVE_STATUSES:
                recovered += 1
                task.sync_progress()
                task.error = task.error or "上次运行被中断（崩溃恢复）"
                if self.config.auto_resume and task.can_transition(TaskStatus.QUEUED):
                    task.transition(TaskStatus.QUEUED)
                    requeued += 1
                elif task.can_transition(TaskStatus.PAUSED):
                    task.transition(TaskStatus.PAUSED)
                self._persist(task)
                self.bus.publish(
                    "task_recovered", task.id, percent=task.percent, requeued=task.status is TaskStatus.QUEUED
                )
            elif task.status is TaskStatus.QUEUED:
                requeued += 1
            if task.speed_limit:
                self.bandwidth.set_task_rate(task.id, task.speed_limit)

        if recovered or requeued:
            log.info("崩溃恢复：%s 个中断任务，%s 个重新入队", recovered, requeued)
        for task in self._tasks.values():
            if task.status is TaskStatus.QUEUED:
                self.scheduler.submit(task)

    async def _on_network_change(self, online: bool) -> None:
        self.bus.publish("network_changed", None, online=online)
        if not online:
            self.task_queue.pause_all()
        else:
            self.task_queue.resume_all()
            for task in self._tasks.values():
                if task.status is TaskStatus.QUEUED:
                    self.scheduler.submit(task)
        self.scheduler.wake()

    # ---- 调试工具 -----------------------------------------------------
    def disk_usage(self) -> dict[str, Any]:
        total, used, free = shutil.disk_usage(str(self.config.download_dir))
        return {"total": total, "used": used, "free": free}
