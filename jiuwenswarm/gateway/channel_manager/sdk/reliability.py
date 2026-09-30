# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reliability primitives (E1 connector SDK).

Three standard, independent building blocks:

- :class:`SessionIdPolicy` — one shared rule for conversation ids, instead
  of each adapter inventing its own format.
- :class:`RetryPolicy` — exponential backoff for reconnects.
- :class:`TokenBucketRateLimiter` — respects per-platform API rate limits.
"""

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionIdPolicy:
    """Single source of truth for session id construction.

    Format: ``{platform}:{channel_id}:{thread_id}`` (thread part omitted
    when the platform has no threads).
    """

    separator: str = ":"

    def build(self, platform: str, channel_id: str, thread_id: str | None = None) -> str:
        parts = [platform, channel_id]
        if thread_id:
            parts.append(thread_id)
        return self.separator.join(parts)


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff schedule: base * 2^attempt, capped."""

    base_delay: float = 1.0
    max_delay: float = 60.0
    max_attempts: int = 8

    def delay_for(self, attempt: int) -> float:
        """Delay in seconds before retry number ``attempt`` (0-based)."""
        return min(self.base_delay * (2 ** attempt), self.max_delay)


class TokenBucketRateLimiter:
    """Classic token bucket: ``capacity`` tokens, refilled at ``rate``/second.

    ``await limiter.acquire()`` blocks until a token is available, so a
    burst of sends is smoothed out instead of hitting the platform limit.
    """

    def __init__(self, capacity: int, rate: float) -> None:
        self._capacity = capacity
        self._rate = rate
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            await asyncio.sleep(wait)