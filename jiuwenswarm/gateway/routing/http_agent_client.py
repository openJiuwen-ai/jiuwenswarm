# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer HTTP/SSE 客户端（``gateway.agent_client.type=http``）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from jiuwenswarm.common.e2a.constants import (
    E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST,
    E2A_WIRE_SERVER_PUSH_KEY,
)
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.security.link_mtls import LinkMTLSConfig
from jiuwenswarm.gateway.routing.agent_client import (
    AGENT_REQUEST_TIMEOUT_SECONDS,
    AgentServerClient,
)
from jiuwenswarm.gateway.routing.agent_rest_map import (
    assemble_rest_request,
    normalize_agent_http_base,
)

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 10.0
_PUSH_RETRY_SECONDS = 3.0
# 与 WebSocketAgentServerClient._delayed_cleanup_cancelled_request_id 对齐。
_CANCELLED_RID_TTL_SECONDS = 2.0


def http_unary_to_agent_response(
    payload: dict[str, Any],
    *,
    channel_id: str,
    request_id: str,
) -> AgentResponse:
    """拆 Agent HTTP ``{ok, data, error}``，不要喂给 ``parse_agent_server_wire_unary``。"""
    rid = str(payload.get("request_id") or request_id)
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    ok = bool(payload.get("ok", False))
    if ok:
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            pl: dict[str, Any] = dict(data["result"])
        elif isinstance(data, dict):
            pl = dict(data)
        elif data is None:
            pl = {}
        else:
            pl = {"content": data}
        return AgentResponse(
            request_id=rid,
            channel_id=channel_id,
            ok=True,
            payload=pl,
            metadata=meta or None,
        )

    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    code = err.get("code")
    if not code and isinstance(details, dict):
        code = details.get("code")
    pl = {"error": err.get("message"), "code": code}
    if isinstance(details, dict):
        for key, value in details.items():
            pl.setdefault(key, value)
    return AgentResponse(
        request_id=rid,
        channel_id=channel_id,
        ok=False,
        payload=pl,
        metadata=meta or None,
    )


async def iter_sse_data_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """解析 SSE：丢掉 ``: keepalive``，产出 ``data:`` JSON 对象。"""
    data_lines: list[str] = []

    def _flush() -> dict[str, Any] | None:
        nonlocal data_lines
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines = []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[HttpSseAgentServerClient] SSE data 非 JSON，已丢弃")
            return None
        return parsed if isinstance(parsed, dict) else None

    async for line in response.aiter_lines():
        text = line.rstrip("\r")
        if text == "":
            frame = _flush()
            if frame is not None:
                yield frame
            continue
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            continue
        if text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
            continue
    frame = _flush()
    if frame is not None:
        yield frame


class HttpSseAgentServerClient(AgentServerClient):
    """按 REST 表发 HTTP；流式读 SSE；打断走另一条 POST，不与 SSE 共用锁。"""

    def __init__(
        self,
        *,
        timeout_s: float = AGENT_REQUEST_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        link_mtls_config: LinkMTLSConfig | None = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._http = http_client
        self._owns_http = http_client is None
        self._base_url: str | None = None
        self._api_root: str | None = None
        self._server_ready = False
        self._running = False
        self._on_server_push: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._push_task: asyncio.Task[None] | None = None
        self._push_ready = asyncio.Event()
        self._push_handlers: set[asyncio.Task[None]] = set()
        self._stream_lock = asyncio.Lock()
        self._cancelled_request_ids: set[str] = set()
        self._inflight_stream_ids: set[str] = set()
        self._link_mtls = link_mtls_config

    def _link_config(self) -> LinkMTLSConfig:
        if self._link_mtls is None:
            self._link_mtls = LinkMTLSConfig.from_env()
        return self._link_mtls

    def _request_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(headers or {})
        merged.update(self._link_config().binding_headers())
        return merged

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    def set_server_push_handler(
        self, handler: Callable[[dict[str, Any]], Awaitable[None]] | None
    ) -> None:
        self._on_server_push = handler
        if handler is not None and self._running and self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop(), name="agent-http-push")

    @property
    def server_ready(self) -> bool:
        return self._server_ready

    async def wait_push_ready(self) -> None:
        """Wait for server-side subscriber registration, not just HTTP headers."""
        await asyncio.wait_for(self._push_ready.wait(), timeout=_CONNECT_TIMEOUT_SECONDS)

    async def _handle_push(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        frame: dict[str, Any],
    ) -> None:
        try:
            await handler(frame)
        except Exception:
            logger.exception("[HttpSseAgentServerClient] server_push 处理失败")

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            link_mtls = self._link_config()
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_s, connect=_CONNECT_TIMEOUT_SECONDS),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                follow_redirects=False,
                trust_env=False,
                **link_mtls.client_kwargs(role="agentserver"),
            )
            self._owns_http = True
        return self._http

    def _resolve_api_root(self, base_url: str | None) -> str:
        if base_url:
            # Enterprise routing may supply a per-Pod endpoint without calling
            # connect() first.  Apply the same downgrade protection here so an
            # enforce-mode request can never bypass HTTPS through base_url.
            base_url = self._link_config().resolve_endpoint(base_url, role="agentserver")
            return normalize_agent_http_base(base_url)
        if not self._running or not self._api_root:
            raise RuntimeError("未连接 AgentServer HTTP，请先调用 connect(uri)")
        return self._api_root

    async def connect(self, uri: str) -> None:
        uri = self._link_config().resolve_endpoint(uri, role="agentserver")
        if self._running:
            await self.disconnect()
        scheme = (urlsplit(uri).scheme or "").lower()
        if scheme not in {"http", "https"}:
            raise RuntimeError(
                "gateway.agent_client.type=http 需要 AGENT_SERVER_URL 为 http(s)://… "
                f"（当前 {uri!r}）。不要打 WS 口。Agent 须已开 http_server。"
            )
        self._base_url = uri.rstrip("/")
        self._api_root = normalize_agent_http_base(uri)
        self._link_config().require_secure_url(uri, label="AgentServer URL")
        self._ensure_http()
        health_url = f"{self._api_root}/health"
        logger.info("[HttpSseAgentServerClient] 正在连接: %s", health_url)
        response = await self._http.get(
            health_url,
            headers=self._request_headers({"Accept": "application/json"}),
        )
        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:  # noqa: BLE001
            payload = {}
        if response.status_code >= 400 or not payload.get("ok", False):
            raise RuntimeError(
                f"AgentServer HTTP health 失败: status={response.status_code} url={health_url} "
                "确认 Agent 已开 http_server.enabled / AGENT_HTTP_ENABLED"
            )
        self._server_ready = True
        self._running = True
        logger.info("[HttpSseAgentServerClient] health ok: %s", health_url)
        if self._on_server_push is not None and self._push_task is None:
            self._push_task = asyncio.create_task(self._push_loop(), name="agent-http-push")

    async def disconnect(self) -> None:
        self._running = False
        self._server_ready = False
        self._push_ready.clear()
        task = self._push_task
        self._push_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        handlers = list(self._push_handlers)
        for handler_task in handlers:
            handler_task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        self._push_handlers.clear()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
        self._base_url = None
        self._api_root = None
        self._cancelled_request_ids.clear()
        self._inflight_stream_ids.clear()
        logger.info("[HttpSseAgentServerClient] 已断开")

    async def send_request(
        self, envelope: E2AEnvelope, *, base_url: str | None = None
    ) -> AgentResponse:
        http = self._ensure_http()
        api_root = self._resolve_api_root(base_url)
        envelope.is_stream = False
        assembled = assemble_rest_request(envelope, base_url=api_root)
        channel_id = str(envelope.channel or "web")
        rid = str(envelope.request_id or "")
        logger.info(
            "[E2A][out][http][unary] request_id=%s method=%s %s %s rpc=%s",
            rid,
            envelope.method,
            assembled.verb,
            assembled.url,
            assembled.used_rpc_fallback,
        )
        response = await http.request(
            assembled.verb,
            assembled.url,
            headers=self._request_headers(assembled.headers),
            json=assembled.json_body,
            params=assembled.query,
        )
        _raise_for_pod_http_error(response, base_url=base_url)
        payload = _response_json(response, request_id=rid)
        return http_unary_to_agent_response(
            payload, channel_id=channel_id, request_id=rid
        )

    def _is_stream_cancelled(self, rid: str) -> bool:
        return bool(rid) and rid in self._cancelled_request_ids

    def _supersede_other_streams(self, rid: str) -> None:
        """新流开始时标记其它 in-flight rid，旧 SSE 残余不再 yield。"""
        for other in list(self._inflight_stream_ids):
            if other and other != rid:
                self._cancelled_request_ids.add(other)
                asyncio.create_task(self._delayed_cleanup_cancelled_request_id(other))

    async def _mark_stream_cancelled(self, rid: str) -> None:
        if not rid:
            return
        async with self._stream_lock:
            already = rid in self._cancelled_request_ids
            self._cancelled_request_ids.add(rid)
            self._inflight_stream_ids.discard(rid)
        if not already:
            asyncio.create_task(self._delayed_cleanup_cancelled_request_id(rid))

    async def _delayed_cleanup_cancelled_request_id(self, rid: str) -> None:
        try:
            await asyncio.sleep(_CANCELLED_RID_TTL_SECONDS)
            async with self._stream_lock:
                self._cancelled_request_ids.discard(rid)
        except Exception:
            logger.exception(
                "[HttpSseAgentServerClient] 清理已取消标记失败: request_id=%s",
                rid,
            )

    def _should_drop_stream_chunk(self, *, envelope_rid: str, chunk_rid: str) -> bool:
        if self._is_stream_cancelled(envelope_rid) or self._is_stream_cancelled(chunk_rid):
            return True
        return False

    async def send_request_stream(
        self, envelope: E2AEnvelope, *, base_url: str | None = None
    ) -> AsyncIterator[AgentResponseChunk]:
        http = self._ensure_http()
        api_root = self._resolve_api_root(base_url)
        envelope.is_stream = True
        assembled = assemble_rest_request(envelope, base_url=api_root)
        channel_id = str(envelope.channel or "web")
        rid = str(envelope.request_id or "")
        logger.info(
            "[E2A][out][http][stream] request_id=%s method=%s %s %s rpc=%s",
            rid,
            envelope.method,
            assembled.verb,
            assembled.url,
            assembled.used_rpc_fallback,
        )
        async with self._stream_lock:
            self._supersede_other_streams(rid)
            if rid:
                self._inflight_stream_ids.add(rid)
        timeout = httpx.Timeout(None, connect=_CONNECT_TIMEOUT_SECONDS)
        try:
            async with http.stream(
                assembled.verb,
                assembled.url,
                headers=self._request_headers(assembled.headers),
                json=assembled.json_body,
                params=assembled.query,
                timeout=timeout,
            ) as response:
                _raise_for_pod_http_error(response, base_url=base_url)
                if response.status_code >= 400:
                    body = await response.aread()
                    payload = _bytes_json(body, request_id=rid)
                    err = http_unary_to_agent_response(
                        payload, channel_id=channel_id, request_id=rid
                    )
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=channel_id,
                        payload=err.payload or {"error": "stream http error"},
                        is_complete=True,
                        metadata=err.metadata or {},
                    )
                    return
                async for frame in iter_sse_data_frames(response):
                    try:
                        chunk = parse_agent_server_wire_chunk(frame)
                    except Exception:
                        logger.exception(
                            "[HttpSseAgentServerClient] SSE 帧无法按 E2A chunk 解析 request_id=%s",
                            rid,
                        )
                        continue
                    if not chunk.channel_id:
                        chunk.channel_id = channel_id
                    chunk_rid = str(chunk.request_id or rid)
                    if self._should_drop_stream_chunk(
                        envelope_rid=rid, chunk_rid=chunk_rid
                    ):
                        logger.debug(
                            "[HttpSseAgentServerClient] 丢弃已取消流的残余 chunk: "
                            "request_id=%s chunk_request_id=%s",
                            rid,
                            chunk_rid,
                        )
                        continue
                    yield chunk
        except asyncio.CancelledError:
            logger.info("[HttpSseAgentServerClient] 流式接收被取消: request_id=%s", rid)
            raise
        finally:
            await self._mark_stream_cancelled(rid)

    async def _push_loop(self) -> None:
        while self._running and self._on_server_push is not None:
            api_root = self._api_root
            if not api_root:
                logger.warning(
                    "[HttpSseAgentServerClient] events/stream 缺少 API 根路径，停止推送循环"
                )
                return
            url = f"{api_root}/events/stream"
            try:
                http = self._ensure_http()
                timeout = httpx.Timeout(None, connect=_CONNECT_TIMEOUT_SECONDS)
                async with http.stream(
                    "GET",
                    url,
                    headers=self._request_headers(
                        {"X-Jiuwen-Push-Consumer": "gateway"}
                    ),
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                    async for frame in iter_sse_data_frames(response):
                        if frame.get("event_type") == "gateway.push_ready":
                            self._push_ready.set()
                            continue
                        meta = frame.get("metadata")
                        if not (isinstance(meta, dict) and meta.get(E2A_WIRE_SERVER_PUSH_KEY)):
                            continue
                        frame_rid = str(frame.get("request_id") or "")
                        if self._is_stream_cancelled(frame_rid):
                            logger.debug(
                                "[HttpSseAgentServerClient] 丢弃已取消 rid 的 server_push: "
                                "request_id=%s",
                                frame_rid,
                            )
                            continue
                        handler = self._on_server_push
                        if handler is None:
                            continue
                        if frame.get("response_kind") != E2A_RESPONSE_KIND_ACP_OUTPUT_REQUEST:
                            await self._handle_push(handler, frame)
                            continue
                        # Keep reading cancellation RPCs while a sync dispatch runs.
                        task = asyncio.create_task(self._handle_push(handler, frame))
                        self._push_handlers.add(task)
                        task.add_done_callback(self._push_handlers.discard)
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    return
                logger.warning(
                    "[HttpSseAgentServerClient] events/stream 断开，%.0fs 后重连: %s",
                    _PUSH_RETRY_SECONDS,
                    exc,
                )
            else:
                if not self._running:
                    return
                logger.info(
                    "[HttpSseAgentServerClient] events/stream 结束，%.0fs 后重连",
                    _PUSH_RETRY_SECONDS,
                )
            finally:
                self._push_ready.clear()
            await asyncio.sleep(_PUSH_RETRY_SECONDS)


def _raise_for_pod_http_error(
    response: httpx.Response, *, base_url: str | None
) -> None:
    """企业按次指定 Pod 时，5xx/429 抛给编排换 Pod；开源钉死那台仍转成 ok=False。"""
    if not base_url:
        return
    if response.status_code >= 500 or response.status_code == 429:
        response.raise_for_status()


def _response_json(response: httpx.Response, *, request_id: str) -> dict[str, Any]:
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass
    return {
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": "HTTP_ERROR",
            "message": f"status={response.status_code} non-json body",
            "details": {},
        },
    }


def _bytes_json(body: bytes, *, request_id: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
        if isinstance(parsed, dict):
            return parsed
    except Exception:  # noqa: BLE001
        pass
    return {
        "request_id": request_id,
        "ok": False,
        "error": {"code": "HTTP_ERROR", "message": "stream open failed", "details": {}},
    }
