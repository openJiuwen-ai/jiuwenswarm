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
        self._released = True
        close = self._kernel._release(self.key, self._entry)
        if close is not None:
            result = close(self._entry.value)
            if inspect.isawaitable(result):
                await result
        await self._kernel._drain_disposals()


class ResourceKernel:
    def __init__(self, *, max_idle: int = 128) -> None:
        self._entries: OrderedDict[ResourceKey, _Entry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_idle = max_idle
        self._preparing: dict[ResourceKey, asyncio.Task[Any]] = {}
        self._versions: dict[ResourceKey, int] = {}
        self._disposals: list[_Entry] = []
        self._active_references = 0
        self._closed = False

    def acquire(self, key: ResourceKey, factory: Callable[[], Any], *,
                close: Callable[[Any], Any] | None = None) -> ResourceLease:
        # Model constructors are synchronous; single-flight construction avoids
        # duplicate connections even when separate preparation threads race.
        with self._lock:
            if self._closed:
                raise RuntimeError("Kernel is closed")
            entry = self._entries.get(key)
            if entry is None or entry.retired:
                value = factory()  # A failed factory is never published.
                entry = _Entry(value, close=close)
                self._entries[key] = entry
            entry.references += 1
            self._active_references += 1
            self._entries.move_to_end(key)
            return ResourceLease(self, key, entry)

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
        lease._released = True
        self._release(key, lease._entry)
        return value

    async def _drain_disposals(self) -> None:
        with self._lock:
            entries, self._disposals = self._disposals, []
        await self._close_entries(entries)

    @staticmethod
    async def _close_entries(entries: list[_Entry]) -> None:
        failures = []
        for entry in entries:
            if entry.close is not None:
                try:
                    result = entry.close(entry.value)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise RuntimeError(f"Kernel resource cleanup failed for {len(failures)} resource(s)") from failures[0]

    async def acquire_async(self, key: ResourceKey, factory: Callable[[], Any], *,
                            close: Callable[[Any], Any] | None = None) -> ResourceLease:
        async def prepare(version: int) -> None:
            value = await factory()
            with self._lock:
                entry = _Entry(value, close=close)
                if self._closed or version != self._versions.get(key, 0):
                    if close is not None:
                        self._disposals.append(entry)
                else:
                    self._entries[key] = entry

        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Kernel is closed")
                entry = self._entries.get(key)
                if entry is not None and not entry.retired:
                    entry.references += 1
                    self._active_references += 1
                    self._entries.move_to_end(key)
                    lease = ResourceLease(self, key, entry)
                    break
                task = self._preparing.get(key)
                if task is None:
                    task = asyncio.create_task(prepare(self._versions.get(key, 0)))
                    self._preparing[key] = task
            try:
                await asyncio.shield(task)
            finally:
                if task.done():
                    with self._lock:
                        if self._preparing.get(key) is task:
                            del self._preparing[key]
            await self._drain_disposals()
        return lease

    async def close(self) -> None:
        """Called by the application owner after all session leases drain."""
        with self._lock:
            if self._active_references:
                raise RuntimeError("Kernel still has active resource leases")
            self._closed = True
            preparing = list(self._preparing.values())
            self._preparing.clear()
        for task in preparing:
            task.cancel()
        if preparing:
            await asyncio.gather(*preparing, return_exceptions=True)
        with self._lock:
            entries = list(self._entries.values()) + self._disposals
            self._entries.clear()
            self._disposals = []
        await self._close_entries(entries)

    def _release(self, key: ResourceKey, entry: _Entry) -> Callable[[Any], Any] | None:
        with self._lock:
            entry.references -= 1
            self._active_references -= 1
            if entry.references == 0 and entry.retired:
                if self._entries.get(key) is entry:
                    del self._entries[key]
                return entry.close
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
