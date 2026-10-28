# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Readiness events the Worker emits toward the Front."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.server.ipc.protocol import KIND_READINESS
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder
from jiuwenswarm.server.worker.preparation import CAPABILITY_STATES

logger = logging.getLogger(__name__)


class RuntimeSignalSink:
    """Sync hooks Agent Runtime already calls. Sends do not block startup."""

    def __init__(self, holder: ConnHolder, *, on_ready: Any = None) -> None:
        self._holder = holder
        self._on_ready = on_ready
        self._agent_ready = False
        self._execution_minimum_ready = False
        self._runtime_phase: str | None = None
        self._capability_readiness: dict[str, str] | None = None
        self._fully_warm = False
        self._state = "RUNTIME_WARMING"
        self._reason = ""
        self._degraded_reason = ""

    def ipc_ready(self) -> None:
        """The IPC listener is usable; this never releases execution admission."""
        if not self._agent_ready:
            self._runtime_phase = "IPC_READY"
            self._emit("RUNTIME_WARMING", "")

    def agent_ready(self) -> None:
        """Compatibility hook for the required execution preparation barrier."""
        self.execution_min_ready()

    def execution_min_ready(self) -> None:
        self._execution_minimum_ready = True
        self.runtime_base_ready()

    def runtime_base_ready(self) -> None:
        """Release legacy admission; request-specific preparation is still pending."""
        first_ready = not self._agent_ready
        self._agent_ready = True
        self._runtime_phase = (
            "FULLY_WARM" if self._fully_warm else "EXECUTION_MIN_READY"
        ) if self._execution_minimum_ready else "IPC_READY"
        if first_ready and self._on_ready is not None:
            asyncio.get_running_loop().create_task(self._on_ready())
        self._emit_execution_state()

    def capabilities(self, readiness: dict[str, str], *, fully_warm: bool = False) -> None:
        """Report optional progress without asserting that the core is ready."""
        self._capability_readiness = {
            name: state
            for name, state in readiness.items()
            if isinstance(name, str) and isinstance(state, str) and state in CAPABILITY_STATES
        }
        self._fully_warm = fully_warm and "failed" not in self._capability_readiness.values()
        if self._agent_ready:
            self._runtime_phase = (
                "FULLY_WARM" if self._fully_warm else "EXECUTION_MIN_READY"
            ) if self._execution_minimum_ready else "IPC_READY"
            self._emit_execution_state()
        else:
            self._emit(self._state, self._reason)

    def fully_warm(self) -> None:
        if not self._execution_minimum_ready:
            raise RuntimeError("execution minimum is not ready")
        if "failed" in (self._capability_readiness or {}).values():
            raise RuntimeError("optional capabilities are not fully warm")
        self._fully_warm = True
        self._runtime_phase = "FULLY_WARM"
        self._emit_execution_state()

    def _emit_execution_state(self) -> None:
        failed = sorted(name for name, state in (self._capability_readiness or {}).items() if state == "failed")
        reason = self._degraded_reason or ("optional capability unavailable: " + ", ".join(failed) if failed else "")
        self._emit("DEGRADED" if reason else "AGENT_READY", reason)

    def warmup_retry(self, reason: str) -> None:
        self._agent_ready = False
        self._execution_minimum_ready = False
        self._runtime_phase = "IPC_READY"
        self._fully_warm = False
        self._degraded_reason = ""
        self._emit("RUNTIME_WARMING", reason)

    def failed(self, reason: str) -> None:
        self._agent_ready = False
        self._execution_minimum_ready = False
        self._runtime_phase = "IPC_READY"
        self._fully_warm = False
        self._emit("FAILED", reason)

    def degraded(self, reason: str) -> None:
        if self._state == "FAILED" and not self._agent_ready:
            self._emit("FAILED", self._reason)
            return
        self._degraded_reason = reason
        self._emit("DEGRADED", reason)

    def _emit(self, state: str, reason: str) -> None:
        message = {"kind": KIND_READINESS, "state": state, "reason": reason,
                   "agent_ready": self._agent_ready}
        self._state, self._reason = state, reason
        if self._runtime_phase is not None:
            message["runtime_phase"] = self._runtime_phase
        if self._capability_readiness is not None:
            message["capability_readiness"] = dict(self._capability_readiness)
        self._holder.remember(message)
        conn = self._holder.conn
        if conn is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_send(conn, message))

    async def _safe_send(self, conn: object, message: dict) -> None:
        send = getattr(conn, "send", None)
        if not callable(send):
            return
        try:
            await send(message)
        except Exception:  # noqa: BLE001
            logger.warning("[Worker] readiness send failed", exc_info=True)
