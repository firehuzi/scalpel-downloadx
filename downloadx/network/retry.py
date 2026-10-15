"""重试策略：指数退避 + 抖动，区分"可重试"与"永久失败"。"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger("downloadx.retry")

T = TypeVar("T")

#: 值得重试的 HTTP 状态码（服务端临时问题 / 限流 / 超时）
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})

#: 重试也没用的状态码（客户端问题 / 资源不存在 / 鉴权失败）
PERMANENT_STATUS: frozenset[int] = frozenset({400, 401, 403, 404, 405, 410, 416, 451})


class HttpStatusError(Exception):
    def __init__(self, status: int, url: str, reason: str = "") -> None:
        super().__init__(f"HTTP {status} {reason}".strip() + f" ({url})")
        self.status = status
        self.url = url
        self.reason = reason

    @property
    def retryable(self) -> bool:
        if self.status in PERMANENT_STATUS:
            return False
        return self.status in RETRYABLE_STATUS or self.status >= 500


class RetryExhausted(Exception):
    """重试次数耗尽，附带最后一次的原始异常。"""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"重试 {attempts} 次后仍失败: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


def is_retryable(exc: BaseException) -> bool:
    """默认判定：HTTP 永久错误不重试，其余网络异常都重试。"""
    if isinstance(exc, HttpStatusError):
        return exc.retryable
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return False
    return True


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    factor: float = 2.0
    jitter: float = 0.25  # 0~1，抖动比例

    @classmethod
    def from_config(cls, config) -> "RetryPolicy":
        return cls(
            max_attempts=max(1, int(config.max_retries)),
            base_delay=float(config.retry_base_delay),
            max_delay=float(config.retry_max_delay),
        )

    def delay_for(self, attempt: int) -> float:
        """attempt 从 1 开始计数。"""
        raw = min(self.max_delay, self.base_delay * (self.factor ** max(0, attempt - 1)))
        if self.jitter <= 0:
            return raw
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    should_retry: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 需要原样记录最后一次异常
            last_error = exc
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise RetryExhausted(attempt, exc) from exc
            delay = policy.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            log.debug("第 %s 次重试，%.2fs 后：%s", attempt, delay, exc)
            await sleep(delay)
    raise RetryExhausted(policy.max_attempts, last_error or RuntimeError("未知错误"))
