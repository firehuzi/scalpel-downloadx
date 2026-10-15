"""网络状态监视：断网时暂停调度、恢复后自动重排任务。

默认关闭（``network_monitor = false``）：在受限网络环境下，主动探测容易误判。
打开后会周期性探测 ``network_probe_urls``，连续失败 N 次才判定离线。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import aiohttp

from .connection_pool import ConnectionPool

log = logging.getLogger("downloadx.netmon")

Listener = Callable[[bool], Awaitable[None] | None]


class NetworkMonitor:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        probe_urls: tuple[str, ...] = (),
        interval: float = 15.0,
        timeout: float = 8.0,
        failure_threshold: int = 2,
    ) -> None:
        self.pool = pool
        self.probe_urls = tuple(probe_urls)
        self.interval = max(2.0, float(interval))
        self.timeout = timeout
        self.failure_threshold = max(1, failure_threshold)
        self._online = True
        self._failures = 0
        self._listeners: list[Listener] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def online(self) -> bool:
        return self._online

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _set_online(self, value: bool) -> None:
        if value == self._online:
            return
        self._online = value
        log.info("网络状态变化: %s", "在线" if value else "离线")
        for listener in list(self._listeners):
            try:
                result = listener(value)
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            except Exception as exc:  # 监听器异常不影响监视器
                log.debug("网络监听器异常: %s", exc)

    async def check_now(self) -> bool:
        if not self.probe_urls:
            return True
        for url in self.probe_urls:
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with self.pool.acquire():
                    async with self.pool.session.get(
                        url, timeout=timeout, allow_redirects=True
                    ) as resp:
                        if resp.status < 500:
                            self._failures = 0
                            self._set_online(True)
                            return True
            except (aiohttp.ClientError, TimeoutError, OSError):
                continue
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._set_online(False)
        return False

    async def start(self) -> None:
        if self._running or not self.probe_urls:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="downloadx-netmon")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("网络探测异常: %s", exc)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
