# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded queue for execution requests while Runtime is warming."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.front.protocol import encode_response
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_LIMIT = 32
_DEFAULT_WAIT_SECONDS = 120.0


class RuntimeBackend(Protocol):
    async def dispatch_parsed_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        ...

    def attach_gateway_connection(self, ws: Any, send_lock: asyncio.Lock) -> None:
        ...

    async def on_gateway_disconnect(self, ws: Any, remote: Any) -> None:
        ...


class ExecutionAdmission:
    """Hold execution RPCs until the in-process Runtime backend is attached."""

    def __init__(
        self,
        readiness: Readiness,
        *,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        wait_seconds: float = _DEFAULT_WAIT_SECONDS,
    ) -> None:
        self._readiness = readiness
        self._queue_limit = queue_limit
        self._wait_seconds = wait_seconds
        self._backend: RuntimeBackend | None = None
        self._waiting = 0
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> RuntimeBackend | None:
        return self._backend

    def attach_backend(self, backend: RuntimeBackend) -> None:
        self._backend = backend
        self._readiness.mark_agent_ready()

    async def dispatch(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        backend = self._backend
        if backend is None:
            async with self._lock:
                if self._waiting >= self._queue_limit:
                    reject = True
                else:
                    self._waiting += 1
                    reject = False
            if reject:
                await self._reject_warming(ws, request, send_lock)
                return
            try:
                ready = await self._readiness.wait_agent_ready(self._wait_seconds)
            finally:
                async with self._lock:
                    self._waiting -= 1
            if not ready or self._backend is None:
                await self._reject_warming(ws, request, send_lock)
                return
            backend = self._backend
        await backend.dispatch_parsed_request(ws, request, send_lock)

    async def _reject_warming(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": "AgentServer runtime is still warming",
                "code": "RUNTIME_WARMING",
                "readiness": self._readiness.state.value,
            },
            metadata=request.metadata,
        )
        wire = encode_response(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)
