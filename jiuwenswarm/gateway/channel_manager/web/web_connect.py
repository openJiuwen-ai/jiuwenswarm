# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""WebChannel facade: composes WS transport, HTTP transport, and RPC host.

Public API is unchanged (``WebChannel`` / ``WebChannelConfig``). Internally:

- ``WebWsTransport`` — WebSocket only (extends ``BaseWsChannel``)
- ``WebHttpTransport`` — uvicorn / FastAPI lifecycle
- ``WebRpcHost`` — ``register_method``, history capture, shared handlers
- ``WebDeliveryRegistry`` — HTTP request-scoped outbounds
"""

from __future__ import annotations

from typing import Any, Callable

from jiuwenswarm.common.schema.message import Message, Mode, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

from jiuwenswarm.gateway.channel_manager.web.web_delivery_registry import WebDeliveryRegistry
from jiuwenswarm.gateway.channel_manager.web.web_http_transport import WebHttpTransport
from jiuwenswarm.gateway.channel_manager.web.web_rpc_host import (
    HANDLER_BEFORE_CALLBACK_METHODS,
    MethodHandler,
    MethodHandlerInvocation,
    WebRpcHost,
)
from jiuwenswarm.gateway.channel_manager.web.web_ws_transport import (
    WebChannelConfig,
    WebWsTransport,
    _WEB_CONNECTION_USER_ID_ATTR,
)

# Backward-compatible re-exports for invoke.py and tests.
_HANDLER_BEFORE_CALLBACK_METHODS = HANDLER_BEFORE_CALLBACK_METHODS
_MethodHandlerInvocation = MethodHandlerInvocation

__all__ = [
    "WebChannel",
    "WebChannelConfig",
    "_HANDLER_BEFORE_CALLBACK_METHODS",
    "_MethodHandlerInvocation",
    "_WEB_CONNECTION_USER_ID_ATTR",
]


class WebChannel(BaseChannel):
    """Northbound Web gateway: WS + HTTP share one RPC host and delivery registry."""

    name = "web"
    channel_id = "web"

    def __init__(self, config: WebChannelConfig, router: RobotMessageRouter) -> None:
        super().__init__(config, router)
        self.config = config
        self.delivery = WebDeliveryRegistry()
        self.rpc = WebRpcHost(self)
        self.ws = WebWsTransport(config, router, self)
        self.http = WebHttpTransport(self)
        self.git_watcher_registry: Any = None

    # ── Compatibility shims (invoke / handlers access private attrs) ──

    @property
    def _method_handlers(self) -> dict[str, MethodHandler]:
        return self.rpc.method_handlers

    @_method_handlers.setter
    def _method_handlers(self, value: dict[str, MethodHandler]) -> None:
        self.rpc.method_handlers.clear()
        self.rpc.method_handlers.update(value)

    @property
    def _on_message_cb(self) -> Callable[[Message], Any] | None:
        return self.rpc.on_message_cb

    @_on_message_cb.setter
    def _on_message_cb(self, value: Callable[[Message], Any] | None) -> None:
        self.rpc.on_message_cb = value

    def lookup_peer(self, peer_id: str) -> Any | None:
        ws = self.ws.lookup_ws_by_id(peer_id)
        if ws is not None:
            return ws
        return self.delivery.lookup(peer_id)

    def peers_for_session(self, session_id: str) -> set[Any]:
        peers = self.ws.peers_for_session_ws(session_id)
        peers.update(self.delivery.peers_for_session(session_id))
        return peers

    def _lookup_peer(self, peer_id: str) -> Any | None:
        return self.lookup_peer(peer_id)

    def _peers_for_session(self, session_id: str) -> set[Any]:
        return self.peers_for_session(session_id)

    def _record_history_frame(self, direction: str, data: Any) -> None:
        self.rpc.record_history_frame(direction, data)

    async def _invoke_method_handler(self, invocation: MethodHandlerInvocation) -> bool:
        return await self.rpc.invoke_method_handler(invocation)

    def _parse_req_method(self, method: str) -> ReqMethod | None:
        return WebWsTransport.parse_req_method(method)

    def _parse_mode(self, raw_mode: Any) -> Mode:
        return WebWsTransport.parse_mode(raw_mode)

    @staticmethod
    def _make_session_id() -> str:
        return WebWsTransport.make_session_id()

    @classmethod
    def _build_event_payload(cls, msg: Message, event_name: str) -> dict[str, Any]:
        return WebWsTransport.build_event_payload(msg, event_name)

    @staticmethod
    def _coalescible_stream_frame(frame: Any) -> tuple[dict[str, Any], str] | None:
        return WebWsTransport.coalescible_stream_frame(frame)

    @staticmethod
    def _extract_query_user_id(flat_query: dict[str, str]) -> str | None:
        return WebWsTransport.extract_query_user_id(flat_query)

    @staticmethod
    def _extract_ws_header_user_id(ws: Any) -> str | None:
        return WebWsTransport.extract_ws_header_user_id(ws)

    @classmethod
    def _resolve_connection_user_id(cls, flat_query: dict[str, str], ws: Any) -> str | None:
        return WebWsTransport.resolve_connection_user_id(flat_query, ws)

    @staticmethod
    def _connection_user_id(ws: Any) -> str | None:
        return WebWsTransport.connection_user_id(ws)

    @staticmethod
    def _routing_key_user_id(connection_user_id: str | None, remote: Any) -> str:
        return WebWsTransport.routing_key_user_id(connection_user_id, remote)

    # ── WS transport shims (tests / legacy callers) ────────────────────

    @property
    def _ws_sessions(self) -> dict[int, set[str]]:
        return self.ws.ws_sessions

    @property
    def _send_queues(self) -> dict[str, Any]:
        return self.ws.send_queues

    @property
    def _request_outbounds(self) -> dict[str, Any]:
        return self.delivery.request_outbounds

    @property
    def _session_request_outbounds(self) -> dict[str, set[str]]:
        return self.delivery.session_request_outbounds

    def _coalesce(self, first: Any, queue: Any) -> list[Any]:
        return self.ws.coalesce(first, queue)

    async def _writer_loop(self, ws: Any, ws_id: str) -> None:
        await self.ws.writer_loop(ws, ws_id)

    async def _handle_raw_message(self, ws: Any, raw: str, query: dict[str, list[str]]) -> None:
        await self.ws.handle_raw_message(ws, raw, query)

    async def register_ws(self, ws: Any, routing_key: Any) -> None:
        await self.ws.register_ws(ws, routing_key)

    async def unregister_ws(self, ws: Any) -> None:
        await self.ws.unregister_ws(ws)

    # ── Public API (delegates) ─────────────────────────────────────────

    @property
    def clients(self) -> set[Any]:
        return self.ws.clients

    @property
    def web_http_port(self) -> int | None:
        return self.http.port

    def register_method(self, method: str, handler: MethodHandler) -> None:
        self.rpc.register_method(method, handler)

    def on_connect(self, callback: Any) -> None:
        self.rpc.on_connect(callback)

    def on_disconnect(self, callback: Any) -> None:
        self.rpc.on_disconnect(callback)

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self.rpc.on_message(callback)

    def wrap_message_callback(self, wrapper: Any) -> None:
        self.rpc.wrap_message_callback(wrapper)

    async def send_response(self, ws: Any, req_id: str, **kwargs: Any) -> None:
        await self.ws.send_response(ws, req_id, **kwargs)

    async def send_event(
        self,
        ws: Any,
        event: str,
        payload: dict[str, Any],
        *,
        seq: int | None = None,
        stream_id: str | None = None,
    ) -> None:
        await self.ws.send_event(ws, event, payload, seq=seq, stream_id=stream_id)

    def register_request_outbound(self, outbound: Any) -> str:
        return self.delivery.register(outbound)

    async def unregister_request_outbound(self, outbound: Any) -> None:
        await self.delivery.unregister(outbound)

    async def clear_request_outbounds(self) -> None:
        await self.delivery.clear()

    async def broadcast_event(self, *args: Any, **kwargs: Any) -> None:
        await self.ws.broadcast_event(*args, **kwargs)

    async def send(
        self,
        msg: Message,
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        # 长程到点提醒绑定 exec session，常无前端 tab；按 session 路由会丢帧。
        # 广播给全部 web 客户端，由前端 Toast 消费。
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_name = ""
        if isinstance(payload.get("event_type"), str):
            event_name = payload["event_type"].strip()
        elif getattr(msg, "event_type", None) is not None:
            raw = msg.event_type
            event_name = raw.value if hasattr(raw, "value") else str(raw)
        if event_name == "long_horizon.stage_due":
            frame = {
                **payload,
                "session_id": getattr(msg, "session_id", None) or payload.get("session_id"),
                "request_id": getattr(msg, "id", None) or payload.get("request_id"),
            }
            await self.broadcast_event("long_horizon.stage_due", frame)
            return
        await self.ws.send(msg, routing_target=routing_target)

    def is_session_busy(self, session_id: str) -> bool:
        return self.ws.is_session_busy(session_id)

    def get_metadata(self) -> ChannelMetadata:
        return self.ws.get_metadata()

    async def start_http(self, *, host: str | None = None, port: int | None = None) -> bool:
        return await self.http.start(host=host, port=port)

    async def stop_http(self) -> None:
        await self.http.stop()

    async def start(self) -> None:
        """Start WebSocket and Gateway Web HTTP; block until WS server closes."""
        if self._running:
            return
        self._running = True
        self.rpc.maybe_start_history_capture()
        await self.ws.start_ws_server()
        await self.http.start()
        logger_info = (
            f"WebChannel 已启动: ws://{self.config.host}:{self.config.ws_port}{self.config.path}"
        )
        if self.http.port:
            logger_info += f" http://{self.config.host}:{self.http.port}"
        import logging
        logging.getLogger(__name__).info(logger_info)
        await self.ws.wait_closed()

    async def stop(self) -> None:
        self._running = False
        await self.http.stop()
        self.rpc.shutdown()
        await self.ws.stop_ws_server()

    async def connect(self) -> None:
        await self.start()

    async def disconnect(self) -> None:
        await self.stop()
