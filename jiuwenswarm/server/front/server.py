# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight AgentServer Front WebSocket listener."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.server.front.admission import ExecutionAdmission, RuntimeBackend
from jiuwenswarm.server.front.connection import ConnectionHandler, process_handshake
from jiuwenswarm.server.front.router import MethodRouter
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)


class AgentServerFront:
    """Owns the Gateway-facing port. Does not construct Agent Runtime."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18092,
        *,
        readiness: Readiness | None = None,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
    ) -> None:
        self._host = host
        self._port = port
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self.readiness = readiness or Readiness()
        self.admission = ExecutionAdmission(self.readiness)
        self.router = MethodRouter(self.readiness, self.admission)
        self.connection = ConnectionHandler(self.readiness, self.router, self.admission)
        self._server: Any = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def attach_runtime_backend(self, backend: RuntimeBackend) -> None:
        if self.connection.current_ws is not None and self.connection.current_send_lock is not None:
            backend.attach_gateway_connection(
                self.connection.current_ws,
                self.connection.current_send_lock,
            )
        self.admission.attach_backend(backend)

    async def start(self) -> None:
        if self._server is not None:
            logger.warning("[Front] 服务端已在运行")
            return
        try:
            from websockets.legacy.server import serve as legacy_serve

            self._server = await legacy_serve(
                self.connection,
                self._host,
                self._port,
                process_request=process_handshake,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            )
        except ImportError:
            import websockets

            self._server = await websockets.serve(
                self.connection,
                self._host,
                self._port,
                process_request=process_handshake,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=AGENT_WS_MAX_MESSAGE_BYTES,
            )
        self.readiness.mark_transport_ready()
        self.readiness.mark_control_ready()
        self.readiness.mark_runtime_warming()
        logger.info("[Front] 已启动: ws://%s:%s", self._host, self._port)

    async def send_push(self, payload: dict[str, Any]) -> None:
        ws = self.connection.current_ws
        send_lock = self.connection.current_send_lock
        if ws is None or send_lock is None:
            return
        async with send_lock:
            await send_wire_payload(ws, payload)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("[Front] 已停止")
