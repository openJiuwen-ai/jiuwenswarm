# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 AgentServerClient：route → HTTP 发信封 → touch。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
from jiuwenswarm.gateway.routing.http_agent_client import HttpSseAgentServerClient

from .invoke_ids import apply_invoke_ids_to_envelope
from .session_route_client import (
    FatalRouteError,
    RetryableRouteError,
    RuntimeSessionRouteClient,
)

logger = logging.getLogger(__name__)

_ROUTE_ATTEMPTS = 3
_TOUCH_INTERVAL_SECONDS = 20.0
_ROUTELESS_METHODS = frozenset(
    {
        "agent.reload_config",
        "agent.prewarm.sync",
        "agent.prewarm_sync",
        "browser.runtime_restart",
        "initialize",
    }
)


def http_base_from_pod_sse_url(pod_sse_url: str) -> str:
    """``http://ip:8080/sse`` → ``http://ip:8080``，交给协议层再拼 ``/api/v1``。"""
    parsed = urlsplit((pod_sse_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FatalRouteError(
            f"invalid pod_sse_url: {pod_sse_url!r}",
            code="VALIDATION",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            value = value[0]
        text = str(value).strip()
        if text:
            return text
    return ""


def _from_maps(key: str, *maps: Any) -> str:
    for mapping in maps:
        if not isinstance(mapping, dict):
            continue
        found = _first_text(mapping.get(key))
        if found:
            return found
        nested = mapping.get("query")
        if isinstance(nested, dict):
            found = _first_text(nested.get(key))
            if found:
                return found
        ext = mapping.get("ext")
        if isinstance(ext, dict):
            found = _first_text(ext.get(key))
            if found:
                return found
    return ""


def _is_heartbeat_envelope(envelope: E2AEnvelope) -> bool:
    if str(envelope.request_id or "").startswith("heartbeat-"):
        return True
    params = envelope.params if isinstance(envelope.params, dict) else {}
    run = params.get("run")
    if isinstance(run, dict) and str(run.get("kind") or "").strip().lower() == "heartbeat":
        return True
    return False


def _is_routeless_envelope(envelope: E2AEnvelope) -> bool:
    """无需 session route 的管理类请求（如启动期 reload_config）。"""
    if _is_heartbeat_envelope(envelope):
        return True
    method = str(envelope.method or "").strip().lower()
    return method in _ROUTELESS_METHODS


def _default_http_base() -> str:
    """管理类请求（reload_config 等）的默认 Agent HTTP Base。

    须指向 Agent ``http_server``（默认 ``AGENT_HTTP_PORT=8766``），
    **不要**打 WS 口 ``AGENT_SERVER_PORT=18092``（对该口发 HTTP 会 426/400 且无 JSON）。
    """
    explicit = read_env("GATEWAY_RUNTIME_DEFAULT_HTTP_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = read_env("AGENT_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = read_env("AGENT_HTTP_PORT", "8766").strip() or "8766"
    return f"http://{host}:{port}"


def identity_from_envelope(envelope: E2AEnvelope) -> tuple[str, str, str, str, str | None]:
    """返回 ``session_id, group_id, bot_id, request_id, user_id``。"""
    params = envelope.params if isinstance(envelope.params, dict) else {}
    ctx = envelope.channel_context if isinstance(envelope.channel_context, dict) else {}
    request_id = _first_text(envelope.request_id)
    user_id = _first_text(
        envelope.user_id,
        params.get("user_id"),
        _from_maps("user_id", params, ctx),
    ) or None
    group_id = _from_maps("group_id", params, ctx) or _first_text(envelope.chat_id)
    bot_id = _from_maps("bot_id", params, ctx)
    if not bot_id:
        bot_id = _from_maps("app_id", params, ctx)
    if not bot_id and isinstance(envelope.agent_ref, dict):
        bot_id = _first_text(
            envelope.agent_ref.get("bot_id"),
            envelope.agent_ref.get("agent_id"),
            envelope.agent_ref.get("id"),
        )
    if not bot_id:
        bot_id = _first_text(envelope.agent_id)
    # 如果 envelope 没带 group_id/bot_id/user_id，用默认值 route 到 wildcard scope
    if not user_id:
        user_id = "default"
    if not group_id:
        group_id = "default"
    if not bot_id:
        bot_id = "default"
    session_id = _first_text(envelope.session_id, params.get("session_id"))
    if not session_id and group_id and bot_id:
        session_id = f"{group_id}:{bot_id}:{user_id or '_'}"
    if not (session_id and group_id and bot_id and request_id):
        raise FatalRouteError(
            "route requires session_id/group_id/bot_id/request_id on envelope",
            code="VALIDATION",
        )
    return session_id, group_id, bot_id, request_id, user_id


def _is_http_retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (httpx.RequestError, httpx.HTTPStatusError, OSError, TimeoutError, asyncio.TimeoutError),
    )


class RuntimeRoutedAgentClient(AgentServerClient):
    """查 Runtime Manager 路由，再用 HTTP 客户端把信封发到对应 Pod。"""

    def __init__(
        self,
        *,
        route_client: RuntimeSessionRouteClient | None = None,
        http_client: HttpSseAgentServerClient | None = None,
        touch_interval_seconds: float = _TOUCH_INTERVAL_SECONDS,
        route_attempts: int = _ROUTE_ATTEMPTS,
    ) -> None:
        self._route = route_client or RuntimeSessionRouteClient()
        self._owns_route = route_client is None
        self._http = http_client or HttpSseAgentServerClient()
        self._owns_http = http_client is None
        self._touch_interval_seconds = max(0.0, float(touch_interval_seconds))
        self._route_attempts = max(1, int(route_attempts))
        self._connected = False
        # 企业版 HTTP 半双工：响应走 POST+SSE，AgentServer→Gateway 的主动 push
        # （cron.response / 文件下载 / evolution 等）必须额外订阅
        # GET /api/v1/events/stream。此处按 Pod（base_url）懒建订阅，对齐个人版
        # WebSocketAgentServerClient 天然具备的双向 push 能力。
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._push_clients: dict[str, HttpSseAgentServerClient] = {}
        self._push_lock = asyncio.Lock()

    async def connect(self, uri: str) -> None:
        _ = uri
        self._connected = True

    @property
    def server_ready(self) -> bool:
        """WebChannel on_connect 依赖此标志发送 connection.ack。"""
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False
        if self._owns_http:
            await self._http.disconnect()
        push_clients = list(self._push_clients.values())
        self._push_clients.clear()
        for client in push_clients:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("[RuntimeRouted] 断开 push 订阅失败")
        if self._owns_route:
            await self._route.aclose()

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        """注册 AgentServer 主动推送（server_push）回调。

        与个人版 ``WebSocketAgentServerClient.set_server_push_handler`` 语义一致，
        供 ``MessageHandler`` 注入 ``_handle_agent_server_push``。企业版经 HTTP/SSE
        通信，push 由每个 Pod 的 ``HttpSseAgentServerClient`` 单独订阅，故此处把
        回调记录并同步到已建立的订阅客户端上。
        """
        self._on_server_push = handler
        for client in self._push_clients.values():
            client.set_server_push_handler(handler)

    async def _ensure_push_subscription(self, base_url: str) -> None:
        """懒建立到指定 AgentServer Pod 的 push（``GET /api/v1/events/stream``）订阅。

        个人版 WS 天然双向，push 走同一条连接；企业版 HTTP 半双工，响应走
        POST+SSE，push 必须额外订阅。首次 route 到某 Pod 时为其建一个专用
        ``HttpSseAgentServerClient`` 订阅并复用；未注册回调或 Pod 已订阅则跳过。
        订阅失败不阻断业务请求（route 已成功，下次请求会重试）。
        """
        if self._on_server_push is None:
            return
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url or base_url in self._push_clients:
            return
        async with self._push_lock:
            if base_url in self._push_clients:
                return
            push_client = HttpSseAgentServerClient()
            try:
                await push_client.connect(base_url)
                push_client.set_server_push_handler(self._on_server_push)
            except Exception:
                logger.exception(
                    "[RuntimeRouted] 建立 push 订阅失败，跳过: base_url=%s",
                    base_url,
                )
                try:
                    await push_client.disconnect()
                except Exception:
                    pass
                return
            self._push_clients[base_url] = push_client
            logger.info("[RuntimeRouted] 已建立 push 订阅: base_url=%s", base_url)

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    def _heartbeat_response(self, envelope: E2AEnvelope) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or "web"),
            ok=True,
            payload={},
        )

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self._ensure_connected()
        if _is_heartbeat_envelope(envelope):
            return self._heartbeat_response(envelope)
        # 对齐旧 RuntimeManagement：发 Agent 前补齐并 MD5 service/agent/workspace。
        apply_invoke_ids_to_envelope(envelope)
        if _is_routeless_envelope(envelope):
            result = await self._http.send_request(envelope, base_url=_default_http_base())
            return result
        session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(envelope)
        base_url, route_id = await self._route_with_retry(
            session_id=session_id,
            group_id=group_id,
            bot_id=bot_id,
            request_id=request_id,
            user_id=user_id,
        )
        await self._ensure_push_subscription(base_url)
        try:
            result = await self._http.send_request(envelope, base_url=base_url)
        except Exception as exc:
            if not _is_http_retryable(exc):
                raise
            logger.warning(
                "[RuntimeRouted] unary http failed, re-route: session=%s err=%s",
                session_id,
                exc,
            )
            base_url, route_id = await self._route_with_retry(
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=uuid.uuid4().hex,
                user_id=user_id,
            )
            await self._ensure_push_subscription(base_url)
            result = await self._http.send_request(envelope, base_url=base_url)
        await self._touch_quiet(session_id, route_id)
        return result

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        self._ensure_connected()
        if _is_heartbeat_envelope(envelope):
            yield AgentResponseChunk(
                request_id=str(envelope.request_id or ""),
                channel_id=str(envelope.channel or "web"),
                payload={},
                is_complete=True,
            )
            return
        # 对齐旧 RuntimeManagement：发 Agent 前补齐并 MD5 service/agent/workspace。
        apply_invoke_ids_to_envelope(envelope)
        if _is_routeless_envelope(envelope):
            async for chunk in self._http.send_request_stream(
                envelope, base_url=_default_http_base()
            ):
                yield chunk
            return
        session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(envelope)
        base_url, route_id = await self._route_with_retry(
            session_id=session_id,
            group_id=group_id,
            bot_id=bot_id,
            request_id=request_id,
            user_id=user_id,
        )
        await self._ensure_push_subscription(base_url)
        yielded = 0
        try:
            async for chunk in self._stream_with_touch(
                envelope, base_url=base_url, session_id=session_id, route_id=route_id
            ):
                yielded += 1
                yield chunk
        except Exception as exc:
            if yielded or not _is_http_retryable(exc):
                raise
            logger.warning(
                "[RuntimeRouted] stream http failed before first chunk, re-route: session=%s err=%s",
                session_id,
                exc,
            )
            base_url, route_id = await self._route_with_retry(
                session_id=session_id,
                group_id=group_id,
                bot_id=bot_id,
                request_id=uuid.uuid4().hex,
                user_id=user_id,
            )
            await self._ensure_push_subscription(base_url)
            async for chunk in self._stream_with_touch(
                envelope, base_url=base_url, session_id=session_id, route_id=route_id
            ):
                yield chunk
        finally:
            await self._touch_quiet(session_id, route_id)

    async def _stream_with_touch(
        self,
        envelope: E2AEnvelope,
        *,
        base_url: str,
        session_id: str,
        route_id: str,
    ) -> AsyncIterator[AgentResponseChunk]:
        last_touch = time.monotonic()
        async for chunk in self._http.send_request_stream(envelope, base_url=base_url):
            now = time.monotonic()
            if (
                self._touch_interval_seconds > 0
                and now - last_touch >= self._touch_interval_seconds
            ):
                touched = await self._touch_quiet(session_id, route_id)
                last_touch = now
                if touched is False:
                    logger.warning(
                        "[RuntimeRouted] touch expired mid-stream: session=%s",
                        session_id,
                    )
            yield chunk

    async def _route_with_retry(
        self,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        request_id: str,
        user_id: str | None,
    ) -> tuple[str, str]:
        last_error: RetryableRouteError | None = None
        route_id = request_id
        for attempt in range(1, self._route_attempts + 1):
            try:
                result = await self._route.route(
                    session_id=session_id,
                    group_id=group_id,
                    bot_id=bot_id,
                    request_id=route_id,
                    user_id=user_id,
                )
                return http_base_from_pod_sse_url(result.pod_sse_url), result.request_id
            except RetryableRouteError as exc:
                last_error = exc
                delay = float(exc.retry_after) if exc.retry_after is not None else 1.0
                logger.warning(
                    "[RuntimeRouted] route retryable: code=%s attempt=%s/%s sleep=%.1fs",
                    exc.code,
                    attempt,
                    self._route_attempts,
                    delay,
                )
                if attempt >= self._route_attempts:
                    break
                await asyncio.sleep(delay)
        if last_error is None:
            raise RetryableRouteError("route failed", code="TRANSPORT")
        raise last_error

    async def _touch_quiet(self, session_id: str, request_id: str) -> bool | None:
        try:
            return await self._route.touch(session_id=session_id, request_id=request_id)
        except Exception:
            logger.warning(
                "[RuntimeRouted] touch failed: session=%s request_id=%s",
                session_id,
                request_id,
                exc_info=True,
            )
            return None
