"""FIFO admission for complete cold restores across all event loops in a worker."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayCursor, HistoryReplayError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyLimitSource:
    session_id: str
    before_request_id: str
    cursor: HistoryReplayCursor


class LegacyGroupLimit(HistoryReplayError):
    """The persisted record or indivisible tool group exceeds a fixed migration limit."""

    def __init__(self, source: LegacyLimitSource | None = None):
        super().__init__("A legacy tool group exceeds the migration budget", code="HISTORY_RESTORE_LIMIT")
        self.source = source


@dataclass
class _AssemblyResult:
    source: LegacyLimitSource | None = None


_assemblies: dict[asyncio.Lock, _AssemblyResult] = {}
_assemblies_lock = threading.Lock()


@asynccontextmanager
async def serialize_adapter_assembly(lock: asyncio.Lock):
    """Only callers already waiting on this assembly can observe its structural failure."""
    with _assemblies_lock:
        predecessor = _assemblies.get(lock)
    async with lock:
        current = _AssemblyResult()
        with _assemblies_lock:
            _assemblies[lock] = current
        try:
            yield (predecessor.source if predecessor is not None else None), current
        finally:
            with _assemblies_lock:
                del _assemblies[lock]


class _RecoveryBudget:
    def __init__(self, capacity: int, max_waiters: int, wait_seconds: float) -> None:
        self._capacity = capacity
        self._max_waiters = max_waiters
        self._wait_seconds = wait_seconds
        self._active = 0
        self._waiters: deque[Future] = deque()
        self._lock = threading.Lock()

    def _release_locked(self) -> None:
        if self._waiters:
            # Assign the slot before waking the oldest waiter's event loop.
            self._waiters.popleft().set_result(None)
        else:
            self._active -= 1

    async def acquire(self, session_id: str | None) -> None:
        with self._lock:
            if self._active < self._capacity:
                self._active += 1
                return
            if len(self._waiters) >= self._max_waiters:
                raise HistoryReplayError("Context recovery queue is full; retry later", code="HISTORY_RESTORE_BUSY")
            waiter = Future()
            self._waiters.append(waiter)
            position = len(self._waiters)

        started = time.monotonic()
        logger.info("Context recovery queued: session=%s position=%d", session_id, position)
        try:
            async with asyncio.timeout(self._wait_seconds):
                await asyncio.shield(asyncio.wrap_future(waiter))
        except (TimeoutError, asyncio.CancelledError) as exc:
            with self._lock:
                if waiter.done():
                    # Cancellation/expiry raced with a grant signalled from another loop.
                    self._release_locked()
                else:
                    self._waiters.remove(waiter)
                    waiter.cancel()
            logger.info("Context recovery left queue: session=%s reason=%s waited_ms=%.1f",
                        session_id, type(exc).__name__, (time.monotonic() - started) * 1000)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HistoryReplayError("Context recovery queue wait expired; retry later",
                                     code="HISTORY_RESTORE_BUSY") from exc
        logger.info("Context recovery admitted: session=%s waited_ms=%.1f",
                    session_id, (time.monotonic() - started) * 1000)

    def release(self) -> None:
        with self._lock:
            self._release_locked()


_budget = _RecoveryBudget(capacity=2, max_waiters=16, wait_seconds=60)
_owner = ContextVar("context_recovery_budget_owner", default=None)


@asynccontextmanager
async def admit_context_recovery(session_id: str | None = None):
    task = asyncio.current_task()
    if _owner.get() is task:
        yield
        return
    budget = _budget
    await budget.acquire(session_id)
    token = _owner.set(task)
    started = time.monotonic()
    try:
        logger.info("Context recovery started: session=%s", session_id)
        yield
    finally:
        _owner.reset(token)
        budget.release()
        logger.info("Context recovery slot released: session=%s elapsed_ms=%.1f",
                    session_id, (time.monotonic() - started) * 1000)
