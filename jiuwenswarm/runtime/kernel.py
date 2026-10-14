# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker-owned reusable resources. Mutable Agent/session state stays outside.

Keys carry trusted deployment identity and opaque configuration/credential
references. Retiring a resource never closes it under an existing execution.
"""
from __future__ import annotations

import hashlib
import asyncio
import hmac
import inspect
import json
import os
import secrets
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

_IDENTITY_SALT = secrets.token_bytes(32)


def identity_reference(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hmac.new(_IDENTITY_SALT, encoded, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class KernelIdentity:
    owner: str
    data_dir: str
    project_dir: str
    work_mode: str
    agent_definition: str
    capabilities: str
    configuration: str

    @classmethod
    def for_agent(cls, *, project_dir: str, work_mode: str, definition: Any,
                  capabilities: Any, configuration: Any) -> KernelIdentity:
        from jiuwenswarm.common.utils import get_user_workspace_dir
        data_dir = os.path.normcase(str(get_user_workspace_dir().resolve()))
        project_dir = os.path.normcase(os.path.realpath(project_dir)) if project_dir else ""
        # user_id is a routing claim, never an input for the data directory.
        owner = identity_reference((data_dir, os.getenv("JIUWENSWARM_RUNTIME_PROFILE", "desktop")))
        return cls(owner, data_dir, project_dir, work_mode, identity_reference(definition),
                   identity_reference(capabilities), identity_reference(configuration))


@dataclass(frozen=True, slots=True)
class ResourceKey:
    identity: KernelIdentity
    kind: str
    resource_id: str
    routing: str
    credential_ref: str
    recipe: str


@dataclass(slots=True)
class _Entry:
    value: Any
    references: int = 0
    retired: bool = False
    close: Callable[[Any], Any] | None = None
    close_loop: asyncio.AbstractEventLoop | None = None


@dataclass(eq=False, slots=True)
class _Preparation:
    key: ResourceKey
    version: int
    owner_thread: int
    done: Future[None]
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[None] | None = None


class ResourceLease:
    def __init__(self, kernel: ResourceKernel, key: ResourceKey, entry: _Entry) -> None:
        self._kernel = kernel
        self.key = key
        self._entry = entry
        self._released = False

    @property
    def value(self) -> Any:
        if self._released:
            raise RuntimeError("resource lease is released")
        return self._entry.value

    async def release(self) -> None:
        if self._released:
            return
        retired = self.release_reference()
        entries = self._kernel.take_disposals()
        if retired is not None:
            entries.insert(0, retired)
        await self._kernel.close_entries(entries)

    def release_reference(self) -> _Entry | None:
        """Drop this lease once; its owner decides when queued disposal runs."""
        if self._released:
            return None
        self._released = True
        return self._kernel.release_entry(self.key, self._entry)


class ResourceKernel:
    def __init__(self, *, max_idle: int = 128) -> None:
        self._entries: OrderedDict[ResourceKey, _Entry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_idle = max_idle
        # The completion Future is loop-neutral: sync and async callers join
        # the same construction without awaiting another loop's Task.
        self._preparing: dict[ResourceKey, _Preparation] = {}
        self._inflight: set[_Preparation] = set()
        self._versions: dict[ResourceKey, int] = {}
        self._disposals: list[_Entry] = []
        self._active_references = 0
        self._closed = False

    def acquire(self, key: ResourceKey, factory: Callable[[], Any], *,
                close: Callable[[Any], Any] | None = None) -> ResourceLease:
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Kernel is closed")
                entry = self._entries.get(key)
                if entry is not None and not entry.retired:
                    return self._lease_locked(key, entry)
                preparing, created = self._preparation_locked(key)
            if not created:
                # Waiting here on a coroutine owned by this thread/loop would
                # stop that coroutine from completing. Recursive factories have
                # the same problem, so fail clearly rather than deadlocking.
                if preparing.owner_thread == threading.get_ident() and not preparing.done.done():
                    raise RuntimeError("synchronous acquire cannot wait for preparation on its own thread")
                preparing.done.result()
                continue
            try:
                value = factory()
            except BaseException as exc:
                self._fail_preparation(key, preparing, exc)
                raise
            lease = self._publish_preparation(key, preparing, _Entry(value, close=close), reserve=True)
            if lease is not None:
                return lease
            # Retirement raced construction. Its value is queued for disposal;
            # retry the current version, never issue a lease for the old value.

    def _preparation_locked(self, key: ResourceKey, *, loop: asyncio.AbstractEventLoop | None = None
                            ) -> tuple[_Preparation, bool]:
        version = self._versions.get(key, 0)
        preparing = self._preparing.get(key)
        if preparing is not None and preparing.version == version:
            return preparing, False
        preparing = _Preparation(key, version, threading.get_ident(), Future(), loop=loop)
        self._preparing[key] = preparing
        self._inflight.add(preparing)
        return preparing, True

    def _lease_locked(self, key: ResourceKey, entry: _Entry) -> ResourceLease:
        entry.references += 1
        self._active_references += 1
        self._entries.move_to_end(key)
        return ResourceLease(self, key, entry)

    def _finish_preparation_locked(self, key: ResourceKey, preparing: _Preparation) -> None:
        if self._preparing.get(key) is preparing:
            del self._preparing[key]
        self._inflight.discard(preparing)

    def _fail_preparation(self, key: ResourceKey, preparing: _Preparation, error: BaseException) -> None:
        with self._lock:
            self._finish_preparation_locked(key, preparing)
        # Future callbacks can re-enter Kernel methods; notify outside the lock.
        if not preparing.done.done():
            preparing.done.set_exception(error)

    def _publish_preparation(self, key: ResourceKey, preparing: _Preparation, entry: _Entry,
                             *, reserve: bool = False) -> ResourceLease | None:
        lease = None
        with self._lock:
            closed = self._closed
            current = self._entries.get(key)
            if (closed or preparing.version != self._versions.get(key, 0)
                    or self._preparing.get(key) is not preparing):
                if entry.close is not None:
                    self._disposals.append(entry)
            elif current is not None and not current.retired:
                # Both APIs share their flight, but never overwrite a resource
                # that was published by an intervening generation.
                if entry.close is not None:
                    self._disposals.append(entry)
                if reserve:
                    lease = self._lease_locked(key, current)
            else:
                self._entries[key] = entry
                if reserve:
                    lease = self._lease_locked(key, entry)
            self._finish_preparation_locked(key, preparing)
        if closed:
            error = RuntimeError("Kernel is closed")
            preparing.done.set_exception(error)
            if reserve:
                raise error
        else:
            preparing.done.set_result(None)
        return lease

    def retire(self, predicate: Callable[[ResourceKey], bool]) -> None:
        with self._lock:
            for key in self._preparing:
                if predicate(key):
                    self._versions[key] = self._versions.get(key, 0) + 1
            for key, entry in list(self._entries.items()):
                if predicate(key):
                    entry.retired = True
                    if entry.references == 0:
                        del self._entries[key]
                        if entry.close is not None:
                            self._disposals.append(entry)

    def recipe(self, key: ResourceKey, factory: Callable[[], Any]) -> Any:
        """Share immutable definitions/factories, never their stateful instances."""
        lease = self.acquire(key, factory)
        value = lease.value
        lease.release_reference()
        return value

    def take_disposals(self) -> list[_Entry]:
        with self._lock:
            entries, self._disposals = self._disposals, []
        return entries

    async def _drain_disposals(self) -> None:
        entries = self.take_disposals()
        await self.close_entries(entries)

    @staticmethod
    async def _wait_preparation(preparing: _Preparation) -> None:
        # A cancelled observer can outlive its loop. Transport errors as values
        # so an abandoned asyncio wrapper never retains an unhandled exception;
        # live waiters still raise the factory's actual exception.
        signal: Future[BaseException | None] = Future()

        def completed(done: Future[None]) -> None:
            try:
                done.result()
            except BaseException as exc:
                signal.set_result(exc)
            else:
                signal.set_result(None)

        preparing.done.add_done_callback(completed)
        error = await asyncio.shield(asyncio.wrap_future(signal))
        if error is not None:
            raise error

    @staticmethod
    async def close_entries(entries: list[_Entry]) -> None:
        failures = []
        for entry in entries:
            if entry.close is not None:
                try:
                    async def close_owned(resource: _Entry) -> None:
                        result = resource.close(resource.value)
                        if inspect.isawaitable(result):
                            await result

                    owner = entry.close_loop
                    if owner is None or owner is asyncio.get_running_loop():
                        await close_owned(entry)
                    elif owner.is_running():
                        completed = asyncio.run_coroutine_threadsafe(close_owned(entry), owner)
                        await asyncio.wrap_future(completed)
                    else:
                        raise RuntimeError("resource cleanup event loop is unavailable")
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise RuntimeError(f"Kernel resource cleanup failed for {len(failures)} resource(s)") from failures[0]

    async def acquire_async(self, key: ResourceKey, factory: Callable[[], Any], *,
                            close: Callable[[Any], Any] | None = None) -> ResourceLease:
        async def prepare(preparing: _Preparation) -> None:
            try:
                value = await factory()
            except BaseException as exc:
                with self._lock:
                    closed = self._closed
                self._fail_preparation(key, preparing, RuntimeError("Kernel is closed") if closed else exc)
            else:
                self._publish_preparation(key, preparing, _Entry(value, close=close, close_loop=preparing.loop))

        def preparation_finished(task: asyncio.Task[None], preparing: _Preparation) -> None:
            # A Task cancelled before its first step never enters prepare's
            # exception handler. Its waiters must still receive an outcome.
            if task.cancelled():
                error: BaseException = asyncio.CancelledError()
            else:
                error = task.exception() or RuntimeError("resource preparation returned without an outcome")
            if not preparing.done.done():
                with self._lock:
                    closed = self._closed
                self._fail_preparation(key, preparing, RuntimeError("Kernel is closed") if closed else error)

        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Kernel is closed")
                entry = self._entries.get(key)
                if entry is not None and not entry.retired:
                    lease = self._lease_locked(key, entry)
                    break
                preparing, created = self._preparation_locked(key, loop=asyncio.get_running_loop())
                if created:
                    preparing.task = asyncio.create_task(prepare(preparing))
                    preparing.task.add_done_callback(lambda task, flight=preparing: preparation_finished(task, flight))
                elif (preparing.task is asyncio.current_task()
                      or (preparing.loop is None and preparing.owner_thread == threading.get_ident())):
                    raise RuntimeError("recursive resource preparation cannot wait for itself")
            await self._wait_preparation(preparing)
            await self._drain_disposals()
        return lease

    async def close(self) -> None:
        """Called by the application owner after all session leases drain."""
        with self._lock:
            if self._active_references:
                raise RuntimeError("Kernel still has active resource leases")
            self._closed = True
            preparing = list(self._inflight)
        failures = []
        for construction in preparing:
            task, loop = construction.task, construction.loop
            if task is not None and not task.done():
                if loop is asyncio.get_running_loop():
                    task.cancel()
                elif loop is not None and loop.is_running():
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError as exc:
                        failures.append(exc)
                        self._fail_preparation(construction.key, construction, exc)
                else:
                    error = RuntimeError("resource preparation event loop is unavailable")
                    failures.append(error)
                    self._fail_preparation(construction.key, construction, error)
        if preparing:
            # A sync factory cannot be cancelled. Await its actual completion;
            # the application deadline may cancel this wait and force exit.
            # Shield also keeps cancellation from corrupting a shared Future.
            await asyncio.gather(*(self._wait_preparation(item) for item in preparing),
                                 return_exceptions=True)
        with self._lock:
            entries = list(self._entries.values()) + self._disposals
            self._entries.clear()
            self._disposals = []
        try:
            await self.close_entries(entries)
        except Exception as exc:
            failures.append(exc)
        if failures:
            if len(failures) == 1:
                raise failures[0]
            raise RuntimeError(f"Kernel shutdown failed in {len(failures)} step(s)") from failures[0]

    def release_entry(self, key: ResourceKey, entry: _Entry) -> _Entry | None:
        with self._lock:
            entry.references -= 1
            self._active_references -= 1
            if entry.references == 0 and entry.retired:
                if self._entries.get(key) is entry:
                    del self._entries[key]
                return entry if entry.close is not None else None
            idle = [key for key, value in self._entries.items() if value.references == 0]
            for discard in idle[:-self._max_idle] if self._max_idle else idle:
                discarded = self._entries.pop(discard)
                if discarded.close is not None:
                    self._disposals.append(discarded)
            return None

    @property
    def resource_count(self) -> int:
        with self._lock:
            return len(self._entries)


_PROCESS_KERNEL = ResourceKernel()


def process_kernel() -> ResourceKernel:
    return _PROCESS_KERNEL
