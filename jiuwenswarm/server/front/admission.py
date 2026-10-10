# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded queue for execution requests while Runtime is warming."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import encode_response, runtime_warming_frame
from jiuwenswarm.server.front.request_registry import RequestRegistry, TrackedRequest
from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
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
    """Hold execution RPCs until Runtime reports ``AGENT_READY``."""

    def __init__(
        self,
        readiness: Readiness,
        *,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        wait_seconds: float = _DEFAULT_WAIT_SECONDS,
        registry: RequestRegistry | None = None,
        forwarder: EventForwarder | None = None,
    ) -> None:
        self._readiness = readiness
        self._queue_limit = queue_limit
        self._wait_seconds = wait_seconds
        self._registry = registry or RequestRegistry()
        self._forwarder = forwarder
        self._backend: RuntimeBackend | None = None
        self._waiting = 0
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> RuntimeBackend | None:
        return self._backend

    @property
    def registry(self) -> RequestRegistry:
        return self._registry

    def attach_backend(self, backend: RuntimeBackend) -> None:
        """Attach a dispatch target. Does not mark ``AGENT_READY``."""
        self._backend = backend

    def detach_backend(self) -> None:
        self._backend = None

    async def dispatch(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        if request.req_method == ReqMethod.CHAT_CANCEL:
            await self._dispatch_cancel(ws, request, send_lock)
            return
        state = self._readiness.state
        if state is ReadinessState.DRAINING:
            await self._reject(ws, request, send_lock, "RUNTIME_DRAINING", "AgentServer is shutting down")
            return
        if state is ReadinessState.FAILED:
            await self._reject(ws, request, send_lock, "RUNTIME_FAILED", "AgentServer runtime failed")
            return
        backend = self._backend
        if backend is not None and self._readiness.snapshot()["agent_ready"]:
            tracked = await self._registry.register(request, queued=False)
            try:
                await backend.dispatch_parsed_request(ws, request, send_lock)
            finally:
                await self._registry.complete(tracked.request_id)
            return
        async with self._lock:
            if self._waiting >= self._queue_limit:
                reject = True
            else:
                self._waiting += 1
                reject = False
        if reject:
            await self._reject(
                ws, request, send_lock, "RUNTIME_QUEUE_FULL", "AgentServer runtime queue is full"
            )
            return
        tracked = await self._registry.register(request, queued=True)
        try:
            await self._emit_warming(ws, request, send_lock)
            ready = await self._wait_ready_or_cancel(tracked)
        finally:
            async with self._lock:
                self._waiting -= 1
        if tracked.cancelled.is_set():
            await self._reject(
                ws, request, send_lock, "REQUEST_CANCELLED", "request cancelled while runtime warming"
            )
            await self._registry.complete(tracked.request_id)
            return
        state = self._readiness.state
        if state is ReadinessState.DRAINING:
            await self._registry.complete(tracked.request_id)
            await self._reject(ws, request, send_lock, "RUNTIME_DRAINING", "AgentServer is shutting down")
            return
        if state is ReadinessState.FAILED or not ready or self._backend is None:
            await self._registry.complete(tracked.request_id)
            code = "RUNTIME_FAILED" if state is ReadinessState.FAILED else "RUNTIME_WARMING"
            message = (
                "AgentServer runtime failed"
                if code == "RUNTIME_FAILED"
                else "AgentServer runtime is still warming"
            )
            await self._reject(ws, request, send_lock, code, message)
            return
        await self._registry.mark_dispatched(tracked.request_id)
        try:
            await self._backend.dispatch_parsed_request(ws, request, send_lock)
        finally:
            await self._registry.complete(tracked.request_id)

    async def _dispatch_cancel(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Cancel queued work immediately; forward to Runtime when attached."""
        session_id = str(request.session_id or "").strip()
        cancelled = await self._registry.cancel_session(session_id) if session_id else []
        params = request.params if isinstance(request.params, dict) else {}
        target_id = str(params.get("request_id") or "").strip()
        if target_id:
            tracked = await self._registry.cancel(target_id)
            if tracked is not None and tracked not in cancelled:
                cancelled.append(tracked)
        backend = self._backend
        if backend is not None:
            await backend.dispatch_parsed_request(ws, request, send_lock)
            return
        if cancelled:
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "event_type": "chat.interrupt_result",
                    "success": True,
                    "queued": True,
                    "cancelled": [item.request_id for item in cancelled],
                    "readiness": self._readiness.state.value,
                },
                metadata=request.metadata,
            )
            await self._send(ws, send_lock, encode_response(response, response_id=request.request_id))
            return
        await self._reject(
            ws,
            request,
            send_lock,
            "RUNTIME_WARMING",
            "AgentServer runtime is still warming",
        )

    async def _wait_ready_or_cancel(self, tracked: TrackedRequest) -> bool:
        ready_wait = asyncio.create_task(self._readiness.wait_agent_ready(self._wait_seconds))
        cancel_wait = asyncio.create_task(tracked.cancelled.wait())
        done, pending = await asyncio.wait(
            {ready_wait, cancel_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if tracked.cancelled.is_set():
            return False
        ready_task = next((task for task in done if task is ready_wait), None)
        if ready_task is None:
            return False
        try:
            return bool(ready_task.result())
        except Exception:  # noqa: BLE001
            return False

    async def _emit_warming(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        if not request.is_stream:
            return
        try:
            await self._send(
                ws,
                send_lock,
                runtime_warming_frame(
                    request, readiness_state=self._readiness.state.value
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[Front] failed to emit runtime.warming: request_id=%s",
                request.request_id,
            )

    async def _reject(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        code: str,
        error: str,
    ) -> None:
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={
                "error": error,
                "code": code,
                "readiness": self._readiness.state.value,
            },
            metadata=request.metadata,
        )
        await self._send(ws, send_lock, encode_response(response, response_id=request.request_id))

    async def _send(self, ws: Any, send_lock: asyncio.Lock, payload: dict[str, Any]) -> None:
        if self._forwarder is not None:
            await self._forwarder.send(payload, ws=ws, send_lock=send_lock)
            return
        async with send_lock:
            await send_wire_payload(ws, payload)
