# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound synchronous Front queries without blocking IPC consumption."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar
from weakref import ReferenceType, WeakKeyDictionary, ref


_Result = TypeVar("_Result")
_QUERY_CONCURRENCY = 4
_QUERY_SLOTS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, ReferenceType[asyncio.Semaphore]
] = WeakKeyDictionary()
_QUERY_TASKS: set[asyncio.Task] = set()


def _query_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    reference = _QUERY_SLOTS.get(loop)
    slots = reference() if reference is not None else None
    if slots is None:
        slots = asyncio.Semaphore(_QUERY_CONCURRENCY)
        # Active callers/tasks own the semaphore. Avoid retaining closed test loops
        # through a semaphore's own loop reference when all queries have finished.
        _QUERY_SLOTS[loop] = ref(slots)
    return slots


async def run_control_query(query: Callable[[], _Result]) -> _Result:
    """Run disk reads, projections and copies with the caller's ContextVars.

    Cancellation stops waiting, not native file I/O. Keep the admission slot
    until the background task really finishes, including on cancelled requests.
    """
    slots = _query_slots()
    await slots.acquire()
    waiting = asyncio.get_running_loop().create_future()
    try:
        task = asyncio.create_task(asyncio.to_thread(query), name="front-control-query")
    except BaseException:
        slots.release()
        raise
    _QUERY_TASKS.add(task)

    def completed(finished: asyncio.Task[_Result]) -> None:
        _QUERY_TASKS.discard(finished)
        slots.release()
        if finished.cancelled():
            waiting.cancel()
            return
        # Retrieve failures even when the requesting coroutine was cancelled.
        error = finished.exception()
        if not waiting.done():
            if error is not None:
                waiting.set_exception(error)
            else:
                waiting.set_result(finished.result())

    task.add_done_callback(completed)
    # Cancelling this waiter must not cancel the native I/O or release its slot.
    return await waiting
