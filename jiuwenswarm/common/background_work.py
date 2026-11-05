# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound optional blocking work without borrowing asyncio or IPC executors."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import copy_context
from typing import TypeVar

T = TypeVar("T")


class BackgroundWorkBusy(RuntimeError):
    """Blocking-work capacity is unavailable; the caller must degrade."""


class BoundedBackgroundWork:
    """Run at most N operations, with no pending queue or unbounded shutdown.

    A filesystem call cannot be cancelled safely in Python. Keep its slot
    until the call actually returns, even after its caller times out. Daemon
    threads let the Worker shutdown deadline remain effective for hung IO.
    Only independent, synchronous read operations belong here.
    """

    def __init__(self, *, max_workers: int, name: str) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._slots = threading.BoundedSemaphore(max_workers)
        self._name = name

    def submit(self, callback: Callable[[], T]) -> Future[T]:
        """Start immediately or reject without allocating another thread."""
        if not self._slots.acquire(blocking=False):
            raise BackgroundWorkBusy("optional background work is busy")
        future: Future[T] = Future()
        context = copy_context()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                self._slots.release()
                return
            try:
                result = context.run(callback)
            except BaseException as exc:
                self._slots.release()
                future.set_exception(exc)
            else:
                self._slots.release()
                future.set_result(result)

        try:
            threading.Thread(target=run, name=self._name, daemon=True).start()
        except Exception as exc:
            self._slots.release()
            raise BackgroundWorkBusy("optional background work could not start") from exc
        except BaseException:
            self._slots.release()
            raise
        return future
