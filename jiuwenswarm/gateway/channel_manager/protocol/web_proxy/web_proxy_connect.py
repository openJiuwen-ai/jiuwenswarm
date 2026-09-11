# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web proxy for 3rd-agent containers, mounted on WebChannel (:19000).

Shares the dual-protocol WebChannel FastAPI app (same port as ``/ws`` and
``/file-api``). Catch-all ``/<agent_type>/...`` is registered last so reserved
paths are not stolen.

The resolver returns a URL string (``http://...`` or ``ws://...``); this
module is protocol-agnostic and contains no backend-specific
(e.g. YuanRong) logic.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse

from jiuwenswarm.extensions.agentos.auth.common import (
    extract_token_from_path_and_headers,
    headers_to_dict,
)

logger = logging.getLogger(__name__)

WebResolver = Callable[[str, str, str], Awaitable[str | None]]
# (user_id, agent_type, protocol) → upstream_url | None
# protocol is "ws" or "http".

_HOP_BY_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
        # IAM credentials must not leak into the agent container.
        "authorization",
        "cookie",
        "x-token",
    }
)

# First-party frontend jump carries ?token= / Bearer; subsequent SPA
# requests (relative assets + WS) authenticate via this cookie.
WEB_PROXY_COOKIE_NAME = "agentos_web_token"
WEB_PROXY_COOKIE_MAX_AGE = 900

# First path segment of WebChannel routes that must not be treated as agent_type.
RESERVED_AGENT_TYPES = frozenset(
    {
        "ws",
        "file-api",
        "container-file-api",
        "docs",
        "redoc",
        "openapi.json",
        "health",
        "git",
    }
)

_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _append_tail(upstream_url: str, tail: str) -> str:
    """Append the request sub-path *tail* to the resolved upstream URL.

    - YuanRong proxy style URL (has query, e.g. ``...?instance=x&port=y``):
      append ``path=/<tail>`` query parameter.
    - Plain URL (no query): join tail as a path segment.
    """
    if not tail:
        return upstream_url
    parsed = urllib.parse.urlsplit(upstream_url)
    if parsed.query:
        query = parsed.query + "&" + urllib.parse.urlencode({"path": "/" + tail})
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
        )
    path = parsed.path.rstrip("/") + "/" + tail
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _apply_referer_fallback(
    *,
    agent_type: str,
    user_id: str,
    referer: str,
) -> tuple[str, str]:
    if (user_id and agent_type) or not referer:
        return agent_type, user_id
    try:
        ref = urllib.parse.urlparse(referer)
        ref_parts = [p for p in ref.path.strip("/").split("/") if p]
        ref_qs = dict(urllib.parse.parse_qsl(ref.query))
        if not agent_type and ref_parts:
            agent_type = ref_parts[0].strip().lower()
        if not user_id:
            user_id = ref_qs.get("user_id", "").strip()
    except Exception:
        return agent_type, user_id
    return agent_type, user_id


def _cookie_token(headers: Mapping[str, str]) -> str:
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "cookie":
            raw = str(value or "")
            break
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get(WEB_PROXY_COOKIE_NAME)
    return str(morsel.value if morsel is not None else "").strip()


def _referer_token(referer: str) -> str:
    if not referer:
        return ""
    try:
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(referer).query))
    except Exception:
        return ""
    return str(qs.get("token") or "").strip()


def _extract_web_proxy_token(path: str, headers: Mapping[str, str], referer: str) -> str:
    """Token sources for a frontend jump, then SPA follow-up.

    1. ``?token=`` / ``Authorization: Bearer`` / ``X-Token`` (file-api order)
    2. ``agentos_web_token`` cookie (set after the first successful jump)
    3. Referer ``?token=`` (relative assets before the cookie lands)
    """
    token = extract_token_from_path_and_headers(path, headers)
    if token and str(token).strip():
        return str(token).strip()
    cookie = _cookie_token(headers)
    if cookie:
        return cookie
    return _referer_token(referer)


def _auth_headers_with_token(
    path: str,
    headers: Mapping[str, str],
    token: str,
) -> dict[str, str]:
    mapped = headers_to_dict(headers)
    if token and not extract_token_from_path_and_headers(path, mapped):
        mapped["Authorization"] = f"Bearer {token}"
    return mapped


def _full_request_path(path: str, query: str) -> str:
    path = path or ""
    query = query or ""
    return f"{path}?{query}" if query else path


def _client_remote(client: Any) -> str:
    host = str(getattr(client, "host", "") or "").strip()
    port = getattr(client, "port", None)
    if host and port:
        return f"{host}:{port}"
    return host


def _auth_client_from_channel(channel: Any) -> Any | None:
    for attr in ("container_file_client", "agent_client"):
        client = getattr(channel, attr, None)
        if client is not None and callable(getattr(client, "authenticate_http", None)):
            return client
    return None


class WebProxyAuthError(Exception):
    def __init__(self, status_code: int, error: str, code: str) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.code = code


def _auth_error_response(exc: WebProxyAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "code": exc.code},
    )


def _set_session_cookie(response: Response, token: str | None) -> Response:
    if not token:
        return response
    response.set_cookie(
        key=WEB_PROXY_COOKIE_NAME,
        value=token,
        max_age=WEB_PROXY_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def _slash_redirect_location(request: Request, user_id: str) -> str:
    path = (request.url.path or "") + "/"
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    keys = {k for k, _ in pairs}
    if user_id and "user_id" not in keys:
        pairs.append(("user_id", user_id))
    qs = urllib.parse.urlencode(pairs)
    return path + ("?" + qs if qs else "")


async def _authenticate_web_proxy(
    channel: Any,
    *,
    path: str,
    headers: Mapping[str, str],
    remote: str,
    query_user_id: str,
    referer: str,
) -> tuple[str, str | None]:
    """Return ``(user_id, session_token)``.

    When IAM is off, ``user_id`` is the query/Referer value (may be empty).
    When IAM is on, the token identity wins; a claimed ``user_id`` must match.
    """
    client = _auth_client_from_channel(channel)
    if client is None or not getattr(client, "auth_enabled", False):
        return query_user_id, None

    token = _extract_web_proxy_token(path, headers, referer)
    result = await client.authenticate_http(
        path=path,
        headers=_auth_headers_with_token(path, headers, token),
        remote=remote,
        channel="web-proxy",
    )
    if not result.success:
        error_code = ""
        if isinstance(result.extensions, dict):
            error_code = str(result.extensions.get("error_code") or "")
        raise WebProxyAuthError(
            401,
            result.error or "unauthorized",
            error_code or "UNAUTHORIZED",
        )

    iam_uid = str(result.user_id or "").strip()
    username = ""
    if isinstance(result.extensions, dict):
        username = str(result.extensions.get("username") or "").strip()
    identities = {value for value in (iam_uid, username) if value}
    claimed = (query_user_id or "").strip()
    if claimed and identities and claimed not in identities:
        raise WebProxyAuthError(403, "user_id 与 token 不匹配", "USER_MISMATCH")

    uid = iam_uid or claimed
    if not uid:
        raise WebProxyAuthError(400, "user_id is required", "BAD_REQUEST")
    return uid, token or None


async def _ensure_proxy_session(channel: Any) -> aiohttp.ClientSession:
    session = getattr(channel, "_web_proxy_session", None)
    if session is None or session.closed:
        session = aiohttp.ClientSession()
        channel._web_proxy_session = session
    return session


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        forwarded[key] = value
    return forwarded


@dataclass
class WebProxyChannelConfig:
    """Web proxy feature flag (traffic is served on WebChannel :19000)."""

    enabled: bool = False

    @classmethod
    def from_dict(cls, conf: dict[str, Any] | None) -> WebProxyChannelConfig:
        if not conf:
            return cls()
        return cls(enabled=bool(conf.get("enabled", False)))


def attach_web_proxy_routes(app: FastAPI, channel: Any) -> None:
    """Mount catch-all agent Web proxy routes last on the WebChannel app.

    Handlers read ``channel.web_proxy_enabled`` and ``channel.web_resolver``
    at request time so config can flip without rebuilding the ASGI app.
    """

    async def _http_proxy(
        request: Request,
        agent_type: str,
        tail: str = "",
    ) -> Response:
        return await _handle_http_proxy(
            channel,
            request,
            agent_type=agent_type,
            tail=tail,
        )

    async def _ws_proxy(websocket: WebSocket, agent_type: str, tail: str = "") -> None:
        await _handle_ws_proxy(
            channel,
            websocket,
            agent_type=agent_type,
            tail=tail,
        )

    app.add_api_route(
        "/{agent_type}",
        _http_proxy,
        methods=_HTTP_METHODS,
        include_in_schema=False,
    )
    app.add_api_route(
        "/{agent_type}/{tail:path}",
        _http_proxy,
        methods=_HTTP_METHODS,
        include_in_schema=False,
    )
    app.add_api_websocket_route("/{agent_type}", _ws_proxy)
    app.add_api_websocket_route("/{agent_type}/{tail:path}", _ws_proxy)
    logger.info(
        "[WebProxy] routes mounted on WebChannel: /{agent_type}/... "
        "(shares :%s with /ws and /file-api)",
        getattr(getattr(channel, "config", None), "port", 19000),
    )


async def _handle_http_proxy(
    channel: Any,
    request: Request,
    *,
    agent_type: str,
    tail: str,
) -> Response:
    agent_type = (agent_type or "").strip().lower()
    tail = (tail or "").strip("/")
    if agent_type in RESERVED_AGENT_TYPES:
        return PlainTextResponse("not found", status_code=404)
    if not getattr(channel, "web_proxy_enabled", False):
        return PlainTextResponse("not found", status_code=404)

    referer = request.headers.get("referer") or request.headers.get("Referer") or ""
    query_user_id = (request.query_params.get("user_id") or "").strip()
    agent_type, query_user_id = _apply_referer_fallback(
        agent_type=agent_type,
        user_id=query_user_id,
        referer=referer,
    )

    auth_path = _full_request_path(str(request.url.path or ""), str(request.url.query or ""))
    try:
        user_id, session_token = await _authenticate_web_proxy(
            channel,
            path=auth_path,
            headers=request.headers,
            remote=_client_remote(request.client),
            query_user_id=query_user_id,
            referer=referer,
        )
    except WebProxyAuthError as exc:
        return _auth_error_response(exc)

    if not tail:
        path = request.url.path or ""
        if not path.endswith("/"):
            location = _slash_redirect_location(request, user_id)
            return _set_session_cookie(
                RedirectResponse(url=location, status_code=301),
                session_token,
            )

    if not agent_type:
        return PlainTextResponse("agent_type is required in path", status_code=400)
    if not user_id:
        return PlainTextResponse("user_id query parameter is required", status_code=400)

    resolver = getattr(channel, "web_resolver", None)
    if not callable(resolver):
        return PlainTextResponse("web resolver is not configured", status_code=503)

    endpoint = await resolver(user_id, agent_type, "http")
    if not endpoint:
        return PlainTextResponse(
            f"agent not found or no http endpoint: user={user_id} agent_type={agent_type}",
            status_code=404,
        )

    session = await _ensure_proxy_session(channel)
    upstream_url = _append_tail(endpoint, tail)
    body = await request.body()
    logger.info(
        "[WebProxy] HTTP proxy: agent=%s method=%s -> %s",
        agent_type,
        request.method,
        upstream_url,
    )
    try:
        ctx = session.request(
            request.method,
            upstream_url,
            headers=_forward_headers(request.headers),
            data=body if body else None,
        )
        upstream_resp = await ctx.__aenter__()
    except aiohttp.ClientError as exc:
        logger.warning("[WebProxy] HTTP proxy failed: %s -> %s", upstream_url, exc)
        return PlainTextResponse(f"upstream error: {exc}", status_code=502)

    content_type = upstream_resp.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:

        async def _stream():
            try:
                async for chunk in upstream_resp.content.iter_any():
                    yield chunk
            finally:
                await ctx.__aexit__(None, None, None)

        return _set_session_cookie(
            StreamingResponse(
                _stream(),
                status_code=upstream_resp.status,
                media_type=content_type,
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            ),
            session_token,
        )

    try:
        resp_body = await upstream_resp.read()
        media = upstream_resp.content_type
        status = upstream_resp.status
    finally:
        await ctx.__aexit__(None, None, None)
    return _set_session_cookie(
        Response(content=resp_body, status_code=status, media_type=media),
        session_token,
    )


async def _handle_ws_proxy(
    channel: Any,
    websocket: WebSocket,
    *,
    agent_type: str,
    tail: str,
) -> None:
    agent_type = (agent_type or "").strip().lower()
    tail = (tail or "").strip("/")
    if agent_type in RESERVED_AGENT_TYPES or not getattr(channel, "web_proxy_enabled", False):
        await websocket.close(code=1008, reason="not found")
        return

    referer = websocket.headers.get("referer") or websocket.headers.get("Referer") or ""
    query_user_id = (websocket.query_params.get("user_id") or "").strip()
    agent_type, query_user_id = _apply_referer_fallback(
        agent_type=agent_type,
        user_id=query_user_id,
        referer=referer,
    )
    auth_path = _full_request_path(
        str(websocket.url.path or ""),
        str(websocket.url.query or ""),
    )
    try:
        user_id, _session_token = await _authenticate_web_proxy(
            channel,
            path=auth_path,
            headers=websocket.headers,
            remote=_client_remote(websocket.client),
            query_user_id=query_user_id,
            referer=referer,
        )
    except WebProxyAuthError:
        await websocket.close(code=1008, reason="unauthorized")
        return
    if not agent_type or not user_id:
        await websocket.close(code=1008, reason="agent_type and user_id are required")
        return

    resolver = getattr(channel, "web_resolver", None)
    if not callable(resolver):
        await websocket.close(code=1013, reason="web resolver is not configured")
        return

    endpoint = await resolver(user_id, agent_type, "ws")
    if not endpoint:
        await websocket.close(code=1008, reason="agent not found")
        return

    session = await _ensure_proxy_session(channel)
    upstream_url = _append_tail(endpoint, tail)
    await websocket.accept()
    logger.info("[WebProxy] WS tunnel: agent=%s -> %s", agent_type, upstream_url)
    try:
        async with session.ws_connect(upstream_url) as ws_upstream:
            await asyncio.gather(
                _pump_starlette_to_upstream(websocket, ws_upstream),
                _pump_upstream_to_starlette(ws_upstream, websocket),
                return_exceptions=True,
            )
    except aiohttp.ClientError as exc:
        logger.warning("[WebProxy] WS proxy failed: %s -> %s", upstream_url, exc)
    finally:
        if websocket.client_state.name != "DISCONNECTED":
            try:
                await websocket.close()
            except Exception:
                pass


async def _pump_starlette_to_upstream(
    src: WebSocket,
    dst: aiohttp.ClientWebSocketResponse,
) -> None:
    try:
        while True:
            msg = await src.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            data = msg.get("bytes")
            if text is not None:
                await dst.send_str(text)
            elif data is not None:
                await dst.send_bytes(data)
    except WebSocketDisconnect:
        return


async def _pump_upstream_to_starlette(
    src: aiohttp.ClientWebSocketResponse,
    dst: WebSocket,
) -> None:
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_text(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            break
