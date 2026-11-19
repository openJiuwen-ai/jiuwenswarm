# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fix openjiuwen HybridAsyncReadWriteLock vs filelock 4.x task affinity.

openjiuwen acquires the async file lock inside ``asyncio.create_task`` and may
release it from a different task (the parent waiter, or the last shared reader).
filelock 4.x ``AsyncReadWriteLock`` tracks holds per asyncio Task, so release
then raises::

    Cannot release a lock on ... that is not held by this task

This host patch wraps public read/write/close operations and uses the public
file_lock handle. Local coordination belongs to the wrapper, which never reads
or mutates the core lock's protected implementation state.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Literal
from weakref import WeakKeyDictionary

from filelock import Timeout as FileLockTimeout

_logger = logging.getLogger(__name__)
_installed = False
_originals: dict[str, Any] = {}


class _TaskAffineLockState:
    """Own task coordination while delegating OS locking to a public handle."""

    def __init__(self, file_lock: Any) -> None:
        self.file_lock = file_lock
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.writers_waiting = 0
        self.closed = False
        self.poll_interval = 0.05

    def timeout(self) -> FileLockTimeout:
        return FileLockTimeout(str(self.file_lock.lock_file))

    def ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("Cannot acquire a closed file lock")

    async def wait_for(self, predicate: Callable[[], bool], deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise self.timeout()
        try:
            await asyncio.wait_for(self.condition.wait_for(predicate), timeout=remaining)
        except asyncio.TimeoutError:
            raise self.timeout() from None
        self.ensure_open()

    async def acquire_file(self, mode: Literal["read", "write"], deadline: float) -> None:
        acquire = self.file_lock.acquire_read if mode == "read" else self.file_lock.acquire_write
        while True:
            try:
                await acquire(timeout=0, blocking=False)
                return
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await self.file_lock.release()
                raise
            except FileLockTimeout:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise self.timeout() from None
                await asyncio.sleep(min(self.poll_interval, remaining))

    @asynccontextmanager
    async def read(self, timeout: float):
        deadline = asyncio.get_running_loop().time() + timeout
        async with self.condition:
            self.ensure_open()
            await self.wait_for(lambda: not self.writer and self.writers_waiting == 0, deadline)
            await self.acquire_file("read", deadline)
            self.readers += 1
        try:
            yield
        finally:
            async with self.condition:
                self.readers -= 1
                try:
                    await self.file_lock.release()
                finally:
                    self.condition.notify_all()

    @asynccontextmanager
    async def write(self, timeout: float):
        deadline = asyncio.get_running_loop().time() + timeout
        async with self.condition:
            self.ensure_open()
            self.writers_waiting += 1
            try:
                await self.wait_for(lambda: not self.writer and self.readers == 0, deadline)
                await self.acquire_file("write", deadline)
                self.writer = True
            finally:
                self.writers_waiting -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                try:
                    await self.file_lock.release()
                finally:
                    self.writer = False
                    self.condition.notify_all()

    async def close(self, close_original: Callable[[], Awaitable[None]]) -> None:
        async with self.condition:
            if self.readers or self.writer:
                raise RuntimeError("Cannot close an active file lock")
            if self.closed:
                return
            await close_original()
            self.closed = True
            self.condition.notify_all()


_states: WeakKeyDictionary[Any, _TaskAffineLockState] = WeakKeyDictionary()


def _state_for(lock: Any) -> _TaskAffineLockState:
    state = _states.get(lock)
    if state is None:
        state = _TaskAffineLockState(lock.file_lock)
        _states[lock] = state
    return state


def _needs_patch(cls: type) -> bool:
    if not hasattr(cls, "file_lock"):
        return False
    if getattr(getattr(cls, "read", None), "jiuwenswarm_fs_rwlock_patched", False):
        return False
    try:
        src = inspect.getsource(cls)
    except (OSError, TypeError):
        return True
    return "create_task" in src


def install_fs_rwlock_task_affinity_patch() -> bool:
    """Idempotently patch HybridAsyncReadWriteLock for filelock task affinity."""
    global _installed
    if _installed:
        return True

    try:
        from openjiuwen.core.sys_operation.local._async_read_write_lock import (
            HybridAsyncReadWriteLock,
        )
    except ImportError as exc:
        _logger.warning("[fs_rwlock_patch] skip: cannot import HybridAsyncReadWriteLock: %s", exc)
        return False

    methods = {name: getattr(HybridAsyncReadWriteLock, name, None) for name in ("read", "write", "close")}
    if not hasattr(HybridAsyncReadWriteLock, "file_lock") or not all(callable(method) for method in methods.values()):
        _logger.warning("[fs_rwlock_patch] skip: incompatible public file-lock interface")
        return False

    if not _needs_patch(HybridAsyncReadWriteLock):
        _logger.info("[fs_rwlock_patch] HybridAsyncReadWriteLock already task-safe; skip")
        _installed = True
        return True

    original_close = methods["close"]

    @asynccontextmanager
    async def read(lock: Any, timeout: float):
        async with _state_for(lock).read(timeout):
            yield

    @asynccontextmanager
    async def write(lock: Any, timeout: float):
        async with _state_for(lock).write(timeout):
            yield

    async def close(lock: Any) -> None:
        await _state_for(lock).close(lambda: original_close(lock))

    read.jiuwenswarm_fs_rwlock_patched = True  # type: ignore[attr-defined]

    try:
        HybridAsyncReadWriteLock.read = read  # type: ignore[method-assign]
        HybridAsyncReadWriteLock.write = write  # type: ignore[method-assign]
        HybridAsyncReadWriteLock.close = close  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        for name, original in methods.items():
            if getattr(HybridAsyncReadWriteLock, name) is not original:
                setattr(HybridAsyncReadWriteLock, name, original)
        _logger.warning("[fs_rwlock_patch] skip: cannot replace file-lock methods", exc_info=True)
        return False
    _originals.update(methods)

    _installed = True
    _logger.info("[fs_rwlock_patch] HybridAsyncReadWriteLock task-affinity patch applied")
    return True


def reset_fs_rwlock_patch() -> None:
    """Restore original HybridAsyncReadWriteLock methods (tests only)."""
    global _installed
    if not _originals:
        _installed = False
        return
    try:
        from openjiuwen.core.sys_operation.local._async_read_write_lock import (
            HybridAsyncReadWriteLock,
        )
    except ImportError:
        _originals.clear()
        _installed = False
        return
    for name, original in _originals.items():
        setattr(HybridAsyncReadWriteLock, name, original)
    _originals.clear()
    _states.clear()
    _installed = False


__all__ = ["install_fs_rwlock_task_affinity_patch", "reset_fs_rwlock_patch"]
