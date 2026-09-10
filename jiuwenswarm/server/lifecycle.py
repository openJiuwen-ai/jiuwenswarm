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
    FAILED = "FAILED"


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

    @property
    def state(self) -> ReadinessState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "transport_ready": self._state
            in {
                ReadinessState.TRANSPORT_READY,
                ReadinessState.CONTROL_READY,
                ReadinessState.RUNTIME_WARMING,
                ReadinessState.AGENT_READY,
                ReadinessState.DEGRADED,
            },
            "control_ready": self._state
            in {
                ReadinessState.CONTROL_READY,
                ReadinessState.RUNTIME_WARMING,
                ReadinessState.AGENT_READY,
                ReadinessState.DEGRADED,
            },
            "runtime_warming": self._state
            in {
                ReadinessState.RUNTIME_WARMING,
                ReadinessState.AGENT_READY,
                ReadinessState.DEGRADED,
            },
            "agent_ready": self._state
            in {ReadinessState.AGENT_READY, ReadinessState.DEGRADED},
            "failed_reason": self._failed_reason,
        }

    def mark_transport_ready(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.TRANSPORT_READY

    def mark_control_ready(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.CONTROL_READY

    def mark_runtime_warming(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.RUNTIME_WARMING

    def mark_agent_ready(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.AGENT_READY
        self._agent_ready.set()

    def mark_degraded(self) -> None:
        if self._state is ReadinessState.FAILED:
            return
        self._state = ReadinessState.DEGRADED
        self._agent_ready.set()

    def mark_failed(self, reason: str) -> None:
        self._state = ReadinessState.FAILED
        self._failed_reason = reason
        self._agent_ready.set()

    async def wait_agent_ready(self, timeout: float | None = None) -> bool:
        if self._agent_ready.is_set():
            return self._state is not ReadinessState.FAILED
        try:
            await asyncio.wait_for(self._agent_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self._state is not ReadinessState.FAILED
