"""连接池：全局并发连接预算 + 复用的 aiohttp 会话。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiohttp


class ConnectionPool:
    """限制并发连接数，并集中管理 DNS 缓存与 TCP 连接复用。

    所有下载协程必须先 ``async with pool.acquire():`` 拿到槽位再发请求；
    这是"低资源占用"的硬约束 —— 连接数不会随任务数无限膨胀。
    """

    def __init__(
        self,
        *,
        limit: int = 32,
        limit_per_host: int = 16,
        connect_timeout: float = 15.0,
        sock_read_timeout: float = 30.0,
        total_timeout: float | None = None,
        user_agent: str = "DownloadX/0.1.0",
    ) -> None:
        self.limit = max(1, limit)
        self.limit_per_host = max(1, limit_per_host)
        self.user_agent = user_agent
        self._semaphore = asyncio.Semaphore(self.limit)
        self._session: aiohttp.ClientSession | None = None
        self._timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=connect_timeout,
            sock_read=sock_read_timeout,
            sock_connect=connect_timeout,
        )
        self._peak_in_use = 0
        self._in_use = 0

    # ---- 生命周期 -----------------------------------------------------
    async def start(self) -> None:
        if self._session is not None:
            return
        connector = aiohttp.TCPConnector(
            limit=self.limit,
            limit_per_host=self.limit_per_host,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=False,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=self._timeout,
            headers={"User-Agent": self.user_agent},
            auto_decompress=False,  # 下载器必须拿到原始字节，否则进度/续传全乱
            trust_env=True,
        )

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()
            await asyncio.sleep(0)  # 给连接回收留一个事件循环周期

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("连接池尚未启动，请先 await pool.start()")
        return self._session

    # ---- 槽位 ---------------------------------------------------------
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        await self._semaphore.acquire()
        self._in_use += 1
        self._peak_in_use = max(self._peak_in_use, self._in_use)
        try:
            yield
        finally:
            self._in_use -= 1
            self._semaphore.release()

    def resize(self, limit: int) -> None:
        """运行时调整连接上限（只影响后续 acquire，不打断进行中的连接）。"""
        limit = max(1, int(limit))
        if limit == self.limit:
            return
        delta = limit - self.limit
        self.limit = limit
        if delta > 0:
            for _ in range(delta):
                self._semaphore.release()
        else:
            loop = asyncio.get_running_loop()
            loop.create_task(self._shrink(-delta))

    async def _shrink(self, count: int) -> None:
        for _ in range(count):
            await self._semaphore.acquire()

    def stats(self) -> dict[str, int]:
        return {
            "limit": self.limit,
            "in_use": self._in_use,
            "available": self.limit - self._in_use,
            "peak_in_use": self._peak_in_use,
        }
