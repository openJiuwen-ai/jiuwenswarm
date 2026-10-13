# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Virtual Gateway socket. Worker writes land on IPC, never on the Front port."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from jiuwenswarm.server.ipc.protocol import KIND_WIRE
from jiuwenswarm.server.ipc.stream import IpcConnection

logger = logging.getLogger(__name__)

_DELIVERED: ContextVar[list[bool] | None] = ContextVar("runtime_ipc_delivered", default=None)
_REQUEST_GENERATION: ContextVar[int | None] = ContextVar("runtime_ipc_generation", default=None)


class ConnHolder:
    """Current supervisor connection. Rebind does not reload the Runtime."""

    def __init__(self) -> None:
        self.conn: IpcConnection | None = None
        self.generation = 0
        self.last_readiness: dict[str, Any] | None = None

    def open_session(self, conn: IpcConnection) -> int:
        """Bind a new supervisor connection and return its generation."""
        self.generation += 1
        self.conn = conn
        return self.generation

    def close_session(self) -> int:
        """Detach the current connection without reusing its generation."""
        generation = self.generation
        self.conn = None
        return generation

    def remember(self, message: dict[str, Any]) -> None:
        self.last_readiness = message


class WorkerGatewayBridge:
    """Object passed to Agent Runtime wherever it used to hold a WebSocket."""

    def __init__(self, holder: ConnHolder) -> None:
        self._holder = holder
        self.remote_address = ("runtime-worker", 0)

    @property
    def holder(self) -> ConnHolder:
        return self._holder

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        payload = json.loads(message)
        request_id = ""
        if isinstance(payload, dict):
            request_id = str(payload.get("request_id") or "")
        generation = _REQUEST_GENERATION.get()
        current = self._holder.generation
        if generation is not None and generation != current:
            logger.info(
                "[Worker] dropped stale wire request_id=%s generation=%s current=%s",
                request_id,
                generation,
                current,
            )
            return
        if generation is None:
            generation = current
        flag = _DELIVERED.get()
        if flag is not None:
            flag.append(True)
        conn = self._holder.conn
        if conn is None:
            raise ConnectionError("runtime worker has no supervisor connection")
        await conn.send(
            {
                "kind": KIND_WIRE,
                "request_id": request_id,
                "generation": generation,
                "payload": payload,
            }
        )

    async def close(self) -> None:
        return None


def begin_delivery_tracking() -> Any:
    box: list[bool] = []
    return _DELIVERED.set(box), box


def end_delivery_tracking(token: Any) -> None:
    _DELIVERED.reset(token)


def begin_request_generation(generation: int) -> Any:
    return _REQUEST_GENERATION.set(generation)


def end_request_generation(token: Any) -> None:
    _REQUEST_GENERATION.reset(token)
