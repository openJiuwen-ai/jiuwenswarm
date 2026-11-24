# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Process-local document Runtime ownership, separate from the continuity pool.

All async operations run on a persistent loop. Caller cancellation never cancels
an accepted write; closing the final lease drains it before stopping the runtime.
"""
from __future__ import annotations

import atexit
import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    resource: Any
    fingerprint: str
    portal_key: str = field(default_factory=lambda: uuid4().hex)
    references: int = 0
    closing: bool = False
    startup: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    calls: set[asyncio.Task] = field(default_factory=set)


class _Lease:
    def __init__(self, holder: DocumentRuntimeHolder, key: str, entry: _Entry, security_provider: Any):
        self._holder, self._key, self._entry = holder, key, entry
        self._security_provider = security_provider
        self._client = entry.resource.bind_security(security_provider)
        self._closed = False
        self._calls: set[asyncio.Task] = set()
        self._closing = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def resource_key(self) -> str:
        return self._key

    @property
    def resource_entry(self) -> _Entry:
        return self._entry

    @property
    def portal_runtime_key(self) -> str:
        return self._entry.portal_key

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._client, name)
        if not inspect.iscoroutinefunction(value):
            return value

        async def call(*args: Any, **kwargs: Any) -> Any:
            # Authentication is resolved on the caller thread, once per operation.
            security = self._security_provider()
            client = self._client.bind_security(lambda: security)
            operation = getattr(client, name)
            future = self._holder.submit(self, lambda: operation(*args, **kwargs))
            return await _await_result(future)

        return call

    async def initialize(self) -> None:
        async def start() -> None:
            entry = self._entry
            if entry.startup is None:
                entry.startup = asyncio.create_task(entry.resource.initialize())
            await asyncio.shield(entry.startup)

        try:
            await _await_result(self._holder.submit(self, start))
        except BaseException:
            await self.close()
            raise

    async def run_operation(self, operation: Any) -> Any:
        """Track an accepted operation on the holder's worker loop."""
        task = asyncio.current_task()
        self._calls.add(task)
        self._entry.calls.add(task)
        try:
            return await operation()
        finally:
            self._calls.discard(task)
            self._entry.calls.discard(task)

    async def wait_for_calls(self) -> None:
        if self._calls:
            await asyncio.gather(*tuple(self._calls), return_exceptions=True)

    def begin_close(self, loop: asyncio.AbstractEventLoop) -> Any:
        """Close admission once; the holder must hold its submission lock."""
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.run_coroutine_threadsafe(
                self._holder.release(self), loop,
            )
        return self._closing

    async def close(self) -> None:
        await _await_result(self._holder.close_lease(self))


async def _await_result(future: Any) -> Any:
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return await asyncio.shield(wrapped)


class DocumentRuntimeHolder:
    """One runtime per normalized document root; first configuration wins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._loop = None
        self._thread = None
        self._stopped = False

    def _start_loop(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

        self._thread = threading.Thread(target=run, name="memory-document-runtime", daemon=True)
        self._thread.start()
        ready.wait()

    def acquire(self, config: dict, security_provider: Any, factory: Any) -> _Lease:
        root = config.get("markdown_store", {}).get("default", {}).get("params", {}).get("root")
        if not root:
            raise ValueError("Shared document runtime requires an explicit document root")
        key = os.path.normcase(str(Path(root).expanduser().resolve()))
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()
        with self._lock:
            if self._stopped:
                raise RuntimeError("Document runtime holder has stopped")
            self._start_loop()

            future = asyncio.run_coroutine_threadsafe(
                self._acquire_lease(key, fingerprint, security_provider, factory), self._loop,
            )
        # Waiting for one root must not hold the admission lock for other roots.
        return future.result()

    async def _acquire_lease(
        self, key: str, fingerprint: str, security_provider: Any, factory: Any,
    ) -> _Lease:
        # Another waiter may have created a replacement while cleanup was running.
        while True:
            if self._stopped:
                raise RuntimeError("Document runtime holder has stopped")
            entry = self._entries.get(key)
            if entry is None or not entry.closing:
                break
            await self._wait_for_close(entry)
        created = entry is None
        if created:
            entry = _Entry(factory(), fingerprint)
            self._entries[key] = entry
        elif entry.fingerprint != fingerprint:
            logger.warning(
                "[TeamMemory] shared runtime configuration differs; "
                "first configuration retained"
            )
        try:
            lease = _Lease(self, key, entry, security_provider)
        except BaseException:
            if created:
                await self._close_entry(key, entry)
            raise
        entry.references += 1
        return lease

    @staticmethod
    async def _wait_for_close(entry: _Entry) -> None:
        if entry.cleanup is None:
            raise RuntimeError("Document runtime is still closing")
        try:
            await asyncio.shield(entry.cleanup)
        except Exception as exc:
            raise RuntimeError(
                "Document runtime is still closing after cleanup failure"
            ) from exc

    def submit(self, lease: _Lease, operation: Any) -> Any:
        with self._lock:
            if lease.closed or self._stopped:
                raise RuntimeError("Document runtime lease is closed")
            return asyncio.run_coroutine_threadsafe(
                lease.run_operation(operation), self._loop,
            )

    def close_lease(self, lease: _Lease) -> Any:
        """Serialize closing admission with accepted operations."""
        with self._lock:
            return lease.begin_close(self._loop)

    async def release(self, lease: _Lease) -> None:
        # FIFO submission and closed admission ensure all prior calls are visible here.
        await lease.wait_for_calls()
        entry = lease.resource_entry
        entry.references -= 1
        if entry.references:
            return
        entry.closing = True
        await self._close_entry(lease.resource_key, entry)

    async def _close_entry(self, key: str, entry: _Entry) -> None:
        entry.closing = True
        if entry.cleanup is None:
            entry.cleanup = asyncio.create_task(self._finish_entry(key, entry))
        await asyncio.shield(entry.cleanup)

    async def _finish_entry(self, key: str, entry: _Entry) -> None:
        if entry.calls:
            await asyncio.gather(*tuple(entry.calls), return_exceptions=True)
        if entry.startup is not None:
            await asyncio.gather(entry.startup, return_exceptions=True)
        # Keep a failed close quarantined; never start a second watcher on top of it.
        await entry.resource.close()
        self._entries.pop(key, None)

    def close(self) -> None:
        """Process shutdown fallback; normal owners release their individual leases."""
        with self._lock:
            if self._stopped or self._loop is None:
                return
            self._stopped = True

            async def close_all() -> None:
                results = await asyncio.gather(
                    *(self._close_entry(key, entry) for key, entry in tuple(self._entries.items())),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            future = asyncio.run_coroutine_threadsafe(close_all(), self._loop)
        try:
            future.result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()


DOCUMENT_RUNTIME_HOLDER = DocumentRuntimeHolder()
atexit.register(DOCUMENT_RUNTIME_HOLDER.close)
