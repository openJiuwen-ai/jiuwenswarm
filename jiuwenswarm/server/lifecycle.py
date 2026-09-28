# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer readiness state machine for the Front / Runtime split."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any


class ReadinessState(str, Enum):
    """Process-visible AgentServer readiness."""

    STARTING = "STARTING"
    TRANSPORT_READY = "TRANSPORT_READY"
    CONTROL_READY = "CONTROL_READY"
    RUNTIME_WARMING = "RUNTIME_WARMING"
    AGENT_READY = "AGENT_READY"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    FAILED = "FAILED"


_TRANSPORT_STATES = frozenset(
    {
        ReadinessState.TRANSPORT_READY,
        ReadinessState.CONTROL_READY,
        ReadinessState.RUNTIME_WARMING,
        ReadinessState.AGENT_READY,
        ReadinessState.DEGRADED,
        ReadinessState.DRAINING,
    }
)
_CONTROL_STATES = frozenset(
    {
        ReadinessState.CONTROL_READY,
        ReadinessState.RUNTIME_WARMING,
        ReadinessState.AGENT_READY,
        ReadinessState.DEGRADED,
        ReadinessState.DRAINING,
    }
)
_WARMING_STATES = frozenset(
    {
        ReadinessState.RUNTIME_WARMING,
        ReadinessState.AGENT_READY,
        ReadinessState.DEGRADED,
    }
)
_AGENT_STATES = frozenset({ReadinessState.AGENT_READY, ReadinessState.DEGRADED})


class Readiness:
    """Advance and snapshot AgentServer readiness.

    Front listen only requires ``TRANSPORT_READY``. Control-plane RPCs become
    available at ``CONTROL_READY``. Execution requests may queue from
    ``RUNTIME_WARMING`` and run when ``AGENT_READY``.
    """

    def __init__(self) -> None:
        self._state = ReadinessState.STARTING
        self._agent_ready = asyncio.Event()
        self._failed_reason: str | None = None
        self._retrying = False

    @property
    def state(self) -> ReadinessState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        transport_ready = self._state in _TRANSPORT_STATES
        control_ready = self._state in _CONTROL_STATES
        runtime_warming = self._state in _WARMING_STATES
        agent_ready = self._state in _AGENT_STATES
        return {
            "state": self._state.value,
            "transport_ready": transport_ready,
            "control_ready": control_ready,
            "runtime_warming": runtime_warming,
            "agent_ready": agent_ready,
            "draining": self._state is ReadinessState.DRAINING,
            "failed": self._state is ReadinessState.FAILED,
            "failed_reason": self._failed_reason if self._state is ReadinessState.FAILED else None,
            "last_error": self._failed_reason,
            "retrying": self._retrying,
            "capabilities": {
                "transport": transport_ready,
                "control": control_ready,
                "execution": agent_ready,
            },
        }

    def mark_transport_ready(self) -> None:
        if self._state in {ReadinessState.FAILED, ReadinessState.DRAINING}:
            return
        self._state = ReadinessState.TRANSPORT_READY

    def mark_control_ready(self) -> None:
        if self._state in {ReadinessState.FAILED, ReadinessState.DRAINING}:
            return
        self._state = ReadinessState.CONTROL_READY

    def mark_runtime_warming(self) -> None:
        if self._state in {ReadinessState.FAILED, ReadinessState.DRAINING}:
            return
        self._state = ReadinessState.RUNTIME_WARMING

    def mark_agent_ready(self) -> None:
        if self._state is ReadinessState.DRAINING:
            return
        self._failed_reason = None
        self._retrying = False
        self._state = ReadinessState.AGENT_READY
        self._agent_ready.set()

    def mark_degraded(self) -> None:
        if self._state is not ReadinessState.AGENT_READY:
            return
        self._state = ReadinessState.DEGRADED
        self._agent_ready.set()

    def mark_draining(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.DRAINING
        self._agent_ready.set()

    def mark_failed(self, reason: str) -> None:
        if self._state is ReadinessState.DRAINING:
            return
        self._state = ReadinessState.FAILED
        self._failed_reason = reason
        self._retrying = False
        self._agent_ready.set()

    def note_warmup_retry(self, reason: str) -> None:
        """Record a retryable Runtime warmup error. Stay in ``RUNTIME_WARMING``.

        Terminal ``FAILED`` is only for paths that stop retrying. Warmup still
        loops, so new connections must keep seeing a warming control plane.
        """
        if self._state in {ReadinessState.FAILED, ReadinessState.DRAINING}:
            return
        if self._state in _AGENT_STATES:
            return
        self._state = ReadinessState.RUNTIME_WARMING
        self._failed_reason = reason
        self._retrying = True

    async def wait_agent_ready(self, timeout: float | None = None) -> bool:
        if self._state in {ReadinessState.FAILED, ReadinessState.DRAINING}:
            return False
        if self._agent_ready.is_set():
            return self._state in _AGENT_STATES
        try:
            await asyncio.wait_for(self._agent_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._state in _AGENT_STATES
