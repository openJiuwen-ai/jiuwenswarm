# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded attribution of the running business task, without request payloads."""

from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import weakref
from contextlib import contextmanager
from typing import Any, Iterator

_business: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "worker_business_diagnostics", default=None,
)


def _text(value: object, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char if char.isprintable() else "?" for char in value[:limit])


class BusinessTracker:
    """Propagate diagnostic identity to child tasks on the business loop.

    The I/O thread only reads public Task metadata and bounded frame locations.
    This is best-effort diagnostics and never controls a health verdict.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread_id = threading.get_ident()
        self._lock = threading.Lock()
        self._tasks: weakref.WeakKeyDictionary[asyncio.Task, dict[str, str]] = weakref.WeakKeyDictionary()
        self._previous_factory = self._loop.get_task_factory()

        def factory(loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> asyncio.Task:
            # Preserve custom factories and Python 3.11-3.14 Task keyword arguments.
            previous = self._previous_factory
            task = previous(loop, coro, **kwargs) if previous is not None else asyncio.Task(coro, loop=loop, **kwargs)
            context = kwargs.get("context")
            metadata = context.get(_business) if context is not None else _business.get()
            if metadata is not None:
                with self._lock:
                    self._tasks[task] = metadata
            return task

        self._factory = factory
        self._loop.set_task_factory(factory)

    @contextmanager
    def activity(self, metadata: dict[str, str]) -> Iterator[None]:
        token = _business.set(metadata)
        task = asyncio.current_task()
        previous = None
        if task is not None:
            with self._lock:
                previous = self._tasks.get(task)
                self._tasks[task] = metadata
        try:
            yield
        finally:
            _business.reset(token)
            if task is not None:
                with self._lock:
                    if previous is None:
                        self._tasks.pop(task, None)
                    else:
                        self._tasks[task] = previous

    def close(self) -> None:
        if self._loop.get_task_factory() is self._factory:
            self._loop.set_task_factory(self._previous_factory)

    def snapshot(self) -> dict[str, Any]:
        """Read the task actually running on the blocked loop, not all active requests."""
        result: dict[str, Any] = {"operation": "event_loop_callback"}
        task = asyncio.current_task(loop=self._loop)
        if task is not None:
            with self._lock:
                result.update(self._tasks.get(task) or {"operation": "unattributed_task"})
            result["task"] = _text(task.get_name())
        # sys._current_frames is the documented cross-thread deadlock diagnostic
        # API. Never read or serialize locals, arguments, source text or exception values.
        frames = sys._current_frames()  # pylint: disable=protected-access
        frame = frames.get(self._thread_id)
        stack = []
        try:
            for _ in range(16):
                if frame is None:
                    break
                stack.append({"file": _text(frame.f_code.co_filename, 256),
                              "line": frame.f_lineno, "function": _text(frame.f_code.co_name)})
                frame = frame.f_back
        finally:
            del frame, frames
        result["stack"] = stack
        return result


def request_metadata(request: Any, dispatch_id: str) -> dict[str, str]:
    method = request.req_method
    return {"operation": "request", "request_id": _text(request.request_id),
            "session_id": _text(request.session_id), "channel_id": _text(request.channel_id),
            "method": _text(getattr(method, "value", method)), "dispatch_id": _text(dispatch_id)}
