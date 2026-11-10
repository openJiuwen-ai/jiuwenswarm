"""Admission for complete cold restores, including asynchronous migration."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

_slots = threading.BoundedSemaphore(2)
_owner = ContextVar("context_recovery_budget_owner", default=None)


@asynccontextmanager
async def admit_context_recovery():
    task = asyncio.current_task()
    if _owner.get() is task:
        yield
        return
    # Nonblocking acquisition shares the process quota across event loops and
    # leaves no background acquire behind when a waiting task is cancelled.
    deadline = asyncio.get_running_loop().time() + 5
    while not _slots.acquire(blocking=False):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HistoryReplayError("Context recovery is busy; retry later", code="HISTORY_RESTORE_BUSY")
        await asyncio.sleep(min(.01, remaining))
    token = _owner.set(task)
    try:
        yield
    finally:
        _owner.reset(token)
        _slots.release()
