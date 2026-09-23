# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket connection accept, origin check, and per-connection dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_allowed_browser_origin,
    is_origin_check_enabled,
)
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import connection_ack_frame, parse_raw_message
from jiuwenswarm.server.front.router import MethodRouter
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)


async def process_handshake(*args: Any) -> Any:
    path, request_headers = extract_handshake_request(args)
    origin = get_header_value(request_headers, "Origin")
    enable_origin_check = is_origin_check_enabled()
    if not enable_origin_check:
        return None
    allowed = is_allowed_browser_origin(origin)
    if allowed:
        return None
    logger.warning(
        "[Front] 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
        path,
        origin,
    )
    return forbidden_origin_response(args)


class ConnectionHandler:
    def __init__(
        self,
        readiness: Readiness,
        router: MethodRouter,
        admission: ExecutionAdmission,
        *,
        forwarder: EventForwarder | None = None,
    ) -> None:
        self._readiness = readiness
        self._router = router
        self._admission = admission
        self._forwarder = forwarder
        self.current_ws: Any = None
        self.current_send_lock: asyncio.Lock | None = None

    async def __call__(self, ws: Any) -> None:
        remote = ws.remote_address
        logger.info("[Front] 新连接: %s", remote)
        send_lock = asyncio.Lock()
        self.current_ws = ws
        self.current_send_lock = send_lock
        if self._forwarder is not None:
            self._forwarder.attach(ws, send_lock)
        backend = self._admission.backend
        if backend is not None:
            backend.attach_gateway_connection(ws, send_lock)
        try:
            ack = connection_ack_frame(
                readiness_state=self._readiness.state.value,
                heartbeat_job_ready=self._readiness.state.value
                in {"AGENT_READY", "DEGRADED"},
            )
            await send_wire_payload(ws, ack)
            logger.info("[Front] 已发送 connection.ack: %s", remote)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Front] 发送 connection.ack 失败: %s", exc)

        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(self._handle_raw(ws, raw, send_lock))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketConnectionClosed as exc:
            logger.info(
                "[Front] 连接关闭: %s",
                format_ws_diagnostics(
                    {"remote": remote, "active_tasks": len(tasks)},
                    describe_ws_peer(ws),
                    describe_ws_exception(exc),
                ),
            )
        except Exception:
            logger.exception("[Front] 连接处理异常 (%s)", remote)
        finally:
            was_current = self.current_ws is ws
            if was_current:
                self.current_ws = None
                self.current_send_lock = None
                if self._forwarder is not None:
                    self._forwarder.detach(ws)
            for task in list(tasks):
                if not task.done():
                    task.cancel()
            if was_current:
                backend = self._admission.backend
                if backend is not None:
                    try:
                        await backend.on_gateway_disconnect(ws, remote)
                    except Exception:
                        logger.exception("[Front] runtime disconnect cleanup failed")
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_raw(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        parsed = parse_raw_message(raw)
        if isinstance(parsed, dict):
            try:
                async with send_lock:
                    await send_wire_payload(ws, parsed)
            except WebSocketConnectionClosed:
                return
            return
        request: AgentRequest = parsed
        logger.info(
            "[Front] 收到请求: request_id=%s channel_id=%s method=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.req_method.value if request.req_method else "",
            request.is_stream,
        )
        await self._router.dispatch(ws, request, send_lock)
