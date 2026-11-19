# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Admit one complete checkpoint preparation/persistence per Worker process."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar

_slots = threading.BoundedSemaphore(1)
_owner = ContextVar("swarm_checkpoint_budget_owner", default=None)


@asynccontextmanager
async def admit_context_checkpoint():
    task = asyncio.current_task()
    if _owner.get() is task:
        yield
        return
    # Never leave a blocking thread acquisition behind on cancellation. The
    # quota spans event loops; inherited ContextVars do not admit child tasks.
    while not _slots.acquire(blocking=False):
        await asyncio.sleep(.01)
    token = _owner.set(task)
    try:
        yield
    finally:
        _owner.reset(token)
        _slots.release()
