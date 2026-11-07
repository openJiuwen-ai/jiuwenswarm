# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own socket transports on one process-local thread, separate from Runtime."""

from __future__ import annotations

import asyncio
import atexit
import threading
from functools import wraps
from typing import Any, Callable, Coroutine, ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is None:
            ready = threading.Event()

            def run() -> None:
                global _loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    tasks = asyncio.all_tasks(loop)
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            _thread = threading.Thread(target=run, name="runtime-ipc-loop", daemon=True)
            _thread.start()
            if not ready.wait(5):
                raise RuntimeError("IPC event loop did not start")
        loop = _loop
        if loop is None:
            raise RuntimeError("IPC event loop is unavailable")
        return loop


async def on_loop(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, _T]) -> _T:
    """Await on the owner loop, propagating cancellation to the submitted work."""
    if asyncio.get_running_loop() is loop:
        return await coro
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except BaseException:
        coro.close()
        raise
    return await asyncio.wrap_future(future)


def ipc_io(func: Callable[_P, Coroutine[Any, Any, _T]]) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Keep connection establishment, authentication and disposal on the I/O loop."""
    @wraps(func)
    async def call(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return await on_loop(_get_loop(), func(*args, **kwargs))

    return call


def _shutdown() -> None:
    loop, thread = _loop, _thread
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2)


atexit.register(_shutdown)
