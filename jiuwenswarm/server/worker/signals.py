# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Readiness events the Worker emits toward the Front."""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.server.ipc.protocol import KIND_READINESS
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder

logger = logging.getLogger(__name__)


class RuntimeSignalSink:
    """Sync hooks Agent Runtime already calls. Sends do not block startup."""

    def __init__(self, holder: ConnHolder) -> None:
        self._holder = holder

    def agent_ready(self) -> None:
        self._emit("AGENT_READY", "")

    def warmup_retry(self, reason: str) -> None:
        self._emit("RUNTIME_WARMING", reason)

    def failed(self, reason: str) -> None:
        self._emit("FAILED", reason)

    def degraded(self, reason: str) -> None:
        self._emit("DEGRADED", reason)

    def _emit(self, state: str, reason: str) -> None:
        message = {"kind": KIND_READINESS, "state": state, "reason": reason}
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
