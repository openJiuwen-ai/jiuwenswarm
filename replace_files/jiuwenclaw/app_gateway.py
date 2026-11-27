# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Standalone Gateway entrypoint (split deployment).

This process starts:
- Gateway MessageHandler + ChannelManager
- WebChannel websocket server (browser inbound)
- Heartbeat service
- Cron scheduler service (triggers remote AgentServer via ws)

It connects to a remote/local AgentServer WebSocket endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import inspect
import signal
import time
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
from openjiuwen.core.common.logging import LogManager

import jiuwenclaw.channel.acp_channel as acp_channel_module
from jiuwenclaw.channel.acp_channel import AcpGatewayBridge
from jiuwenclaw.gateway.route_binding import GatewayRouteBinding
from jiuwenclaw.jiuwen_core_patch import apply_openai_model_client_patch
from jiuwenclaw.utils import (
    configure_asyncio_event_loop_logging,
    get_user_workspace_dir,
    get_env_file,
    prepare_workspace,
    get_multi_tenant_user_workspace_dir,
    shutdown_logging,
)
from jiuwenclaw.extensions.extension_config_sync import decrypt_extensions_sensitive_for_agent
from jiuwenclaw.local_env_config import decrypt

apply_openai_model_client_patch()

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
# 多租户路径：service_default/agent_default/agent/jiuwenclaw_workspace
_multi_tenant_workspace = get_multi_tenant_user_workspace_dir("default", "default")
if _multi_tenant_workspace:
    _new_workspace = _multi_tenant_workspace / "agent" / "jiuwenclaw_workspace"
else:
    _new_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"
_old_workspace = _workspace_dir / "agent" / "workspace"

# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration)
if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
    prepare_workspace(overwrite=False)

# Reduce openjiuwen internal logs (keep Gateway logs)
for _lg in LogManager.get_all_loggers().values():
    _lg.set_level(logging.INFO)

load_dotenv(dotenv_path=get_env_file())

logger = logging.getLogger(__name__)

# Keep gateway idle-finalize fallback aligned with ACP channel default.
_PROMPT_IDLE_FINALIZE_SECONDS = 3.0


def _get_bool_config(
    env_key: str | None,
    cfg_value: Any,
    default: bool = True,
) -> bool:
    """读取布尔配置项，支持环境变量优先或 config.yaml 配置。

    Args:
        env_key: 环境变量名，如 "HEARTBEAT_ENABLED"；为 None 则不读取环境变量。
        cfg_value: config.yaml 中的配置值。
        default: 默认值。

    Returns:
        配置结果：环境变量优先 > config.yaml > 默认值。
    """
    if env_key is not None:
        env_raw = os.getenv(env_key)
        if env_raw is not None:
            return env_raw.lower() in ("true", "1", "yes")
    if cfg_value is not None:
        return bool(cfg_value)
    return default


def _normalize_gateway_message(msg):
    from jiuwenclaw.schema.message import Message, ReqMethod

    req_method = getattr(msg, "req_method", None) or ReqMethod.CHAT_SEND
    params = dict(msg.params or {})
    if "query" not in params and "content" in params:
        params["query"] = params["content"]
    if req_method == ReqMethod.CHAT_RESUME:
        req_method = ReqMethod.CHAT_CANCEL
        params.setdefault("intent", "resume")

    method_val = req_method.value
    is_stream = bool(
        msg.is_stream
        or method_val in (ReqMethod.CHAT_SEND.value, ReqMethod.HISTORY_GET.value)
    )

    return Message(
        id=msg.id,
        type=msg.type,
        channel_id=msg.channel_id,
        session_id=msg.session_id,
        params=params,
        timestamp=msg.timestamp,
        ok=msg.ok,
        req_method=req_method,
        mode=msg.mode,
        is_stream=is_stream,
        stream_seq=msg.stream_seq,
        stream_id=msg.stream_id,
        metadata=msg.metadata,
    )


def _normalize_and_forward_message(msg, channel_manager) -> bool:
    normalized = _normalize_gateway_message(msg)
    channel_manager.deliver_to_message_handler(normalized)
    logger.info("[App] Gateway inbound -> MessageHandler: id=%s channel_id=%s", msg.id, msg.channel_id)
    return False


class _InboundGatewayServer:
    """Gateway internal inbound service for forwarding channel messages to MessageHandler."""

    def __init__(self, inbound_handler):
        self._inbound_handler = inbound_handler
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._serve_loop(), name="gateway-inbound-server")

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def handle_message(self, msg) -> bool:
        await self._queue.put(msg)
        return True

    async def _serve_loop(self) -> None:
        while self._running:
            try:
                msg = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                handled = self._inbound_handler(msg)
                if asyncio.iscoroutine(handled):
                    await handled
            except Exception:  # noqa: BLE001
                logger.exception("[App] Gateway inbound handling failed: id=%s", getattr(msg, "id", None))


async def _connect_with_retry(
    client,
    uri: str,
    *,
    max_retries: int = 20,
    interval: float = 3.0,
) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            await client.connect(uri)
            logger.info("[App] connected to AgentServer: %s", uri)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries:
                logger.exception(
                    "[App] connect AgentServer failed after %d tries: %s",
                    attempt,
                    uri,
                )
                raise
            logger.warning(
                "[App] connect AgentServer failed (%d/%d): %s  retry in %s s...",
                attempt,
                max_retries,
                exc,
                interval,
            )
            await asyncio.sleep(interval)


@dataclass
class RouteConfig:
    """单条路由的配置（/acp, /cli 等）。

    http_handler: HTTP 请求处理器，签名为 (method: str, path: str, headers: dict, body: bytes) -> bytes。
    如果为 None，则该路由不支持 HTTP 请求。
    """

    path: str
    channel_id: str
    forward_methods: frozenset[str] = frozenset()
    forward_no_local_handler_methods: frozenset[str] = frozenset()
    local_handlers: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    inbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    outbound_interceptor: Callable[..., Awaitable[bool]] | None = None
    cleanup_handler: Callable[..., Any] | None = None
    disconnect_handler: Callable[..., Any] | None = None
    http_handler: Callable[..., bytes] | None = None


@dataclass
class GatewayServerConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 19001
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    path: str | None = None
    channel_id: str | None = None

    def __post_init__(self) -> None:
        if self.routes:
            return
        path = str(self.path or "").strip()
        channel_id = str(self.channel_id or "").strip()
        if path and channel_id:
            self.routes[path] = RouteConfig(path=path, channel_id=channel_id)


class GatewayServer:
    """通用多路路由 WebSocket Gateway Server。

    支持多个路径（如 /acp、/cli），每条路径可以有独立的 channel_id 和本地 handler。
    本地 handler 优先处理请求，未处理或无匹配则 forward 到 MessageHandler。
    """

    def __init__(self, config: GatewayServerConfig, router) -> None:
        self.config = config
        self.bus = router
        self._server = None
        self._running = False
        self._on_message_cb = None
        self._clients: set[Any] = set()
        self._request_to_client: dict[tuple[str, str], Any] = {}
        self._session_to_client: dict[tuple[str, str], Any] = {}
        self._acp_bridge = AcpGatewayBridge(
            self._dispatch_on_message,
            bind_session_client=self._bind_acp_session_client,
            channel_id="acp",
            idle_finalize_seconds=lambda: _PROMPT_IDLE_FINALIZE_SECONDS,
        )
        self._install_default_route_hooks()

    @staticmethod
    def _client_route_key(channel_id: str | None, scoped_id: str | None) -> tuple[str, str] | None:
        channel = str(channel_id or "").strip()
        scope = str(scoped_id or "").strip()
        if not channel or not scope:
            return None
        return channel, scope

    def on_message(self, callback) -> None:
        self._on_message_cb = callback

    async def _dispatch_on_message(self, msg) -> bool:
        if self._on_message_cb is None:
            return False
        result = self._on_message_cb(msg)
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    def _bind_acp_session_client(self, session_id: str, ws: Any) -> None:
        session_key = self._client_route_key("acp", session_id)
        if session_key is not None:
            self._session_to_client[session_key] = ws

    def _install_default_route_hooks(self) -> None:
        for route in self.config.routes.values():
            if route.channel_id != "acp":
                continue
            if route.inbound_interceptor is not None and route.outbound_interceptor is not None:
                continue
            route.inbound_interceptor = route.inbound_interceptor or self._acp_bridge.inbound_intercept
            route.outbound_interceptor = route.outbound_interceptor or self._acp_bridge.outbound_intercept
            route.cleanup_handler = route.cleanup_handler or self._acp_bridge.cleanup

    def _resolve_route(self, request_path: str) -> tuple[RouteConfig | None, str]:
        """按精确路径匹配路由；支持常见变体（如尾部斜杠）以避免客户端握手失败。"""
        routes = self.config.routes
        p = (request_path or "").strip()
        if not p:
            return None, request_path
        if p in routes:
            return routes[p], p
        if p != "/" and p.endswith("/") and p.rstrip("/") in routes:
            return routes[p.rstrip("/")], p.rstrip("/")
        if not p.endswith("/") and f"{p}/" in routes:
            return routes[f"{p}/"], f"{p}/"
        return None, p

    async def wait_until_closed(self) -> None:
        """阻塞至底层 WebSocket 服务完全关闭（与 :meth:`start` 配对供主循环持有任务）。"""
        if self._server is None:
            return
        await self._server.wait_closed()

    def register_local_handler(self, path: str, method: str, handler: Callable[..., Awaitable[None]]) -> None:
        """为指定路径注册本地方法 handler。"""
        route = self.config.routes.get(path)
        if route is None:
            route = RouteConfig(path=path, channel_id=path.strip("/"))
            self.config.routes[path] = route
        route.local_handlers[method] = handler

    async def send_response(
        self,
        ws: Any,
        req_id: str,
        *,
        ok: bool,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        """向指定客户端发送 res 帧（供本地 handler 使用）。"""
        frame: dict[str, Any] = {
            "type": "res",
            "id": req_id,
            "ok": ok,
            "payload": payload or {},
        }
        if not ok:
            frame["error"] = error or "request failed"
            if code:
                frame["code"] = code
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            logger.debug("send_response failed (client disconnected?)", exc_info=True)

    async def send_event(
        self,
        ws: Any,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """向指定客户端发送 event 帧（供本地 handler 使用）。"""
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:
            logger.debug("send_event failed (client disconnected?)", exc_info=True)

    async def start(self) -> None:
        if self._running or not self.config.enabled:
            return
        logger.info("[GatewayServer] starting on %s:%s", self.config.host, self.config.port)
        try:
            from websockets.legacy.server import serve as ws_serve
            from websockets.legacy.server import WebSocketServerProtocol
        except Exception:  # pragma: no cover
            from websockets import serve as ws_serve
            from websockets.server import WebSocketServerProtocol

        # Create unified protocol class that handles both HTTP and WebSocket
        gateway_server = self

        class _UnifiedServerProtocol(WebSocketServerProtocol):  # type: ignore[attr-defined]
            """WebSocket protocol that also handles HTTP requests on the same endpoint."""

            async def process_request(self, path, headers):
                # Route by URL path first to avoid misclassifying HTTP requests
                # that accidentally carry Upgrade-related headers.
                request_path = str(path).split("?", 1)[0]
                if request_path not in gateway_server.config.routes:
                    # It's an HTTP request (or unsupported WS path).
                    # VibeSkill HTTP is handled by VibeSkillChannel's own HTTP server on a separate port.
                    return (404, {"Content-Type": "application/json"}, b'{"error": "Not found"}')

                # It's a known WebSocket path - let the WS library validate handshake headers.
                return await super().process_request(path, headers)

        logger.info("[GatewayServer] calling ws_serve on %s:%s", self.config.host, self.config.port)
        self._server = await ws_serve(
            self._connection_handler,
            self.config.host,
            self.config.port,
            ping_interval=20,
            ping_timeout=600,
            create_protocol=_UnifiedServerProtocol,
        )
        self._running = True
        paths = ", ".join(self.config.routes.keys())
        logger.info(
            "[App] Gateway server started: ws://%s:%s [%s]",
            self.config.host,
            self.config.port,
            paths,
        )

    async def stop(self) -> None:
        self._running = False
        close_tasks = [client.close(code=1001, reason="server shutdown") for client in list(self._clients)]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients.clear()
        self._request_to_client.clear()
        self._session_to_client.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.info("[App] Gateway server stopped")

    async def send(self, msg) -> None:
        ws = None
        request_key = self._client_route_key(getattr(msg, "channel_id", None), getattr(msg, "id", None))
        if request_key is not None:
            ws = self._request_to_client.get(request_key)
            if ws is None:
                ws = self._request_to_client.get(request_key[1])
        if ws is None:
            session_key = self._client_route_key(
                getattr(msg, "channel_id", None),
                getattr(msg, "session_id", None),
            )
            if session_key is not None:
                ws = self._session_to_client.get(session_key)
                if ws is None:
                    ws = self._session_to_client.get(session_key[1])
        if ws is None or bool(getattr(ws, "closed", False)):
            return

        if getattr(msg, "channel_id", None) == "acp":
            handled = await self._acp_bridge.send_message(msg, ws)
            if handled:
                return

        # 让 route 的 outbound_interceptor 有机会拦截
        for route in self.config.routes.values():
            if route.channel_id == msg.channel_id and route.outbound_interceptor is not None:
                try:
                    handled = route.outbound_interceptor(msg, ws)
                    if asyncio.iscoroutine(handled):
                        handled = await handled
                    if handled:
                        return
                except Exception:
                    logger.warning(
                        "GatewayServer outbound interceptor failed: channel_id=%s",
                        msg.channel_id,
                        exc_info=True,
                    )
                break

        if msg.type == "res":
            payload = dict(msg.payload or {}) if isinstance(msg.payload, dict) else {}
            frame: dict[str, Any] = {
                "type": "res",
                "id": msg.id,
                "ok": bool(msg.ok),
                "payload": payload,
            }
            if not msg.ok:
                frame["error"] = str(payload.get("error") or "request failed")
            await ws.send(json.dumps(frame, ensure_ascii=False))
            return

        event_name = "chat.final"
        if msg.event_type is not None:
            event_name = msg.event_type.value

        if isinstance(msg.payload, dict):
            payload = {**msg.payload}
            payload.setdefault("session_id", msg.session_id)
        else:
            payload = {"session_id": msg.session_id, "content": str(msg.payload or "")}

        frame = {"type": "event", "event": event_name, "payload": payload}
        await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _connection_handler(self, ws: Any, path: str | None = None) -> None:
        raw_path = path if path is not None else getattr(ws, "path", "")
        parsed = urlparse(raw_path)
        request_path = parsed.path or raw_path

        route, matched_path = self._resolve_route(request_path)
        if route is None:
            await ws.close(code=1008, reason=f"unsupported path: {request_path}")
            return

        self._clients.add(ws)

        # connection.ack
        try:
            await ws.send(json.dumps({
                "type": "event",
                "event": "connection.ack",
                "payload": {
                    "protocol_version": "1.0",
                    "transport": route.channel_id,
                },
            }, ensure_ascii=False))
        except Exception:
            self._clients.discard(ws)
            return

        try:
            async for raw in ws:
                await self._handle_raw_message(ws, raw, matched_path, route)
        finally:
            self._clients.discard(ws)
            stale_request_ids = [request_id for request_id, client in self._request_to_client.items() if client is ws]
            for request_id in stale_request_ids:
                self._request_to_client.pop(request_id, None)
            stale_session_keys = [key for key, client in self._session_to_client.items() if client is ws]
            for session_key in stale_session_keys:
                self._session_to_client.pop(session_key, None)
            if route.disconnect_handler is not None:
                try:
                    result = route.disconnect_handler(ws, stale_session_keys)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.warning(
                        "GatewayServer disconnect handler failed: path=%s",
                        request_path,
                        exc_info=True,
                    )
            elif route.cleanup_handler is not None:
                try:
                    route.cleanup_handler(ws)
                except Exception:
                    logger.warning(
                        "GatewayServer cleanup handler failed: path=%s",
                        request_path,
                        exc_info=True,
                    )

    async def _handle_raw_message(self, ws: Any, raw: str, request_path: str, route: RouteConfig) -> None:
        from jiuwenclaw.schema.message import Message, Mode, ReqMethod

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(
                json.dumps(
                    {"type": "res", "id": "", "ok": False, "error": "invalid json"},
                    ensure_ascii=False,
                )
            )
            return

        if route.channel_id == "acp":
            handled = await self._acp_bridge.handle_jsonrpc_request(ws, data)
            if handled:
                return

        if route.channel_id == "acp" and self._acp_bridge.is_jsonrpc_request(data):
            return

        # route 级别的原始帧拦截（如 ACP JSON-RPC response）
        if route.inbound_interceptor is not None:
            try:
                handled = route.inbound_interceptor(ws, data)
                if asyncio.iscoroutine(handled):
                    handled = await handled
                if handled:
                    return
            except Exception:
                logger.warning(
                    "GatewayServer inbound interceptor failed: path=%s",
                    request_path,
                    exc_info=True,
                )

        if not isinstance(data, dict) or data.get("type") != "req":
            await ws.send(
                json.dumps(
                    {"type": "res", "id": "", "ok": False, "error": "invalid request"},
                    ensure_ascii=False,
                )
            )
            return

        req_id = str(data.get("id") or "").strip()
        method = str(data.get("method") or "").strip()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        if not req_id or not method:
            await ws.send(
                json.dumps(
                    {"type": "res", "id": req_id, "ok": False, "error": "invalid request"},
                    ensure_ascii=False,
                )
            )
            return

        session_id = str(params.get("session_id") or "").strip() or req_id

        # 1. forward 优先：方法在 forward_methods 中则转发到 MessageHandler
        if method in route.forward_methods:
            req_method = None
            for item in ReqMethod:
                if item.value == method:
                    req_method = item
                    break

            if req_method is None:
                await ws.send(
                    json.dumps(
                        {"type": "res", "id": req_id, "ok": False, "error": f"unknown method: {method}"},
                        ensure_ascii=False,
                    )
                )
                return

            request_key = self._client_route_key(route.channel_id, req_id)
            if request_key is not None:
                self._request_to_client[request_key] = ws
            session_key = self._client_route_key(route.channel_id, session_id)
            if session_key is not None:
                self._session_to_client[session_key] = ws

            mode = Mode.from_raw(params.get("mode"), default=Mode.AGENT_PLAN)

            msg = Message(
                id=req_id,
                type="req",
                channel_id=route.channel_id,
                session_id=session_id,
                params=params,
                timestamp=time.time(),
                ok=True,
                req_method=req_method,
                mode=mode,
                metadata={"method": method},
            )

            if self._on_message_cb is not None:
                result = self._on_message_cb(msg)
                if asyncio.iscoroutine(result):
                    await result

            # ACP route may receive legacy ``type=req`` frames from some clients.
            # They should be forwarded upstream without falling through to the
            # generic "unknown method" error path.
            if route.channel_id == "acp":
                return

            # 如果在 forward_no_local_handler_methods 中，不需要本地 ack
            if method in route.forward_no_local_handler_methods:
                return

        # 2. 本地 handler：发 ack 或纯本地处理
        local_handler = route.local_handlers.get(method)
        if local_handler is not None:
            try:
                await local_handler(ws, req_id, params, session_id)
            except Exception as e:
                ws_closed = bool(getattr(ws, "closed", False))
                if ws_closed:
                    logger.warning("GatewayServer local handler aborted on closed ws (%s): %s", method, e)
                    return
                logger.exception("GatewayServer local handler error (%s)", method)
                try:
                    await self.send_response(
                        ws, req_id, ok=False,
                        error=f"handler error: {e}", code="INTERNAL_ERROR",
                    )
                except Exception:
                    logger.debug(
                        "GatewayServer failed to send handler error response: method=%s id=%s",
                        method,
                        req_id,
                        exc_info=True,
                    )
            return

        # 3. 无 forward 也无本地 handler
        await ws.send(
            json.dumps(
                {"type": "res", "id": req_id, "ok": False, "error": f"unknown method: {method}"},
                ensure_ascii=False,
            )
        )



def _build_acp_route_binding(
    *,
    path: str,
    channel_id: str,
    forward_methods: frozenset[str],
    forward_no_local_handler_methods: frozenset[str],
    on_message_cb,
) -> GatewayRouteBinding:
    return GatewayRouteBinding(
        path=path,
        channel_id=channel_id,
        forward_methods=forward_methods,
        forward_no_local_handler_methods=forward_no_local_handler_methods,
    )


def _build_route_config_map(bindings: list[GatewayRouteBinding]) -> dict[str, RouteConfig]:
    return {
        binding.path: RouteConfig(
            path=binding.path,
            channel_id=binding.channel_id,
            forward_methods=binding.forward_methods,
            forward_no_local_handler_methods=binding.forward_no_local_handler_methods,
            inbound_interceptor=binding.inbound_interceptor,
            outbound_interceptor=binding.outbound_interceptor,
            cleanup_handler=binding.cleanup_handler,
            disconnect_handler=binding.disconnect_handler,
            http_handler=binding.http_handler,
        )
        for binding in bindings
    }


async def _run(
    agent_server_url: str,
    web_host: str,
    web_port: int,
    web_path: str,
) -> None:
    configure_asyncio_event_loop_logging()
    from jiuwenclaw.channel.dingding import DingTalkChannel, DingTalkConfig
    from jiuwenclaw.channel.feishu import FeishuChannel, FeishuConfig
    from jiuwenclaw.channel.whatsapp_channel import WhatsAppChannel, WhatsAppChannelConfig
    from jiuwenclaw.channel.wechat_channel import WechatChannel, WechatConfig
    from jiuwenclaw.channel.web_channel import WebChannel, WebChannelConfig
    from jiuwenclaw.channel.xiaoyi_channel import XiaoyiChannel, XiaoyiChannelConfig
    from jiuwenclaw.channel.telegram_channel import TelegramChannel, TelegramChannelConfig
    from jiuwenclaw.channel.discord_channel import DiscordChannel, DiscordChannelConfig
    from jiuwenclaw.channel.wecom_channel import WecomChannel, WecomConfig
    from jiuwenclaw.channel.vibeskill_channel import VibeSkillChannel, VibeSkillConfig
    from jiuwenclaw.config import get_config
    from jiuwenclaw.gateway.agent_client import WebSocketAgentServerClient
    from jiuwenclaw.gateway.channel_manager import ChannelManager
    from jiuwenclaw.gateway.cron import CronController, CronJobStore, CronSchedulerService
    from jiuwenclaw.gateway.heartbeat import GatewayHeartbeatService, HeartbeatConfig
    from jiuwenclaw.gateway.message_handler import MessageHandler
    from jiuwenclaw.app_web_handlers import (
        WebHandlersBindParams,
        _DummyBus,
        _CONFIG_SET_ENV_MAP,
        _CONFIG_YAML_KEYS,
        _FORWARD_NO_LOCAL_HANDLER_METHODS,
        _FORWARD_REQ_METHODS,
        _register_web_handlers,
    )
    from jiuwenclaw.channel.cli_channel import (
        CliRouteBindParams,
        build_cli_route_binding,
    )
    from jiuwenclaw.extensions.manager import ExtensionManager
    from jiuwenclaw.extensions.registry import ExtensionRegistry
    from jiuwenclaw.schema.message import Message
    from jiuwenclaw.schema.hook_event import GatewayHookEvents
    from jiuwenclaw.schema.hooks_context import WebChannelCreatedHookContext
    from jiuwenclaw.updater import WindowsUpdaterService
    from jiuwenclaw.telemetry import init_telemetry
    from openjiuwen.core.runner import Runner

    logger.info("[App] Gateway starting, connecting AgentServer: %s", agent_server_url)

    callback_framework = Runner.callback_framework
    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(registry=extension_registry)
    await extension_manager.load_all_extensions()
    logger.info("[App] extensions loaded: %d", len(extension_manager.list_extensions()))

    # ---------- Telemetry 初始化 ----------
    init_telemetry()

    startup_cfg: dict[str, Any] = {}
    try:
        startup_cfg = get_config()
    except Exception as e:  # noqa: BLE001
        logger.warning("[App] failed to read startup config from config.yaml: %s", e)

    def _cfg_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    from jiuwenclaw.sandbox.sandbox_routing_settings import sandbox_routing_enabled

    max_retries = int(os.getenv("AGENT_CONNECT_RETRY", "20"))
    retry_interval = float(os.getenv("AGENT_CONNECT_RETRY_INTERVAL", "3"))

    if sandbox_routing_enabled():
        from jiuwenclaw.gateway.sandbox_router import SandboxRouterAgentClient

        client = SandboxRouterAgentClient()
        await client.connect(agent_server_url)
        logger.info("[App] sandbox routing enabled for VibeSkill; default WebSocketAgentServerClient is not used")
    else:
        agent_server_ext = extension_registry.get_agent_server_client_extension()
        if agent_server_ext is not None:
            logger.info("[App] using extension AgentServerClient: %s", agent_server_ext.metadata.name)
            client = agent_server_ext.get_client()
        else:
            client = WebSocketAgentServerClient(ping_interval=20.0, ping_timeout=600.0)
        await _connect_with_retry(
            client,
            agent_server_url,
            max_retries=max_retries,
            interval=retry_interval,
        )

    message_handler = MessageHandler(client)
    await message_handler.start_forwarding()

    # IM Pipeline 初始化（数字分身）
    from jiuwenclaw.gateway.im_pipeline.im_inbound import IMInboundPipeline
    from jiuwenclaw.gateway.im_pipeline.im_outbound import IMOutboundPipeline
    im_inbound = IMInboundPipeline()
    im_outbound = IMOutboundPipeline()
    message_handler.set_inbound_pipeline(im_inbound)
    message_handler.set_outbound_pipeline(im_outbound)

    cron_store = CronJobStore(path=get_user_workspace_dir() / "gateway" / "cron_jobs.json")
    cron_scheduler = CronSchedulerService(
        store=cron_store,
        agent_client=client,
        message_handler=message_handler,
    )
    cron_controller = CronController.get_instance(store=cron_store, scheduler=cron_scheduler)
    message_handler.set_cron_controller(cron_controller)

    full_cfg: dict[str, Any] = {}
    heartbeat_cfg: dict | None = None
    channels_cfg: dict | None = None
    sync_config_cfg: dict | None = None
    try:
        full_cfg = get_config()
        heartbeat_cfg = full_cfg.get("heartbeat") if isinstance(full_cfg, dict) else None
        channels_cfg = full_cfg.get("channels") if isinstance(full_cfg, dict) else None
        sync_config_cfg = full_cfg.get("sync_config") if isinstance(full_cfg, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[App] failed to read heartbeat config from config.yaml, using defaults: %s", e)
        heartbeat_cfg = None
        channels_cfg = None
        sync_config_cfg = None

    # 配置解密后存储在内存中
    env_dict = {}
    for env_key in _CONFIG_SET_ENV_MAP.values():
        env_dict[env_key] = decrypt(env_key, os.getenv(env_key))
    client.set_or_update_server_config(
        config=dict(full_cfg or {}),
        env=env_dict
    )

    if isinstance(heartbeat_cfg, dict):
        cfg_every = heartbeat_cfg.get("every")
        cfg_target = heartbeat_cfg.get("target")
        cfg_active_hours = heartbeat_cfg.get("active_hours")
    else:
        cfg_every = None
        cfg_target = None
        cfg_active_hours = None

    heartbeat_interval = float(
        os.getenv("HEARTBEAT_INTERVAL")
        or (str(cfg_every) if cfg_every is not None else "60")
    )
    heartbeat_timeout = float(os.getenv("HEARTBEAT_TIMEOUT", "30")) if os.getenv("HEARTBEAT_TIMEOUT") else None
    heartbeat_relay_channel = os.getenv("HEARTBEAT_RELAY_CHANNEL_ID") or (
        str(cfg_target) if cfg_target is not None else "web"
    )

    # Heartbeat is disabled by hardcode for vibeskill.
    heartbeat_enabled = False

    heartbeat_config = HeartbeatConfig(
        enabled=heartbeat_enabled,
        interval_seconds=heartbeat_interval,
        timeout_seconds=heartbeat_timeout,
        relay_channel_id=heartbeat_relay_channel,
        active_hours=cfg_active_hours if isinstance(cfg_active_hours, dict) else None,
    )
    heartbeat_service = GatewayHeartbeatService(
        client,
        heartbeat_config,
        message_handler=message_handler,
    )
    if heartbeat_enabled:
        await heartbeat_service.start()
    else:
        logger.info("[App] Heartbeat service is hard-disabled")

    initial_channels_conf: dict = channels_cfg if isinstance(channels_cfg, dict) else {}
    channel_manager = ChannelManager(message_handler, config=initial_channels_conf)
    updater_service = WindowsUpdaterService()

    async def _on_config_saved(
        updated_env_keys: set[str] | None = None,
        *,
        env_updates: dict[str, str] | None = None,
        config_payload: dict[str, Any] | None = None,
    ) -> bool:
        browser_runtime_keys = {
            "MODEL_PROVIDER",
            "MODEL_NAME",
            "API_BASE",
            "API_KEY",
            "VIDEO_PROVIDER",
            "VIDEO_MODEL_NAME",
            "VIDEO_API_BASE",
            "VIDEO_API_KEY",
            "AUDIO_PROVIDER",
            "AUDIO_MODEL_NAME",
            "AUDIO_API_BASE",
            "AUDIO_API_KEY",
            "VISION_PROVIDER",
            "VISION_MODEL_NAME",
            "VISION_API_BASE",
            "VISION_API_KEY",
        }
        try:
            client.set_or_update_server_config(
                config=dict(config_payload or {}),
                env=dict(env_updates or {}),
            )

            from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenclaw.schema.message import ReqMethod

            # 发送给 AgentServer 前解密扩展敏感配置
            decrypted_config = decrypt_extensions_sensitive_for_agent(config_payload or {})
            reload_env = e2a_from_agent_fields(
                request_id=f"agent-reload-{uuid_module.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                params={
                    # config: full config snapshot after save; Agent should prefer this over local yaml.
                    "config": dict(decrypted_config or {}),
                    # env: incremental environment updates; missing keys mean unchanged.
                    "env": dict(env_updates or {}),
                },
            )
            reload_resp = await client.send_request(reload_env)
            if not getattr(reload_resp, "ok", False):
                err_payload = getattr(reload_resp, "payload", None) or {}
                err_msg = (
                    err_payload.get("error")
                    if isinstance(err_payload, dict)
                    else err_payload
                )
                raise RuntimeError(f"agent.reload_config rejected: {err_msg}")

            if updated_env_keys and (browser_runtime_keys & set(updated_env_keys)):
                restart_env = e2a_from_agent_fields(
                    request_id=f"browser-restart-{uuid_module.uuid4().hex[:8]}",
                    channel_id="",
                    req_method=ReqMethod.BROWSER_RUNTIME_RESTART,
                )
                await client.send_request(restart_env)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("[App] hot config reload failed")
            return False
    # 启动时将配置同步给agentserver（可通过配置关闭）
    sync_config_enabled_cfg = sync_config_cfg.get("enabled") if isinstance(sync_config_cfg, dict) else None
    sync_config_on_startup = _get_bool_config("SYNC_CONFIG_ON_STARTUP", sync_config_enabled_cfg, default=False)

    if sync_config_on_startup:
        callback_result = _on_config_saved(
            set(_CONFIG_SET_ENV_MAP.values()) | _CONFIG_YAML_KEYS,
            env_updates=dict(env_dict),
            config_payload=dict(full_cfg or {})
        )
        if inspect.isawaitable(callback_result):
            await callback_result
    else:
        logger.info("[App] Sync config to AgentServer on startup disabled by config")
    web_channel = None
    web_config = WebChannelConfig(enabled=True, host=web_host, port=web_port, path=web_path)
    web_channel = WebChannel(web_config, _DummyBus())

    # 触发 WebChannel 创建完成扩展点
    web_channel_ctx = WebChannelCreatedHookContext(
        web_channel=web_channel,
        host=web_host,
        port=web_port,
        path=web_path,
    )
    await extension_registry.trigger(GatewayHookEvents.WEB_CHANNEL_CREATED, web_channel_ctx)

    _register_web_handlers(
        WebHandlersBindParams(
            channel=web_channel,
            agent_client=client,
            message_handler=message_handler,
            channel_manager=channel_manager,
            on_config_saved=_on_config_saved,
            heartbeat_service=heartbeat_service if heartbeat_enabled else None,
            cron_controller=cron_controller,
            updater_service=updater_service,
        )
    )

    def _make_norm_and_forward(
        forward_methods: set[str] | frozenset[str],
        no_local_methods: set[str] | frozenset[str],
        source_label: str,
    ):
        def _norm_and_forward(msg: Message) -> bool:
            method_val = getattr(getattr(msg, "req_method", None), "value", None) or ""
            if method_val not in forward_methods:
                return False
            normalized = _normalize_gateway_message(msg)
            channel_manager.deliver_to_message_handler(normalized)
            logger.info("[App] %s 入站 -> MessageHandler: id=%s channel_id=%s", source_label, msg.id, msg.channel_id)
            if method_val in no_local_methods:
                return True
            return False

        return _norm_and_forward

    # remote 模式：session 操作经队列与 session.list 顺序一致；create/delete 转发到 Agent，list 在网关读索引
    from jiuwenclaw.gateway.session_index import is_remote_storage
    from jiuwenclaw.gateway.web_normalize import (
        register_forward_method,
        get_extra_forward_methods,
        get_extra_no_local_methods,
    )
    if is_remote_storage():
        register_forward_method("session.create", skip_local=True)
        register_forward_method("session.delete", skip_local=True)
        register_forward_method("session.list", skip_local=True)
        logger.info(
            "[App] remote session storage 已启用：session.create/delete 转发到 Agent，"
            "session.list 由 MessageHandler 读网关索引",
        )

    web_norm_and_forward = _make_norm_and_forward(
        _FORWARD_REQ_METHODS | get_extra_forward_methods(),
        _FORWARD_NO_LOCAL_HANDLER_METHODS | get_extra_no_local_methods(),
        "Web",
    )
    channel_manager.register_channel_with_inbound(web_channel, web_norm_and_forward)

    acp_inbound_server = _InboundGatewayServer(
        lambda msg: _normalize_and_forward_message(msg, channel_manager)
    )
    await acp_inbound_server.start()

    # VibeSkill 有自己独立的 inbound server（与 acp 隔离）
    vibeskill_inbound_server = _InboundGatewayServer(
        lambda msg: _normalize_and_forward_message(msg, channel_manager)
    )
    await vibeskill_inbound_server.start()

    # VibeSkill 统一端口（默认 19003，HTTP + WebSocket 共用）；
    # 显式设置 VIBESKILL_HTTP_PORT / VIBESKILL_WS_PORT 可回退到双端口兼容模式。
    def _env_int(name: str) -> int | None:
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            logger.warning("[App] invalid %s=%r, ignored", name, raw)
            return None

    _vibeskill_port = _env_int("VIBESKILL_PORT") or 19003
    _vibeskill_config = VibeSkillConfig(
        channel_id="vibeskill",
        port=_vibeskill_port,
        http_port=_env_int("VIBESKILL_HTTP_PORT"),
        ws_port=_env_int("VIBESKILL_WS_PORT"),
        max_busy_sessions_user=_env_int("MAX_BUSY_SESSIONS_USER"),
        max_busy_sessions=_env_int("MAX_BUSY_SESSIONS"),
    )
    vibeskill_channel = VibeSkillChannel(
        config=_vibeskill_config,
        router=channel_manager,
        agent_client=client,
    )
    vibeskill_channel.on_message(vibeskill_inbound_server.handle_message)

    route_bindings = [
        _build_acp_route_binding(
            path="/acp",
            channel_id="acp",
            forward_methods=_FORWARD_REQ_METHODS,
            forward_no_local_handler_methods=_FORWARD_NO_LOCAL_HANDLER_METHODS,
            on_message_cb=acp_inbound_server.handle_message,
        ),
        build_cli_route_binding(
            CliRouteBindParams(
                agent_client=client,
                message_handler=message_handler,
                on_config_saved=_on_config_saved,
                path="/tui",
                channel_id="tui",
            )
        ),
    ]

    gateway_server_config = GatewayServerConfig(
        enabled=True,
        host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("GATEWAY_PORT", "19001")),
        routes=_build_route_config_map(route_bindings),
    )
    gateway_server = GatewayServer(gateway_server_config, _DummyBus())
    for binding in route_bindings:
        route_config = gateway_server_config.routes[binding.path]
        channel_manager.register_external_channel(route_config.channel_id, gateway_server)
        if binding.install is not None:
            binding.install(gateway_server)
    gateway_server.on_message(acp_inbound_server.handle_message)

    channel_manager.register_channel(vibeskill_channel)
    await vibeskill_channel.start()

    feishu_channel = None
    feishu_task = None
    feishu_enterprise_channels: dict[str, FeishuChannel] = {}
    feishu_enterprise_tasks: dict[str, asyncio.Task] = {}
    xiaoyi_channel = None
    xiaoyi_task = None
    dingtalk_channel = None
    dingtalk_task = None
    telegram_channel = None
    telegram_task = None
    discord_channel = None
    discord_task = None
    whatsapp_channel = None
    whatsapp_task = None
    wecom_channel = None
    wecom_task = None
    wechat_channel = None
    wechat_task = None

    _last_channels_conf: dict = {}

    def _should_restart_channel(channel_name: str, old_conf: dict, new_conf: dict) -> bool:
        old_channel_conf = old_conf.get(channel_name) if isinstance(old_conf, dict) else None
        new_channel_conf = new_conf.get(channel_name) if isinstance(new_conf, dict) else None
        if (old_channel_conf is None) != (new_channel_conf is None):
            return True
        if old_channel_conf is None:
            return False
        return old_channel_conf != new_channel_conf

    async def _stop_channel(channel, task, channel_name: str, background_wait: bool = False) -> None:
        if task is not None:
            task.cancel()
            if background_wait:

                async def wait_cancel():
                    try:
                        await task
                    except (TypeError, asyncio.CancelledError):
                        logger.info("[App] cancelled previous %sChannel task", channel_name.capitalize())
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "[App] ignored exception while waiting for previous %sChannel task: %s",
                            channel_name.capitalize(),
                            e,
                        )

                asyncio.create_task(wait_cancel(), name=f"wait_{channel_name}_cancel")
            else:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[App] timeout while waiting for %sChannel task cancellation",
                        channel_name.capitalize(),
                    )
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[App] ignored exception while waiting for previous %sChannel task: %s",
                        channel_name.capitalize(),
                        e,
                    )

        if channel is not None:
            try:
                await asyncio.wait_for(channel.stop(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[App] timeout while stopping %sChannel", channel_name.capitalize())
            except Exception as e:  # noqa: BLE001
                logger.warning("[App] failed to stop previous %sChannel: %s", channel_name.capitalize(), e)
            channel_manager.unregister_channel(channel.channel_id)

    def _is_channel_enabled(conf: dict | None, required_fields: list[str]) -> tuple[bool, str]:
        if conf is None:
            return False, "missing or invalid config"
        enabled_raw = conf.get("enabled", None)
        if enabled_raw is None:
            all_fields_present = all(conf.get(f) for f in required_fields)
            return all_fields_present, f"missing {','.join(required_fields)}" if not all_fields_present else ""
        return bool(enabled_raw), "enabled = false" if not enabled_raw else ""

    async def _apply_channel_config(conf: dict) -> None:
        nonlocal feishu_channel, feishu_task, xiaoyi_channel, xiaoyi_task
        nonlocal dingtalk_channel, dingtalk_task, telegram_channel, telegram_task
        nonlocal discord_channel, discord_task
        nonlocal whatsapp_channel, whatsapp_task
        nonlocal wecom_channel, wecom_task
        nonlocal wechat_channel, wechat_task
        nonlocal _last_channels_conf
        nonlocal feishu_enterprise_channels, feishu_enterprise_tasks

        restart_pending = channel_manager.pop_channel_restart_pending()
        changed_channels: list[str] = []
        for channel_name in [
            "feishu",
            "feishu_enterprise",
            "xiaoyi",
            "dingtalk",
            "telegram",
            "whatsapp",
            "discord",
            "wecom",
            "wechat",
        ]:
            if _should_restart_channel(channel_name, _last_channels_conf, conf) or channel_name in restart_pending:
                if channel_name in restart_pending and not _should_restart_channel(
                    channel_name, _last_channels_conf, conf
                ):
                    logger.info(
                        "[App] channels.%s force restart requested; cached runtime state must be dropped",
                        channel_name,
                    )
                changed_channels.append(channel_name)
        _last_channels_conf = dict(conf or {})

        if "feishu" in changed_channels:
            feishu_conf = conf.get("feishu") if isinstance(conf, dict) else None
            await _stop_channel(feishu_channel, feishu_task, "feishu")
            feishu_channel, feishu_task = None, None

            if isinstance(feishu_conf, dict):
                enabled, reason = _is_channel_enabled(feishu_conf, ["app_id", "app_secret"])
                if not enabled:
                    logger.info("[App] channels.feishu.%s, FeishuChannel disabled", reason)
                else:
                    feishu_config = FeishuConfig(
                        enabled=True,
                        app_id=str(feishu_conf.get("app_id") or "").strip(),
                        app_secret=str(feishu_conf.get("app_secret") or "").strip(),
                        encrypt_key=str(feishu_conf.get("encrypt_key") or "").strip(),
                        verification_token=str(feishu_conf.get("verification_token") or "").strip(),
                        allow_from=feishu_conf.get("allow_from") or [],
                        enable_streaming=bool(feishu_conf.get("enable_streaming", True)),
                        chat_id=str(feishu_conf.get("chat_id") or "").strip(),
                        last_chat_id=str(feishu_conf.get("last_chat_id") or "").strip(),
                        last_open_id=str(feishu_conf.get("last_open_id") or "").strip(),
                        group_digital_avatar=bool(feishu_conf.get("group_digital_avatar", False)),
                        my_user_id=str(feishu_conf.get("my_user_id") or feishu_conf.get("my_open_id") or "").strip(),
                        bot_name=str(feishu_conf.get("bot_name") or "").strip(),
                        enable_memory=bool(feishu_conf.get("enable_memory", False)),
                        message_merge_window_ms=int(feishu_conf.get("message_merge_window_ms", 15000)),
                    )
                    # 数字分身：创建 adapter 并注册到 pipeline
                    feishu_adapter = None
                    if feishu_config.group_digital_avatar:
                        from jiuwenclaw.channel.platform_adapter.feishu_im_adapter import FeishuIMPlatformAdapter
                        feishu_adapter = FeishuIMPlatformAdapter(
                            my_open_id=feishu_config.my_user_id,
                            bot_name=feishu_config.bot_name,
                        )
                        im_inbound.register_adapter("feishu", feishu_adapter)
                        im_outbound.register_adapter("feishu", feishu_adapter)
                    feishu_channel = FeishuChannel(feishu_config, _DummyBus(), im_platform_adapter=feishu_adapter)
                    channel_manager.register_channel(feishu_channel)
                    feishu_task = asyncio.create_task(feishu_channel.start(), name="feishu")
                    logger.info("[App] FeishuChannel registered from config.yaml.channels.feishu")
            else:
                logger.info("[App] channels.feishu missing or invalid, FeishuChannel disabled")

        if "feishu_enterprise" in changed_channels:
            for bot_key, task in list(feishu_enterprise_tasks.items()):
                await _stop_channel(
                    feishu_enterprise_channels.get(bot_key),
                    task,
                    f"feishu_enterprise[{bot_key}]",
                )
            for _old_ch in feishu_enterprise_channels.values():
                _old_ch_id = getattr(_old_ch, "_channel_id", "") or getattr(_old_ch, "name", "")
                if _old_ch_id:
                    im_inbound.unregister_adapter(_old_ch_id)
                    im_outbound.unregister_adapter(_old_ch_id)
            feishu_enterprise_channels = {}
            feishu_enterprise_tasks = {}

            enterprise_conf = conf.get("feishu_enterprise") if isinstance(conf, dict) else None
            if not isinstance(enterprise_conf, dict):
                logger.info(
                    "[App] channels.feishu_enterprise missing or invalid; "
                    "FeishuEnterpriseChannel disabled"
                )
            else:
                for bot_key, bot_conf_raw in enterprise_conf.items():
                    if not isinstance(bot_key, str) or not bot_key.strip():
                        continue
                    bot_conf = bot_conf_raw if isinstance(bot_conf_raw, dict) else None
                    if bot_conf is None:
                        logger.info("[App] channels.feishu_enterprise.%s invalid config, skipping", bot_key)
                        continue
                    enabled, reason = _is_channel_enabled(bot_conf, ["app_id", "app_secret"])
                    if not enabled:
                        logger.info(
                            "[App] channels.feishu_enterprise.%s.%s, FeishuEnterpriseChannel disabled",
                            bot_key,
                            reason,
                        )
                        continue

                    bot_key = bot_key.strip()
                    app_id = str(bot_conf.get("app_id") or "").strip()
                    channel_id = f"feishu_enterprise:{app_id}"
                    feishu_config = FeishuConfig(
                        enabled=True,
                        app_id=app_id,
                        app_secret=str(bot_conf.get("app_secret") or "").strip(),
                        encrypt_key=str(bot_conf.get("encrypt_key") or "").strip(),
                        verification_token=str(bot_conf.get("verification_token") or "").strip(),
                        allow_from=bot_conf.get("allow_from") or [],
                        enable_streaming=bool(bot_conf.get("enable_streaming", True)),
                        chat_id=str(bot_conf.get("chat_id") or "").strip(),
                        channel_id=channel_id,
                        bot_key=bot_key,
                        last_chat_id=str(bot_conf.get("last_chat_id") or "").strip(),
                        last_open_id=str(bot_conf.get("last_open_id") or "").strip(),
                        my_user_id=str(bot_conf.get("my_user_id") or "").strip(),
                        bot_name=str(bot_conf.get("bot_name") or "").strip(),
                        group_digital_avatar=bool(bot_conf.get("group_digital_avatar", False)),
                        enable_memory=bool(bot_conf.get("enable_memory", False)),
                    )
                    feishu_adapter = None
                    if feishu_config.group_digital_avatar:
                        from jiuwenclaw.channel.platform_adapter.feishu_im_adapter import FeishuIMPlatformAdapter
                        feishu_adapter = FeishuIMPlatformAdapter(
                            my_open_id=feishu_config.my_user_id,
                            bot_name=feishu_config.bot_name,
                        )
                        im_inbound.register_adapter(channel_id, feishu_adapter)
                        im_outbound.register_adapter(channel_id, feishu_adapter)
                    channel = FeishuChannel(feishu_config, _DummyBus(), im_platform_adapter=feishu_adapter)
                    channel_manager.register_channel(channel)
                    task = asyncio.create_task(channel.start(), name=f"feishu-enterprise-{bot_key}")
                    feishu_enterprise_channels[bot_key] = channel
                    feishu_enterprise_tasks[bot_key] = task
                    logger.info(
                        "[App] registered FeishuChannel(%s) from config.yaml.channels.feishu_enterprise.%s",
                        bot_key,
                        channel_id,
                    )

        if "xiaoyi" in changed_channels:
            xiaoyi_conf = conf.get("xiaoyi") if isinstance(conf, dict) else None
            await _stop_channel(xiaoyi_channel, xiaoyi_task, "xiaoyi")
            xiaoyi_channel, xiaoyi_task = None, None

            if isinstance(xiaoyi_conf, dict):
                enabled, reason = _is_channel_enabled(xiaoyi_conf, ["ak", "sk", "agent_id"])
                if not enabled:
                    logger.info("[App] channels.xiaoyi.%s, XiaoyiChannel disabled", reason)
                else:
                    if xiaoyi_conf.get("mode") == "xiaoyi_claw":
                        xiaoyi_config = XiaoyiChannelConfig(
                            enabled=True,
                            mode=str(xiaoyi_conf.get("mode") or "xiaoyi_claw").strip(),
                            api_id=str(xiaoyi_conf.get("api_id") or "").strip(),
                            push_id=str(xiaoyi_conf.get("push_id") or "").strip(),
                            push_url=str(xiaoyi_conf.get("push_url") or "").strip(),
                            agent_id=str(xiaoyi_conf.get("agent_id") or "").strip(),
                            uid=str(xiaoyi_conf.get("uid") or "").strip(),
                            api_key=str(xiaoyi_conf.get("api_key") or "").strip(),
                            file_upload_url=str(xiaoyi_conf.get("file_upload_url") or "").strip(),
                            ws_url1=str(xiaoyi_conf.get("ws_url1")).strip(),
                            ws_url2=str(xiaoyi_conf.get("ws_url2")).strip(),
                            enable_streaming=bool(xiaoyi_conf.get("enable_streaming", True)),
                        )
                    else:
                        xiaoyi_config = XiaoyiChannelConfig(
                            enabled=True,
                            mode=str(xiaoyi_conf.get("mode") or "xiaoyi_channel").strip(),
                            ak=str(xiaoyi_conf.get("ak") or "").strip(),
                            sk=str(xiaoyi_conf.get("sk") or "").strip(),
                            api_id=str(xiaoyi_conf.get("api_id") or "").strip(),
                            push_id=str(xiaoyi_conf.get("push_id") or "").strip(),
                            push_url=str(xiaoyi_conf.get("push_url") or "").strip(),
                            agent_id=str(xiaoyi_conf.get("agent_id") or "").strip(),
                            ws_url1=str(xiaoyi_conf.get("ws_url1") or "").strip()
                            or "wss://hag.cloud.huawei.com/openclaw/v1/ws/link",
                            ws_url2=str(xiaoyi_conf.get("ws_url2") or "").strip()
                            or "wss://116.63.174.231/openclaw/v1/ws/link",
                            enable_streaming=bool(xiaoyi_conf.get("enable_streaming", True)),
                        )
                    xiaoyi_channel = XiaoyiChannel(xiaoyi_config, _DummyBus())
                    channel_manager.register_channel(xiaoyi_channel)
                    xiaoyi_task = asyncio.create_task(xiaoyi_channel.start(), name="xiaoyi")
                    logger.info("[App] XiaoyiChannel registered from config.yaml.channels.xiaoyi")
            else:
                logger.info("[App] channels.xiaoyi missing or invalid, XiaoyiChannel disabled")

        if "dingtalk" in changed_channels:
            dingtalk_conf = conf.get("dingtalk") if isinstance(conf, dict) else None
            await _stop_channel(dingtalk_channel, dingtalk_task, "dingtalk", background_wait=True)
            dingtalk_channel, dingtalk_task = None, None

            if isinstance(dingtalk_conf, dict):
                enabled, reason = _is_channel_enabled(dingtalk_conf, ["client_id", "client_secret"])
                if not enabled:
                    logger.info("[App] channels.dingtalk.%s, DingTalkChannel disabled", reason)
                else:
                    dingtalk_config = DingTalkConfig(
                        enabled=True,
                        client_id=str(dingtalk_conf.get("client_id") or "").strip(),
                        client_secret=str(dingtalk_conf.get("client_secret") or "").strip(),
                        allow_from=dingtalk_conf.get("allow_from") or [],
                    )
                    dingtalk_channel = DingTalkChannel(dingtalk_config, _DummyBus())
                    channel_manager.register_channel(dingtalk_channel)
                    dingtalk_task = asyncio.create_task(dingtalk_channel.start(), name="dingtalk")
                    logger.info("[App] DingTalkChannel registered from config.yaml.channels.dingtalk")
            else:
                logger.info("[App] channels.dingtalk missing or invalid, DingTalkChannel disabled")

        if "telegram" in changed_channels:
            telegram_conf = conf.get("telegram") if isinstance(conf, dict) else None
            await _stop_channel(telegram_channel, telegram_task, "telegram")
            telegram_channel, telegram_task = None, None

            if isinstance(telegram_conf, dict):
                enabled, reason = _is_channel_enabled(telegram_conf, ["bot_token"])
                if not enabled:
                    logger.info("[App] channels.telegram.%s, TelegramChannel disabled", reason)
                else:
                    telegram_config = TelegramChannelConfig(
                        enabled=True,
                        bot_token=str(telegram_conf.get("bot_token") or "").strip(),
                        allow_from=telegram_conf.get("allow_from") or [],
                        parse_mode=str(telegram_conf.get("parse_mode") or "Markdown").strip(),
                        group_chat_mode=str(telegram_conf.get("group_chat_mode") or "mention").strip(),
                    )
                    telegram_channel = TelegramChannel(telegram_config, _DummyBus())
                    channel_manager.register_channel(telegram_channel)
                    telegram_task = asyncio.create_task(telegram_channel.start(), name="telegram")
                    logger.info("[App] TelegramChannel registered from config.yaml.channels.telegram")
            else:
                logger.info("[App] channels.telegram missing or invalid, TelegramChannel disabled")

        if "discord" in changed_channels:
            discord_conf = conf.get("discord") if isinstance(conf, dict) else None
            await _stop_channel(discord_channel, discord_task, "discord")
            discord_channel, discord_task = None, None

            if isinstance(discord_conf, dict):
                enabled, reason = _is_channel_enabled(discord_conf, ["bot_token"])
                if not enabled:
                    logger.info("[App] channels.discord.%s, DiscordChannel disabled", reason)
                else:
                    discord_config = DiscordChannelConfig(
                        enabled=True,
                        bot_token=str(discord_conf.get("bot_token") or "").strip(),
                        application_id=str(discord_conf.get("application_id") or "").strip(),
                        guild_id=str(discord_conf.get("guild_id") or "").strip(),
                        channel_id=str(discord_conf.get("channel_id") or "").strip(),
                        allow_from=discord_conf.get("allow_from") or [],
                        block_dm=(str(discord_conf.get("block_dm")).lower() in ["true", "1"]) or False,
                    )
                    discord_channel = DiscordChannel(discord_config, _DummyBus())
                    channel_manager.register_channel(discord_channel)
                    discord_task = asyncio.create_task(discord_channel.start(), name="discord")
                    logger.info("[App] DiscordChannel registered from config.yaml.channels.discord")
            else:
                logger.info("[App] channels.discord missing or invalid, DiscordChannel disabled")

        if "whatsapp" in changed_channels:
            whatsapp_conf = conf.get("whatsapp") if isinstance(conf, dict) else None
            await _stop_channel(whatsapp_channel, whatsapp_task, "whatsapp")
            whatsapp_channel, whatsapp_task = None, None

            if isinstance(whatsapp_conf, dict):
                bridge_ws_url = str(whatsapp_conf.get("bridge_ws_url") or "ws://127.0.0.1:19600/ws").strip()
                default_jid = str(whatsapp_conf.get("default_jid") or "").strip()
                allow_from = whatsapp_conf.get("allow_from") or []
                enable_streaming = bool(whatsapp_conf.get("enable_streaming", True))
                auto_start_bridge = bool(whatsapp_conf.get("auto_start_bridge", False))
                bridge_command = str(
                    whatsapp_conf.get("bridge_command") or "node scripts/whatsapp-bridge.js"
                ).strip()
                bridge_workdir = str(whatsapp_conf.get("bridge_workdir") or "").strip()
                bridge_env_raw = whatsapp_conf.get("bridge_env") or {}
                bridge_env = bridge_env_raw if isinstance(bridge_env_raw, dict) else {}

                enabled_raw = whatsapp_conf.get("enabled", None)
                if enabled_raw is None:
                    enabled = bool(bridge_ws_url)
                else:
                    enabled = bool(enabled_raw)

                if not enabled:
                    logger.info("[App] channels.whatsapp.enabled = false, WhatsAppChannel disabled")
                elif not bridge_ws_url:
                    logger.info("[App] channels.whatsapp missing bridge_ws_url, WhatsAppChannel disabled")
                else:
                    whatsapp_config = WhatsAppChannelConfig(
                        enabled=True,
                        enable_streaming=enable_streaming,
                        bridge_ws_url=bridge_ws_url,
                        allow_from=allow_from,
                        default_jid=default_jid,
                        auto_start_bridge=auto_start_bridge,
                        bridge_command=bridge_command,
                        bridge_workdir=bridge_workdir,
                        bridge_env={str(k): str(v) for k, v in bridge_env.items()},
                    )
                    whatsapp_channel = WhatsAppChannel(whatsapp_config, _DummyBus())
                    channel_manager.register_channel(whatsapp_channel)
                    whatsapp_task = asyncio.create_task(whatsapp_channel.start(), name="whatsapp")
                    logger.info("[App] WhatsAppChannel registered from config.yaml.channels.whatsapp")
            else:
                logger.info("[App] channels.whatsapp missing or invalid, WhatsAppChannel disabled")

        if "wecom" in changed_channels:
            wecom_conf = conf.get("wecom") if isinstance(conf, dict) else None
            await _stop_channel(wecom_channel, wecom_task, "wecom")
            wecom_channel, wecom_task = None, None

            if isinstance(wecom_conf, dict):
                enabled, reason = _is_channel_enabled(wecom_conf, ["bot_id", "secret"])
                if not enabled:
                    logger.info("[App] channels.wecom.%s, WecomChannel disabled", reason)
                else:
                    wecom_config = WecomConfig(
                        enabled=True,
                        bot_id=str(wecom_conf.get("bot_id") or "").strip(),
                        secret=str(wecom_conf.get("secret") or "").strip(),
                        ws_url=str(wecom_conf.get("ws_url") or "wss://openws.work.weixin.qq.com").strip(),
                        allow_from=wecom_conf.get("allow_from") or [],
                        enable_streaming=bool(wecom_conf.get("enable_streaming", True)),
                        send_thinking_message=bool(wecom_conf.get("send_thinking_message", True)),
                        group_digital_avatar=bool(wecom_conf.get("group_digital_avatar", False)),
                        my_user_id=str(wecom_conf.get("my_user_id") or "").strip(),
                        bot_name=str(wecom_conf.get("bot_name") or "").strip(),
                        enable_memory=bool(wecom_conf.get("enable_memory", False)),
                    )
                    # 数字分身：创建 adapter 并注册到 pipeline
                    wecom_adapter = None
                    if wecom_config.group_digital_avatar:
                        from jiuwenclaw.channel.platform_adapter.wecom_im_adapter import WecomIMPlatformAdapter
                        wecom_adapter = WecomIMPlatformAdapter(
                            my_user_id=wecom_config.my_user_id,
                            bot_name=wecom_config.bot_name,
                        )
                        im_inbound.register_adapter("wecom", wecom_adapter)
                        im_outbound.register_adapter("wecom", wecom_adapter)
                    wecom_channel = WecomChannel(wecom_config, _DummyBus(), im_platform_adapter=wecom_adapter)
                    channel_manager.register_channel(wecom_channel)
                    wecom_task = asyncio.create_task(wecom_channel.start(), name="wecom")
                    logger.info("[App] WecomChannel registered from config.yaml.channels.wecom")
            else:
                logger.info("[App] channels.wecom missing or invalid, WecomChannel disabled")

        if "wechat" in changed_channels:
            wechat_conf = conf.get("wechat") if isinstance(conf, dict) else None
            await _stop_channel(wechat_channel, wechat_task, "wechat")
            wechat_channel, wechat_task = None, None

            if isinstance(wechat_conf, dict):
                enabled, reason = _is_channel_enabled(wechat_conf, [])
                if not enabled:
                    logger.info("[App] channels.wechat.%s, WechatChannel disabled", reason)
                else:
                    wechat_config = WechatConfig(
                        enabled=True,
                        base_url=str(wechat_conf.get("base_url") or "https://ilinkai.weixin.qq.com").strip(),
                        bot_token=str(wechat_conf.get("bot_token") or "").strip(),
                        ilink_bot_id=str(wechat_conf.get("ilink_bot_id") or "").strip(),
                        ilink_user_id=str(wechat_conf.get("ilink_user_id") or "").strip(),
                        allow_from=wechat_conf.get("allow_from") or [],
                        auto_login=bool(wechat_conf.get("auto_login", True)),
                        qrcode_poll_interval_sec=float(wechat_conf.get("qrcode_poll_interval_sec", 2.0)),
                        long_poll_timeout_sec=int(wechat_conf.get("long_poll_timeout_sec", 45)),
                        backoff_base_sec=float(wechat_conf.get("backoff_base_sec", 1.0)),
                        backoff_max_sec=float(wechat_conf.get("backoff_max_sec", 30.0)),
                        credential_file=str(
                            wechat_conf.get("credential_file") or "~/.wx-ai-bridge/credentials.json"
                        ).strip(),
                        enable_streaming=bool(wechat_conf.get("enable_streaming", True)),
                    )
                    wechat_channel = WechatChannel(wechat_config, _DummyBus())
                    channel_manager.register_channel(wechat_channel)
                    wechat_task = asyncio.create_task(wechat_channel.start(), name="wechat")
                    logger.info("[App] WechatChannel registered from config.yaml.channels.wechat")
            else:
                logger.info("[App] channels.wechat missing or invalid, WechatChannel disabled")

    channel_manager.set_config_callback(_apply_channel_config)
    await channel_manager.set_config(initial_channels_conf)

    await channel_manager.start_dispatch()
    await cron_scheduler.start()
    # 先同步完成监听绑定，避免 IDE/ACP 子进程在端口尚未就绪时连接导致多次重试。
    logger.info("[App] about to call gateway_server.start()")
    try:
        await gateway_server.start()
        logger.info("[App] gateway_server.start() returned")
    except Exception:
        logger.exception("[App] gateway_server.start() failed")
        raise
    gateway_server_task = asyncio.create_task(
        gateway_server.wait_until_closed(),
        name="acp-gateway-server",
    )
    web_task = (
        asyncio.create_task(web_channel.start(), name="web-channel")
        if web_channel is not None
        else None
    )
    if web_channel is not None:
        logger.info(
            "[App] started: Web ws://%s:%s%s  AgentServer: %s  Press Ctrl+C to exit.",
            web_host,
            web_port,
            web_path,
            agent_server_url,
        )

    stop_event = asyncio.Event()

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("[App] shutdown signal received, draining...")
            stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    except (NotImplementedError, RuntimeError, OSError, ValueError):
        pass

    try:
        tasks_to_wait = [task for task in (gateway_server_task, web_task) if task is not None]
        if tasks_to_wait:
            stop_waiter = asyncio.create_task(stop_event.wait(), name="gateway-shutdown")
            _done, pending = await asyncio.wait(
                set(tasks_to_wait) | {stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        else:
            await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("[App] received Ctrl+C, shutting down...")
    except asyncio.CancelledError:
        pass
    finally:
        if gateway_server_task is not None:
            gateway_server_task.cancel()
            try:
                await gateway_server_task
            except asyncio.CancelledError:
                pass
        await gateway_server.stop()
        await acp_inbound_server.stop()
        if web_task is not None:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        if web_channel is not None:
            await web_channel.stop()

        if feishu_channel is not None and feishu_task is not None:
            feishu_task.cancel()
            try:
                await feishu_task
            except asyncio.CancelledError:
                pass
            await feishu_channel.stop()
        for bot_key, task in list(feishu_enterprise_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            channel = feishu_enterprise_channels.get(bot_key)
            if channel is not None:
                await channel.stop()
        if xiaoyi_channel is not None and xiaoyi_task is not None:
            xiaoyi_task.cancel()
            try:
                await xiaoyi_task
            except asyncio.CancelledError:
                pass
            await xiaoyi_channel.stop()
        if dingtalk_channel is not None and dingtalk_task is not None:
            dingtalk_task.cancel()
            try:
                await dingtalk_task
            except (TypeError, asyncio.CancelledError):
                pass
            await dingtalk_channel.stop()
        if telegram_channel is not None and telegram_task is not None:
            telegram_task.cancel()
            try:
                await telegram_task
            except asyncio.CancelledError:
                pass
            await telegram_channel.stop()
        if discord_channel is not None and discord_task is not None:
            discord_task.cancel()
            try:
                await discord_task
            except asyncio.CancelledError:
                pass
            await discord_channel.stop()
        if whatsapp_channel is not None and whatsapp_task is not None:
            whatsapp_task.cancel()
            try:
                await whatsapp_task
            except asyncio.CancelledError:
                pass
            await whatsapp_channel.stop()
        if wecom_channel is not None and wecom_task is not None:
            wecom_task.cancel()
            try:
                await wecom_task
            except asyncio.CancelledError:
                pass
            await wecom_channel.stop()
        if wechat_channel is not None and wechat_task is not None:
            wechat_task.cancel()
            try:
                await wechat_task
            except asyncio.CancelledError:
                pass
            await wechat_channel.stop()

        await cron_scheduler.stop()
        await channel_manager.stop_dispatch()
        if heartbeat_enabled:
            await heartbeat_service.stop()
        await message_handler.stop_forwarding()
        await client.disconnect()
        logger.info("[App] Gateway stopped")
        shutdown_logging()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jiuwenclaw-gateway",
        description="Start JiuwenClaw Gateway + Channels (split deployment; connects to jiuwenclaw-agentserver).",
    )
    parser.add_argument(
        "--agent-server-url",
        "-u",
        default=None,
        metavar="URL",
        help="AgentServer WebSocket URL (default: AGENT_SERVER_URL or ws://AGENT_SERVER_HOST:AGENT_SERVER_PORT).",
    )
    parser.add_argument(
        "--host",
        "-H",
        default=None,
        metavar="HOST",
        help="WebChannel bind host (default: WEB_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="WebChannel bind port (default: WEB_PORT or 19000).",
    )
    parser.add_argument(
        "--web-path",
        default=None,
        metavar="PATH",
        help="WebChannel ws path (default: WEB_PATH or /ws).",
    )
    args = parser.parse_args()

    default_host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1")
    default_port = os.getenv("AGENT_SERVER_PORT") or os.getenv("AGENT_PORT", "18092")
    agent_server_url = (
        args.agent_server_url
        or os.getenv("AGENT_SERVER_URL")
        or f"ws://{default_host}:{default_port}"
    )
    web_host = args.host or os.getenv("WEB_HOST", "127.0.0.1")
    web_port = args.port or int(os.getenv("WEB_PORT", "19000"))
    web_path = args.web_path or os.getenv("WEB_PATH", "/ws")

    try:
        asyncio.run(
            _run(
                agent_server_url=agent_server_url,
                web_host=web_host,
                web_port=web_port,
                web_path=web_path,
            )
        )
    except KeyboardInterrupt:
        logger.info("[App] interrupted by user")
    except Exception:
        logger.exception("[App] Gateway fatal error")
        raise


if __name__ == "__main__":
    main()
