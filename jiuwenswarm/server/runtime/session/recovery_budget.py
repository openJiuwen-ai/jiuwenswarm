"""Admission for complete cold restores, including asynchronous migration."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

_loop = None
_slots = None
_owner = ContextVar("context_recovery_budget_owner", default=None)


def _current_slots():
    global _loop, _slots
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _loop, _slots = loop, asyncio.Semaphore(2)
    return _slots


@asynccontextmanager
async def admit_context_recovery():
    task = asyncio.current_task()
    if _owner.get() is task:
        yield
        return
    slots = _current_slots()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=5)
    except asyncio.TimeoutError as exc:
        raise HistoryReplayError("Context recovery is busy; retry later", code="HISTORY_RESTORE_BUSY") from exc
    token = _owner.set(task)
    try:
        yield
    finally:
        _owner.reset(token)
        slots.release()
