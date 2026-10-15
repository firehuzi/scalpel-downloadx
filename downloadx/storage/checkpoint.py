"""检查点：把内存里的下载进度定期刷到磁盘，用于崩溃恢复。

策略：脏标记 + 定时刷 + 事件触发刷。刷盘在后台批次写，不阻塞下载协程。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .metadata_store import MetadataStore

log = logging.getLogger("downloadx.checkpoint")

SnapshotProvider = Callable[[], dict[str, Any]]


class CheckpointManager:
    def __init__(self, store: MetadataStore, interval: float = 2.0) -> None:
        self.store = store
        self.interval = max(0.2, float(interval))
        self._providers: dict[str, SnapshotProvider] = {}
        self._dirty: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._flushes = 0

    # ---- 注册 ---------------------------------------------------------
    def attach(self, task_id: str, provider: SnapshotProvider) -> None:
        self._providers[task_id] = provider
        self._dirty.add(task_id)

    def detach(self, task_id: str) -> None:
        self._providers.pop(task_id, None)
        self._dirty.discard(task_id)

    def touch(self, task_id: str) -> None:
        if task_id in self._providers:
            self._dirty.add(task_id)

    @property
    def flushes(self) -> int:
        return self._flushes

    # ---- 生命周期 -----------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="downloadx-checkpoint")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self.flush()

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 检查点失败绝不能拖垮下载
                log.warning("检查点刷盘失败: %s", exc)

    # ---- 刷盘 ---------------------------------------------------------
    async def flush(self, task_id: str | None = None, force: bool = False) -> int:
        if task_id is not None:
            targets = [task_id]
        else:
            targets = sorted(self._dirty)
        if not targets:
            return 0

        payloads: list[dict[str, Any]] = []
        for tid in targets:
            provider = self._providers.get(tid)
            if provider is None:
                self._dirty.discard(tid)
                continue
            try:
                payloads.append(provider())
            except Exception as exc:
                log.warning("任务 %s 快照失败: %s", tid, exc)
        if not payloads:
            return 0

        await asyncio.to_thread(self.store.save_tasks, payloads)
        self._flushes += 1
        if task_id is None:
            self._dirty.clear()
        else:
            self._dirty.discard(task_id)
        return len(payloads)
