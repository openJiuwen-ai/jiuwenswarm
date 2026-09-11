# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Web proxy for 3rd-agent containers, mounted on WebChannel (:19000).

Shares the dual-protocol WebChannel FastAPI app (same port as ``/ws`` and
``/file-api``). Catch-all ``/<agent_type>/...`` is registered last so reserved
paths are not stolen.

The resolver returns a URL string (``http://...`` or ``ws://...``); this
module is protocol-agnostic and contains no backend-specific
(e.g. YuanRong) logic. Optional ``channel.web_runtime_release`` drops the
sandbox task hold after the HTTP/WS proxy finishes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
        "x-user-id",
        # Rebuild client attribution for OpenClaw / similar UIs.
        "x-forwarded-for",
        "x-real-ip",
        "forwarded",
        "x-forwarded-user",
    }
)

# First-party frontend jump carries ?access_token= / ?token= / Bearer;
# subsequent SPA requests (relative assets + WS) use this cookie.
WEB_PROXY_COOKIE_NAME = "access_token"
WEB_PROXY_USER_COOKIE = "user_id"
WEB_PROXY_COOKIE_MAX_AGE = 900
_TOKEN_QUERY_KEYS = frozenset({"access_token", "token"})

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

# OpenClaw (and similar SPAs) emit root-absolute `/assets/...` and icons.
# Those must not be treated as agent_type when Referer already names the agent.
_STATIC_FIRST_SEGMENTS = frozenset(
    {
        "assets",
        "static",
        "favicon.ico",
        "favicon.svg",
        "robots.txt",
        "manifest.webmanifest",
        "manifest.json",
    }
)
_ROOT_ASSET_ATTR = re.compile(
    r"""((?:src|href)=["'])(/(?:assets/|favicon[^"']*|apple-touch-icon[^"']*|manifest\.webmanifest)[^"']*)(["'])""",
    re.IGNORECASE,
)

_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _append_tail(upstream_url: str, tail: str) -> str:
    """Join *tail* onto the upstream path; keep existing query as-is.

    YuanRong frontend: ``/serverless/v1/http?instance=&port=&tenant_id=`` plus
    ``api/foo`` becomes ``/serverless/v1/http/api/foo?instance=&port=&tenant_id=``.
    """
    if not tail:
        return upstream_url
    parsed = urllib.parse.urlsplit(upstream_url)
    path = parsed.path.rstrip("/") + "/" + str(tail).lstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _looks_like_static_first_segment(seg: str) -> bool:
    value = (seg or "").strip().lower()
    if not value or value in RESERVED_AGENT_TYPES:
        return False
    if value in _STATIC_FIRST_SEGMENTS:
        return True
    return "." in value


def _apply_referer_fallback(
    *,
    agent_type: str,
    user_id: str,
    tail: str = "",
    referer: str,
) -> tuple[str, str, str]:
    """Fill user_id / agent_type from Referer; remap SPA `/assets` onto the agent prefix."""
    if not referer:
        return agent_type, user_id, tail
    try:
        ref = urllib.parse.urlparse(referer)
        ref_parts = [p for p in ref.path.strip("/").split("/") if p]
        ref_qs = dict(urllib.parse.parse_qsl(ref.query))
        ref_agent = (ref_parts[0].strip().lower() if ref_parts else "")
        if ref_agent in RESERVED_AGENT_TYPES:
            ref_agent = ""
        if not user_id:
            user_id = ref_qs.get("user_id", "").strip()
        if _looks_like_static_first_segment(agent_type) and ref_agent:
            extra = "/".join(part for part in (agent_type, tail) if part)
            return ref_agent, user_id, extra
        if not agent_type and ref_agent:
            agent_type = ref_agent
    except Exception:
        return agent_type, user_id, tail
    return agent_type, user_id, tail


def _rewrite_agent_html(body: bytes, agent_type: str) -> bytes:
    """Prefix root-absolute SPA assets so the browser stays under /{agent_type}/."""
    prefix = "/" + (agent_type or "").strip("/")
    if prefix == "/":
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    empty_base = 'data-openclaw-control-ui-base-path=""'
    if empty_base in text:
        text = text.replace(empty_base, f'data-openclaw-control-ui-base-path="{prefix}"', 1)

    def _sub(match: re.Match[str]) -> str:
        url = match.group(2)
        if url.startswith(prefix + "/") or url == prefix:
            return match.group(0)
        return f"{match.group(1)}{prefix}{url}{match.group(3)}"

    return _ROOT_ASSET_ATTR.sub(_sub, text).encode("utf-8")


def _cookie_named(headers: Mapping[str, str], name: str) -> str:
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
    morsel = jar.get(name)
    return str(morsel.value if morsel is not None else "").strip()


def _cookie_token(headers: Mapping[str, str]) -> str:
    return _cookie_named(headers, WEB_PROXY_COOKIE_NAME)


def _query_access_token(query: str) -> str:
    try:
        qs = dict(urllib.parse.parse_qsl(query or "", keep_blank_values=False))
    except Exception:
        return ""
    return str(qs.get("access_token") or qs.get("token") or "").strip()


def _referer_token(referer: str) -> str:
    if not referer:
        return ""
    try:
        query = urllib.parse.urlparse(referer).query
    except Exception:
        return ""
    return _query_access_token(query)


def _extract_web_proxy_token(path: str, headers: Mapping[str, str], referer: str) -> str:
    """Token sources for a frontend jump, then SPA follow-up.

    1. ``?access_token=`` / ``?token=`` / ``Authorization: Bearer`` / ``X-Token``
    2. ``access_token`` cookie (set after the first successful jump)
    3. Referer ``?access_token=`` / ``?token=`` (relative assets before the cookie lands)
    """
    parsed = urllib.parse.urlparse(path or "")
    token = _query_access_token(parsed.query)
    if not token:
        token = (extract_token_from_path_and_headers(path, headers) or "").strip()
    if token:
        return token
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


def _retry_after_seconds(exc: BaseException) -> int | None:
    raw = getattr(exc, "retry_after_seconds", None)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _sandbox_creating_response(exc: BaseException) -> PlainTextResponse:
    seconds = _retry_after_seconds(exc) or 5
    return PlainTextResponse(
        "agent sandbox is creating",
        status_code=503,
        headers={"Retry-After": str(seconds)},
    )


def _set_session_cookie(
    response: Response,
    token: str | None,
    user_id: str = "",
) -> Response:
    if token:
        response.set_cookie(
            key=WEB_PROXY_COOKIE_NAME,
            value=token,
            max_age=WEB_PROXY_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
    if user_id:
        response.set_cookie(
            key=WEB_PROXY_USER_COOKIE,
            value=user_id,
            max_age=WEB_PROXY_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _slash_redirect_location(request: Request, user_id: str) -> str:
    path = (request.url.path or "") + "/"
    pairs = [
        (k, v)
        for k, v in request.query_params.multi_items()
        if k not in _TOKEN_QUERY_KEYS
    ]
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
    if not getattr(channel, "web_proxy_auth_enabled", True):
        return query_user_id, None
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

    # Prefer claimed / username so workspace stays /home/agentos/users/<name>
    # (same as /file-api). IAM UUID has no host workspace on this plane.
    uid = claimed or username or iam_uid
    if not uid:
        raise WebProxyAuthError(400, "user_id is required", "BAD_REQUEST")
    return uid, token or None


async def _ensure_proxy_session(channel: Any) -> aiohttp.ClientSession:
    """Reuse one aiohttp session per WebChannel; create it under the channel lock."""
    ensure = getattr(channel, "ensure_web_proxy_session", None)
    if callable(ensure):
        return await ensure()
    session = getattr(channel, "web_proxy_session", None)
    if session is not None and not getattr(session, "closed", False):
        return session
    created = aiohttp.ClientSession()
    current = getattr(channel, "web_proxy_session", None)
    if current is None or getattr(current, "closed", False):
        channel.web_proxy_session = created
        return created
    await created.close()
    return current


async def _release_web_runtime(channel: Any, user_id: str, agent_type: str) -> None:
    """Drop the sandbox hold taken by a successful web_resolver call."""
    releaser = getattr(channel, "web_runtime_release", None)
    if not callable(releaser):
        return
    try:
        await releaser(user_id, agent_type)
    except Exception:
        logger.exception(
            "[WebProxy] release web runtime failed: user=%s agent=%s",
            user_id,
            agent_type,
        )


def _is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in {"", "localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _forward_headers(
    headers: Mapping[str, str],
    *,
    client_host: str = "",
    user_id: str = "",
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        forwarded[key] = value
    # YuanRong sets X-Forwarded-Proto; OpenClaw then requires a trusted
    # proxy plus a non-loopback client address that is not the sandbox
    # host's own IP (same-host curls use GATEWAY_HOST and get 403).
    peer = client_host.strip()
    if _is_loopback_host(peer):
        peer = str(os.environ.get("GATEWAY_HOST") or "").strip()
    if peer and not _is_loopback_host(peer):
        gateway_ip = str(os.environ.get("GATEWAY_HOST") or "").strip()
        if gateway_ip and peer == gateway_ip:
            forwarded["X-Forwarded-For"] = f"192.0.2.1, {peer}"
        else:
            forwarded["X-Forwarded-For"] = peer
    uid = (user_id or "").strip()
    if uid:
        forwarded["X-Forwarded-User"] = uid
    return forwarded


def _forward_ws_headers(
    headers: Mapping[str, str],
    *,
    client_host: str = "",
    user_id: str = "",
) -> dict[str, str]:
    """Headers for the upstream WS handshake (YuanRong → OpenClaw)."""
    forwarded = _forward_headers(headers, client_host=client_host, user_id=user_id)
    for key in list(forwarded):
        if key.lower().startswith("sec-websocket-"):
            forwarded.pop(key, None)
    if not any(k.lower() == "origin" for k in forwarded):
        referer = ""
        for key, value in headers.items():
            if str(key).lower() == "referer":
                referer = str(value or "")
                break
        origin = ""
        if referer:
            parsed = urllib.parse.urlsplit(referer)
            if parsed.scheme and parsed.netloc:
                origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if not origin:
            host = str(os.environ.get("GATEWAY_HOST") or "").strip()
            port = str(os.environ.get("WEB_PORT") or "19000").strip()
            if host:
                origin = f"http://{host}:{port}"
        if origin:
            forwarded["Origin"] = origin
    return forwarded


@dataclass
class WebProxyChannelConfig:
    """Web proxy feature flag (traffic is served on WebChannel :19000)."""

    enabled: bool = False
    auth_enabled: bool = True

    @classmethod
    def from_dict(cls, conf: dict[str, Any] | None) -> WebProxyChannelConfig:
        if not conf:
            return cls()
        return cls(
            enabled=bool(conf.get("enabled", False)),
            auth_enabled=bool(conf.get("auth_enabled", True)),
        )


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
    agent_type, query_user_id, tail = _apply_referer_fallback(
        agent_type=agent_type,
        user_id=query_user_id,
        tail=tail,
        referer=referer,
    )
    if not query_user_id:
        query_user_id = _cookie_named(request.headers, WEB_PROXY_USER_COOKIE)

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
                user_id,
            )

    if not agent_type:
        return PlainTextResponse("agent_type is required in path", status_code=400)
    if not user_id:
        return PlainTextResponse("user_id query parameter is required", status_code=400)

    resolver = getattr(channel, "web_resolver", None)
    if not callable(resolver):
        return PlainTextResponse("web resolver is not configured", status_code=503)

    held = False
    defer_release = False
    try:
        try:
            endpoint = await resolver(user_id, agent_type, "http")
        except Exception as exc:
            if _retry_after_seconds(exc) is not None:
                return _sandbox_creating_response(exc)
            raise
        if not endpoint:
            return PlainTextResponse(
                f"agent not found or no http endpoint: user={user_id} agent_type={agent_type}",
                status_code=404,
            )
        held = True

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
                headers=_forward_headers(
                    request.headers,
                    client_host=str(getattr(request.client, "host", "") or ""),
                    user_id=user_id,
                ),
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
                    await _release_web_runtime(channel, user_id, agent_type)

            defer_release = True
            return _set_session_cookie(
                StreamingResponse(
                    _stream(),
                    status_code=upstream_resp.status,
                    media_type=content_type,
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                ),
                session_token,
                user_id,
            )

        try:
            resp_body = await upstream_resp.read()
            media = upstream_resp.content_type
            status = upstream_resp.status
        finally:
            await ctx.__aexit__(None, None, None)
        if media and "text/html" in media:
            resp_body = _rewrite_agent_html(resp_body, agent_type)
        return _set_session_cookie(
            Response(content=resp_body, status_code=status, media_type=media),
            session_token,
            user_id,
        )
    finally:
        if held and not defer_release:
            await _release_web_runtime(channel, user_id, agent_type)


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
    agent_type, query_user_id, tail = _apply_referer_fallback(
        agent_type=agent_type,
        user_id=query_user_id,
        tail=tail,
        referer=referer,
    )
    if not query_user_id:
        query_user_id = _cookie_named(websocket.headers, WEB_PROXY_USER_COOKIE)
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

    held = False
    try:
        try:
            endpoint = await resolver(user_id, agent_type, "ws")
        except Exception as exc:
            if _retry_after_seconds(exc) is not None:
                await websocket.close(code=1013, reason="agent sandbox is creating")
                return
            raise
        if not endpoint:
            await websocket.close(code=1008, reason="agent not found")
            return
        held = True

        session = await _ensure_proxy_session(channel)
        upstream_url = _append_tail(endpoint, tail)
        ws_headers = _forward_ws_headers(
            websocket.headers,
            client_host=str(getattr(websocket.client, "host", "") or ""),
            user_id=user_id,
        )
        logger.info("[WebProxy] WS tunnel: agent=%s -> %s", agent_type, upstream_url)
        try:
            ws_ctx = session.ws_connect(upstream_url, headers=ws_headers)
            ws_upstream = await ws_ctx.__aenter__()
        except aiohttp.ClientError as exc:
            logger.warning("[WebProxy] WS proxy failed: %s -> %s", upstream_url, exc)
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close(code=1011, reason="upstream websocket failed")
                except Exception:
                    logger.debug(
                        "[WebProxy] close client websocket after upstream fail ignored",
                        exc_info=True,
                    )
            return
        await websocket.accept()
        tasks = [
            asyncio.create_task(
                _pump_starlette_to_upstream(websocket, ws_upstream)
            ),
            asyncio.create_task(
                _pump_upstream_to_starlette(ws_upstream, websocket)
            ),
        ]
        try:
            _done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            try:
                await ws_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("[WebProxy] close upstream websocket ignored", exc_info=True)
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close()
                except Exception:
                    logger.debug(
                        "[WebProxy] close client websocket after tunnel ignored",
                        exc_info=True,
                    )
    finally:
        if held:
            await _release_web_runtime(channel, user_id, agent_type)


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
