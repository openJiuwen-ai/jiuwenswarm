# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Finite optional preparation, gated by the required global Runtime base.

Factories initialize a capability or complete its first warm pass. They must
not await the lifetime of a daemon. Core/config preparation remains with the
Worker lifecycle; registering optional work never makes the core ready.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

CAPABILITY_STATES = frozenset({"pending", "ready", "disabled", "failed"})
logger = logging.getLogger(__name__)


class CapabilityUnavailableError(RuntimeError):
    """An optional capability is disabled or did not become usable."""


@dataclass(slots=True)
class _Capability:
    factory: Callable[[], Any]
    enabled: bool
    warm: bool
    state: str
    task: asyncio.Task[Any] | None = None


class PreparationRegistry:
    """One shielded Future per capability; no Runtime/SDK imports."""

    def __init__(self, *, on_change: Callable[[dict[str, str], bool], None] | None = None) -> None:
        self._capabilities: dict[str, _Capability] = {}
        self._minimum_ready = asyncio.Event()
        self._closed = False
        self._on_change = on_change

    def register(self, name: str, factory: Callable[[], Any], *, enabled: bool = True,
                 warm: bool = True) -> None:
        if self._closed:
            raise RuntimeError("preparation registry is closed")
        if name in self._capabilities:
            raise ValueError(f"capability already registered: {name}")
        if not name or not callable(factory):
            raise ValueError("capability requires a name and preparation factory")
        self._capabilities[name] = _Capability(factory, enabled, warm, "pending" if enabled else "disabled")
        self._notify()
        if self._minimum_ready.is_set() and warm and enabled:
            self.start(name)

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
        task = self.start(name)
        if task is None:
            raise CapabilityUnavailableError(f"capability is disabled: {name}")
        value = await asyncio.shield(task)
        if value is False:
            raise CapabilityUnavailableError(f"capability preparation failed: {name}")
        return value

    async def _prepare(self, name: str, capability: _Capability) -> Any:
        try:
            await self.wait_minimum_ready()
            value = capability.factory()
            if inspect.isawaitable(value):
                value = await value
            if value is False:
                # Preserve best-effort background helpers' return contract.
                # Explicit first-use callers still receive an error in ensure.
                capability.state = "failed"
                self._notify()
                return False
        except BaseException:
            capability.state = "failed"
            self._notify()
            raise
        capability.state = "ready"
        self._notify()
        return value

    @staticmethod
    def _observe_completion(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.snapshot(), self.fully_warm)
            except Exception:
                logger.warning("capability readiness observer failed", exc_info=True)

    async def close(self) -> None:
        self._closed = True
        self._minimum_ready.set()
        tasks = [capability.task for capability in self._capabilities.values() if capability.task is not None]
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
