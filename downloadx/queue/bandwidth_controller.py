"""带宽控制：令牌桶限速（全局 + 单任务）。

``rate <= 0`` 表示不限速 —— 此时 ``consume`` 是零开销的直通，不产生任何 sleep，
保证"不加限速就不付额外代价"。
"""

from __future__ import annotations

import asyncio
import time

#: 单次等待消耗的最大字节数：把大 chunk 切片，避免一次 sleep 很久导致暂停/取消不灵敏
MAX_SLICE = 64 * 1024


class TokenBucket:
    def __init__(self, rate: int = 0, burst: int | None = None) -> None:
        self._rate = max(0, int(rate))
        self._burst = max(MAX_SLICE, int(burst if burst is not None else max(self._rate, MAX_SLICE)))
        # 初始余量为 0：限速从第一个字节就生效，不做"先送一整秒"的隐式突发
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def unlimited(self) -> bool:
        return self._rate <= 0

    def set_rate(self, rate: int) -> None:
        rate = max(0, int(rate))
        if rate == self._rate:
            return
        self._rate = rate
        self._burst = max(MAX_SLICE, rate)
        self._tokens = min(self._tokens, float(self._burst))
        self._last = time.monotonic()

    async def consume(self, nbytes: int) -> None:
        if nbytes <= 0 or self.unlimited:
            return
        remaining = nbytes
        while remaining > 0:
            take = min(remaining, MAX_SLICE)
            await self._consume_slice(take)
            remaining -= take

    async def _consume_slice(self, nbytes: int) -> None:
        while True:
            async with self._lock:
                if self.unlimited:
                    return
                now = time.monotonic()
                self._tokens = min(
                    float(self._burst), self._tokens + (now - self._last) * self._rate
                )
                self._last = now
                if self._tokens >= nbytes:
                    self._tokens -= nbytes
                    return
                wait = (nbytes - self._tokens) / self._rate
            await asyncio.sleep(min(max(wait, 0.005), 0.5))


class BandwidthController:
    """全局限速 + 每任务限速。两者同时生效时取更严格的那个。"""

    def __init__(self, global_rate: int = 0) -> None:
        self.global_bucket = TokenBucket(global_rate)
        self._task_buckets: dict[str, TokenBucket] = {}

    # ---- 配置 ---------------------------------------------------------
    @property
    def global_rate(self) -> int:
        return self.global_bucket.rate

    def set_global_rate(self, rate: int) -> None:
        self.global_bucket.set_rate(rate)

    def task_rate(self, task_id: str) -> int:
        bucket = self._task_buckets.get(task_id)
        return bucket.rate if bucket else 0

    def set_task_rate(self, task_id: str, rate: int) -> None:
        if rate <= 0:
            self.clear_task(task_id)
            return
        bucket = self._task_buckets.get(task_id)
        if bucket is None:
            self._task_buckets[task_id] = TokenBucket(rate)
        else:
            bucket.set_rate(rate)

    def clear_task(self, task_id: str) -> None:
        self._task_buckets.pop(task_id, None)

    # ---- 消费 ---------------------------------------------------------
    async def acquire(self, nbytes: int, task_id: str | None = None) -> None:
        if self.global_bucket.unlimited and (
            task_id is None or task_id not in self._task_buckets
        ):
            return
        task_bucket = self._task_buckets.get(task_id) if task_id else None
        if task_bucket is not None and not task_bucket.unlimited:
            await task_bucket.consume(nbytes)
        if not self.global_bucket.unlimited:
            await self.global_bucket.consume(nbytes)

    def snapshot(self) -> dict[str, object]:
        return {
            "global_rate": self.global_rate,
            "task_limits": {
                tid: bucket.rate for tid, bucket in self._task_buckets.items()
            },
        }
