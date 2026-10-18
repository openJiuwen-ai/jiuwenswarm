# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket connection accept, origin check, and per-connection dispatch."""

from __future__ import annotations

import asyncio
import inspect
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
from jiuwenswarm.server.front.protocol import (
    CONNECTION_ROLE_HEADER,
    CONNECTION_ROLE_RPC,
    connection_ack_frame,
    parse_raw_message,
)
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


def connection_role(ws: Any) -> str:
    """Return the role a client declared in its handshake, or ``""``."""
    headers = getattr(ws, "request_headers", None)
    if headers is None:
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", None)
    value = get_header_value(headers, CONNECTION_ROLE_HEADER)
    return (value or "").strip().lower()


class ConnectionHandler:
    """Accept Gateway connections and own the server-push slot.

    Every connection that may receive server push is kept in arrival order;
    the newest one owns the push slot. When it closes, the slot moves to the
    newest connection still open. Runtime disconnect cleanup runs only once
    no push-capable connection is left. Connections that declare the ``rpc``
    role never touch the slot.
    """

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
        self._push_conns: list[tuple[Any, asyncio.Lock]] = []

    @property
    def current_ws(self) -> Any:
        return self._push_conns[-1][0] if self._push_conns else None

    @property
    def current_send_lock(self) -> asyncio.Lock | None:
        return self._push_conns[-1][1] if self._push_conns else None

    async def _publish_push_target(self, ws: Any, send_lock: asyncio.Lock) -> None:
        if self._forwarder is not None:
            self._forwarder.attach(ws, send_lock)
        backend = self._admission.backend
        if backend is None:
            return
        result = backend.attach_gateway_connection(ws, send_lock)
        if inspect.isawaitable(result):
            await result

    async def _release_push_target(self, ws: Any, remote: Any) -> None:
        index = next(
            (i for i, (conn, _) in enumerate(self._push_conns) if conn is ws),
            None,
        )
        if index is None:
            return
        was_current = index == len(self._push_conns) - 1
        del self._push_conns[index]
        if not was_current:
            return
        if self._push_conns:
            next_ws, next_lock = self._push_conns[-1]
            logger.info(
                "[Front] push 通道切换到仍存活的连接: closed=%s promoted=%s",
                remote,
                getattr(next_ws, "remote_address", None),
            )
            try:
                await self._publish_push_target(next_ws, next_lock)
            except Exception:
                logger.exception("[Front] push 通道切换失败")
            return
        if self._forwarder is not None:
            self._forwarder.detach(ws)
        backend = self._admission.backend
        if backend is not None:
            try:
                await backend.on_gateway_disconnect(ws, remote)
            except Exception:
                logger.exception("[Front] runtime disconnect cleanup failed")

    async def __call__(self, ws: Any) -> None:
        remote = ws.remote_address
        role = connection_role(ws)
        is_rpc = role == CONNECTION_ROLE_RPC
        logger.info("[Front] 新连接: %s role=%s", remote, role or "gateway")
        send_lock = asyncio.Lock()
        if not is_rpc:
            self._push_conns.append((ws, send_lock))
            try:
                await self._publish_push_target(ws, send_lock)
            except Exception:
                logger.exception("[Front] push 通道登记失败 (%s)", remote)
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
            for task in list(tasks):
                if not task.done():
                    task.cancel()
            if not is_rpc:
                await self._release_push_target(ws, remote)
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
