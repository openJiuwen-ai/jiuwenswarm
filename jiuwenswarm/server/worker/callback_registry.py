# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the resident Worker's shared SDK registry on its business event loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError, Future
from functools import wraps
import threading
from typing import Any, Callable


_ASYNC_METHODS = (
    "register", "unregister", "unregister_namespace", "unregister_by_tags", "unregister_event",
)
_SYNC_METHODS = (
    "register_sync", "unregister_sync", "add_filter", "add_global_filter", "add_circuit_breaker",
    "add_hook", "enable_event_history", "reset_metrics", "has_subscribers", "get_metrics",
    "get_slow_callbacks", "get_event_history", "list_events", "list_callbacks", "get_statistics",
)


class CallbackRegistryOwner:
    """Delegate registry access from initializer threads to one owning loop.

    Only this Worker-owned framework instance is adapted. Callback execution
    retains the SDK's existing semantics. No SDK private lock is replaced.
    """

    def __init__(self, framework: Any) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.get_ident()
        self._state_lock = threading.Lock()
        self._closed = False
        self._pending: set[Future] = set()
        self._tasks: set[asyncio.Task] = set()
        for name in _ASYNC_METHODS:
            method = getattr(framework, name, None)
            if callable(method):
                setattr(framework, name, self._wrap_async(method))
        for name in _SYNC_METHODS:
            method = getattr(framework, name, None)
            if callable(method):
                setattr(framework, name, self._wrap_sync(method))

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closed or not self._loop.is_running():
                raise RuntimeError("Worker callback registry owner is closed")

    def check_owner_loop(self) -> None:
        """Require an open registry on the loop that originally bound it."""
        self._require_open()
        if self._loop is not asyncio.get_running_loop():
            raise RuntimeError("Worker callback registry already belongs to another event loop")

    def _forget(self, future: Future) -> None:
        with self._state_lock:
            self._pending.discard(future)

    def _wrap_async(self, method: Callable) -> Callable:
        @wraps(method)
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            self._require_open()
            if asyncio.get_running_loop() is self._loop:
                return await method(*args, **kwargs)
            coroutine = self._invoke_async(method, args, kwargs)
            with self._state_lock:
                if self._closed:
                    coroutine.close()
                    raise RuntimeError("Worker callback registry owner is closed")
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
                except RuntimeError:
                    coroutine.close()
                    raise
                self._pending.add(future)
            future.add_done_callback(self._forget)
            return await asyncio.wrap_future(future)

        setattr(invoke, "_worker_callback_registry_owner", self)
        return invoke

    async def _invoke_async(self, method: Callable, args: tuple, kwargs: dict) -> Any:
        self._require_open()
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            return await method(*args, **kwargs)
        finally:
            if task is not None:
                self._tasks.discard(task)

    def _wrap_sync(self, method: Callable) -> Callable:
        @wraps(method)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            self._require_open()
            if threading.get_ident() == self._thread:
                return method(*args, **kwargs)
            future: Future = Future()
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Worker callback registry owner is closed")
                self._pending.add(future)
                try:
                    self._loop.call_soon_threadsafe(self._invoke_sync, future, method, args, kwargs)
                except RuntimeError:
                    self._pending.discard(future)
                    raise
            future.add_done_callback(self._forget)
            while True:
                try:
                    return future.result(timeout=0.25)
                except TimeoutError:
                    if future.done():
                        raise
                    if not self._loop.is_running():
                        future.cancel()
                        raise RuntimeError("Worker callback registry owner is closed") from None
                except CancelledError:
                    raise RuntimeError("Worker callback registry owner is closed") from None

        return invoke

    def _invoke_sync(self, future: Future, method: Callable, args: tuple, kwargs: dict) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            self._require_open()
            result = method(*args, **kwargs)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    async def aclose(self) -> None:
        """Reject late registrations and release callers after Runtime teardown."""
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Worker callback registry must close on its owner loop")
        with self._state_lock:
            self._closed = True
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Let already queued submissions observe closure and settle their
        # concurrent futures before the Worker's event loop is closed.
        await asyncio.sleep(0)


def bind_callback_registry_owner(framework: Any) -> CallbackRegistryOwner:
    """Bind once before extensions and agents can initialize in other threads."""
    method = getattr(framework, "register", None)
    owner = getattr(method, "_worker_callback_registry_owner", None)
    if isinstance(owner, CallbackRegistryOwner):
        owner.check_owner_loop()
        return owner
    return CallbackRegistryOwner(framework)
