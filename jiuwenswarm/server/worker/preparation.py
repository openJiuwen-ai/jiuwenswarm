# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Finite optional preparation, gated by the required global Runtime base.

Factories initialize a capability or complete its first warm pass. They must
not await the lifetime of a daemon. Core/config preparation remains with the
Worker lifecycle; registering optional work never makes the core ready.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable

CAPABILITY_STATES = frozenset({"pending", "ready", "disabled", "failed"})
logger = logging.getLogger(__name__)

_loop_registries: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def current_preparation_registry() -> "PreparationRegistry | None":
    """Return the running AgentServer's owner, without creating a server.

    Standalone Process CLI/SDK adapters have no owner and retain their own
    preparation. Weak references prevent a closed loop retaining a Runtime.
    """
    reference = _loop_registries.get(asyncio.get_running_loop())
    registry = reference() if reference is not None else None
    return registry if registry is not None and not registry.closed else None


async def log_event_loop_lag(stage: str, *, interval: float = 0.01) -> None:
    """Observe loop responsiveness without logging each scheduling sample."""
    samples = 0
    maximum = 0.0
    total = 0.0
    try:
        while True:
            due = time.monotonic() + interval
            await asyncio.sleep(interval)
            lag = max(0.0, time.monotonic() - due)
            samples += 1
            maximum = max(maximum, lag)
            total += lag
    finally:
        logger.info("[Worker] event_loop_lag stage=%s samples=%d max_ms=%.3f mean_ms=%.3f",
                    stage, samples, maximum * 1000, total * 1000 / samples if samples else 0)


class CapabilityUnavailableError(RuntimeError):
    """An optional capability is disabled or did not become usable."""


@dataclass(slots=True)
class _Capability:
    factory: Callable[[], Any]
    enabled: bool
    warm: bool
    state: str
    imports: tuple[str, ...] = ()
    offload: bool = False
    requested: bool = False
    task: asyncio.Task[Any] | None = None


class PreparationRegistry:
    """One shielded Future per capability; no Runtime/SDK imports."""

    @property
    def closed(self) -> bool:
        return self._closed

    def __init__(self, *, on_change: Callable[[dict[str, str], bool], None] | None = None,
                 background_concurrency: int = 1) -> None:
        if background_concurrency < 1:
            raise ValueError("background preparation concurrency must be positive")
        self._capabilities: dict[str, _Capability] = {}
        self._minimum_ready = asyncio.Event()
        self._closed = False
        self._on_change = on_change
        self._background_concurrency = background_concurrency
        self._active_background = 0
        self._foreground_tokens: set[object] = set()
        self._admission_changed = asyncio.Event()
        self._lag_task: asyncio.Task[None] | None = None

    def register(self, name: str, factory: Callable[[], Any], *, enabled: bool = True,
                 warm: bool = True, imports: tuple[str, ...] = (), offload: bool = False) -> None:
        if self._closed:
            raise RuntimeError("preparation registry is closed")
        if name in self._capabilities:
            raise ValueError(f"capability already registered: {name}")
        if not name or not callable(factory):
            raise ValueError("capability requires a name and preparation factory")
        self._capabilities[name] = _Capability(
            factory, enabled, warm, "pending" if enabled else "disabled", imports, offload,
        )
        self._notify()
        if self._minimum_ready.is_set() and warm and enabled:
            self.start(name)

    def bind_current_loop(self) -> None:
        """Expose this AgentServer owner to loop-local adapter warmups."""
        _loop_registries[asyncio.get_running_loop()] = weakref.ref(self)

    def snapshot(self) -> dict[str, str]:
        return {name: capability.state for name, capability in self._capabilities.items()}

    @property
    def fully_warm(self) -> bool:
        return not self._closed and self._minimum_ready.is_set() and all(
            capability.state in {"ready", "disabled"}
            for capability in self._capabilities.values() if capability.warm
        )

    @property
    def first_pass_settled(self) -> bool:
        """Finite startup passes ended; failed capabilities are still unusable."""
        return not self._closed and self._minimum_ready.is_set() and all(
            capability.state in {"ready", "disabled", "failed"}
            for capability in self._capabilities.values() if capability.warm
        )

    def mark_minimum_ready(self) -> None:
        """Release preparation after required global core/config work succeeds.

        This barrier releases capability preparation, not the externally
        observed EXECUTION_MIN_READY phase. Request-owned model, safety Rails,
        permission handoff and history must complete at the actual SDK sender
        before the Worker can report that stricter phase.
        """
        if self._closed:
            raise RuntimeError("preparation registry is closed")
        self._minimum_ready.set()
        if self._lag_task is None:
            self._lag_task = asyncio.create_task(log_event_loop_lag("optional_preparation"),
                                                 name="optional-preparation-loop-lag")
        for name, capability in self._capabilities.items():
            if capability.warm and capability.enabled:
                self.start(name)
        self._notify()

    async def wait_minimum_ready(self) -> None:
        await self._minimum_ready.wait()
        if self._closed:
            raise RuntimeError("preparation registry is closed")

    def start(self, name: str) -> asyncio.Task[Any] | None:
        if self._closed:
            raise RuntimeError("preparation registry is closed")
        capability = self._capabilities[name]
        if not capability.enabled:
            return None
        if capability.task is None:
            capability.task = asyncio.create_task(self._prepare(name, capability), name=f"capability-{name}")
            # Warm passes may have no caller. Consume their exception without
            # hiding it from later ensure() callers or the readiness snapshot.
            capability.task.add_done_callback(self._observe_completion)
        return capability.task

    async def ensure(self, name: str) -> Any:
        # First use must not queue behind unrelated network warmups, or behind
        # the foreground pause installed by the requesting execution itself.
        self._capabilities[name].requested = True
        self._admission_changed.set()
        task = self.start(name)
        if task is None:
            raise CapabilityUnavailableError(f"capability is disabled: {name}")
        value = await asyncio.shield(task)
        if value is False:
            raise CapabilityUnavailableError(f"capability preparation failed: {name}")
        return value

    def pause_background(self) -> object:
        """Prioritize request preparation; running async services retain ownership."""
        token = object()
        self._foreground_tokens.add(token)
        return token

    def resume_background(self, token: object) -> None:
        self._foreground_tokens.discard(token)
        self._admission_changed.set()

    async def _admit(self, capability: _Capability) -> bool:
        while True:
            if self._closed:
                raise RuntimeError("preparation registry is closed")
            if capability.requested:
                return False
            if not self._foreground_tokens and self._active_background < self._background_concurrency:
                self._active_background += 1
                return True
            self._admission_changed.clear()
            await self._admission_changed.wait()

    @staticmethod
    def _load_modules(modules: tuple[str, ...]) -> None:
        for module in modules:
            importlib.import_module(module)

    async def _prepare(self, name: str, capability: _Capability) -> Any:
        began = time.monotonic()
        admitted = began
        background_slot = False
        try:
            await self.wait_minimum_ready()
            background_slot = await self._admit(capability)
            admitted = time.monotonic()
            # Modules may run substantial synchronous code before an async
            # factory reaches its first await. Load them off the IPC loop.
            if capability.imports:
                await asyncio.to_thread(self._load_modules, capability.imports)
            value = (await asyncio.to_thread(capability.factory)
                     if capability.offload else capability.factory())
            if inspect.isawaitable(value):
                value = await value
            if value is False:
                # Preserve best-effort background helpers' return contract.
                # Explicit first-use callers still receive an error in ensure.
                capability.state = "failed"
                self._notify()
                return False
            capability.state = "ready"
        except BaseException:
            capability.state = "failed"
            self._notify()
            raise
        finally:
            if background_slot:
                self._active_background -= 1
                self._admission_changed.set()
            logger.info(
                "[Worker] preparation capability=%s queue_ms=%.3f run_ms=%.3f state=%s",
                name, (admitted - began) * 1000,
                (time.monotonic() - admitted) * 1000,
                capability.state,
            )
        self._notify()
        return value

    @staticmethod
    def _observe_completion(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _notify(self) -> None:
        if self.first_pass_settled and self._lag_task is not None:
            self._lag_task.cancel()
        if self._on_change is not None:
            try:
                self._on_change(self.snapshot(), self.fully_warm)
            except Exception:
                logger.warning("capability readiness observer failed", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        self._minimum_ready.set()
        self._admission_changed.set()
        tasks = [capability.task for capability in self._capabilities.values() if capability.task is not None]
        if self._lag_task is not None:
            tasks.append(self._lag_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # An unstarted Task never enters _prepare's cancellation handler.
        for capability in self._capabilities.values():
            if capability.task is not None and capability.task.cancelled():
                capability.state = "failed"
        self._notify()
