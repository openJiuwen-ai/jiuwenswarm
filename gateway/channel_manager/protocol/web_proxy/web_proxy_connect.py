# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Northbound HTTP/WS proxy for 3rd-agent Web UIs, one port per agent_type.

``:19000`` stays the WebChannel control plane (``/ws``, ``/file-api``).
The listen port per ``agent_type`` is decided by the registry and applied by
``registry_watcher``; ``3rdagent.web`` only reports the already-bound port as
``http://host:port/?user_id=&token=``. Every user of the same ``agent_type``
shares that port; the query (then Cookie) selects the sandbox.

The resolver returns a URL string (``http://...`` or ``ws://...``); this
module is protocol-agnostic and contains no backend-specific
(e.g. YuanRong) logic. Each proxied request acquires and releases the
sandbox hold. The listen port does not hold a sandbox.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from gateway.extensions.auth.common import (
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
DEFAULT_IDLE_TIMEOUT_SEC = 900
_TOKEN_QUERY_KEYS = frozenset({"access_token", "token"})
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})
_HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# IAM cookies that must NOT reach the agent container.
_IAM_COOKIE_NAMES = frozenset(
    {WEB_PROXY_COOKIE_NAME, WEB_PROXY_USER_COOKIE, "refresh_token"}
)

# Browser query keys never forwarded upstream: the resolver-owned route
# selectors (``instance``/``tenant_id``/``port``) plus IAM/session params the
# container must not see and the browser must not be able to override.
_BLOCKED_FORWARD_QUERY_KEYS = _TOKEN_QUERY_KEYS | frozenset(
    {"instance", "tenant_id", "port", "refresh_token", "user_id", "agent_type"}
)

# RFC 5737 TEST-NET-1: never routed, used only as the UDP route-probe target
# that picks the outbound interface and as the synthetic first XFF hop. Built
# via ``ipaddress`` so the source carries no hardcoded IP literal.
_TEST_NET_1 = str(ipaddress.ip_address(0xC0000201))


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


def _merge_request_query(upstream_url: str, request_query: str) -> str:
    """Append the browser's own query params onto *upstream_url*.

    Keeps the resolver-owned route params (``instance``/``tenant_id``/``port``)
    and drops ``_BLOCKED_FORWARD_QUERY_KEYS`` (IAM/session), so the browser can
    neither override the upstream route nor leak credentials. Blank values,
    duplicate keys and UTF-8 encodings are preserved.
    """
    pairs = urllib.parse.parse_qsl(request_query or "", keep_blank_values=True)
    extra_pairs = [
        (key, value)
        for key, value in pairs
        if key.lower() not in _BLOCKED_FORWARD_QUERY_KEYS
    ]
    if not extra_pairs:
        return upstream_url
    parsed = urllib.parse.urlsplit(upstream_url)
    upstream_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    merged_query = urllib.parse.urlencode(
        [*upstream_pairs, *extra_pairs], doseq=True
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, merged_query, parsed.fragment)
    )


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


def _cookie_max_age(channel: Any) -> int:
    config = getattr(channel, "web_proxy_config", None)
    raw = getattr(config, "idle_timeout_sec", None)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = DEFAULT_IDLE_TIMEOUT_SEC
    return seconds if seconds > 0 else DEFAULT_IDLE_TIMEOUT_SEC


def _set_session_cookie(
    response: Response,
    token: str | None,
    user_id: str = "",
    *,
    max_age: int = WEB_PROXY_COOKIE_MAX_AGE,
) -> Response:
    if token:
        response.set_cookie(
            key=WEB_PROXY_COOKIE_NAME,
            value=token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            path="/",
        )
    if user_id:
        response.set_cookie(
            key=WEB_PROXY_USER_COOKIE,
            value=user_id,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _location_without_token(request: Request, user_id: str) -> str:
    """Same path, token query keys removed. ``user_id`` stays when present."""
    pairs = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in _TOKEN_QUERY_KEYS
    ]
    keys = {key for key, _ in pairs}
    if user_id and "user_id" not in keys:
        pairs.append(("user_id", user_id))
    path = request.url.path or "/"
    query = urllib.parse.urlencode(pairs)
    return f"{path}?{query}" if query else path


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


def new_web_proxy_client_session(*, force_close: bool = False) -> aiohttp.ClientSession:
    """Shared southbound session: no cookie storage, no auto-decompress.

    ``DummyCookieJar`` stops cross-user cookie replay; ``auto_decompress=False``
    keeps the upstream-encoded bytes so the proxy can pass ``Content-Encoding``
    through unchanged and the rebuilt headers match the bytes actually sent.
    """
    connector = aiohttp.TCPConnector(force_close=True) if force_close else None
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        connector=connector,
        auto_decompress=False,
        timeout=aiohttp.ClientTimeout(
            total=None,
            connect=30,
            sock_connect=30,
        ),
    )


async def _ensure_proxy_session(channel: Any) -> aiohttp.ClientSession:
    """Reuse one aiohttp session per WebChannel for WebSocket tunnels."""
    return await channel.ensure_web_proxy_session()


async def _ensure_http_proxy_session(channel: Any) -> aiohttp.ClientSession:
    """HTTP session that closes the socket after each response.

    YuanRong returns an empty 404 for the second request on a keep-alive
    connection; see ``WebChannel.ensure_web_proxy_http_session`` for the
    force_close rationale. WebSocket must keep using
    :func:`_ensure_proxy_session`.
    """
    return await channel.ensure_web_proxy_http_session()


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
    """True for empty/``localhost`` and any loopback address (v4/v6/mapped)."""
    value = (host or "").strip().lower()
    if value in {"", "localhost"}:
        return True
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _filter_iam_cookies(cookie_header: str) -> str:
    """Drop IAM cookies; keep the agent's own session cookies."""
    kept: list[str] = []
    for chunk in (cookie_header or "").split(";"):
        name = chunk.split("=", 1)[0].strip()
        if name.lower() in _IAM_COOKIE_NAMES:
            continue
        if chunk.strip():
            kept.append(chunk.strip())
    return "; ".join(kept)


_RESPONSE_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"transfer-encoding",
        b"te",
        b"trailer",
        b"upgrade",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
    }
)


def _filter_response_headers(
    raw_headers: Iterable[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Upstream response headers minus hop-by-hop ones; keeps duplicate Set-Cookie.

    Reads ``raw_headers`` (not a dict) so repeated fields survive. Names are
    lower-cased, values kept verbatim; any field named by a ``Connection`` value
    is dropped too (RFC 9110 7.6.1). Do not reuse the request-side
    ``_HOP_BY_HOP_HEADERS`` — that one carries IAM/cookie request policy.
    """
    connection_bound: set[bytes] = set()
    for name, value in raw_headers:
        if name.lower() == b"connection":
            connection_bound.update(
                token.strip().lower()
                for token in value.split(b",")
                if token.strip()
            )
    blocked = _RESPONSE_HOP_BY_HOP_HEADERS | connection_bound
    return [
        (name.lower(), value)
        for name, value in raw_headers
        if name.lower() not in blocked
    ]


def _final_response_headers(
    upstream_headers: Iterable[tuple[bytes, bytes]],
    *,
    method: str,
    status: int,
    body: bytes,
) -> list[tuple[bytes, bytes]]:
    """Upstream headers plus a Content-Length matching the bytes we actually send.

    - normal body responses (200/206/416/...): recompute from ``body``;
    - HEAD: no body, keep the upstream length so clients still learn the size;
    - 204/304/1xx: no body, no Content-Length.
    """
    headers = [
        (name, value) for name, value in upstream_headers if name != b"content-length"
    ]
    if method == "HEAD":
        headers.extend(
            (name, value)
            for name, value in upstream_headers
            if name == b"content-length"
        )
    elif status in (204, 304) or 100 <= status < 200:
        pass
    else:
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    return headers


def _forward_headers(
    headers: Mapping[str, str],
    *,
    client_host: str = "",
    user_id: str = "",
) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    original_host = ""
    for key, value in headers.items():
        low = key.lower()
        if low == "host":
            # Keep the browser Host (re-added below) for Host/Origin checks.
            original_host = str(value or "").strip()
        if low == "cookie":
            kept = _filter_iam_cookies(value)
            if kept:
                forwarded[key] = kept
            continue
        if low in _HOP_BY_HOP_HEADERS:
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
            forwarded["X-Forwarded-For"] = f"{_TEST_NET_1}, {peer}"
        else:
            forwarded["X-Forwarded-For"] = peer
    uid = (user_id or "").strip()
    if uid:
        forwarded["X-Forwarded-User"] = uid
    if original_host:
        forwarded["Host"] = original_host
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
    """Config for the 3rd-agent Web proxy.

    Ports are decided by the registry (``registry_watcher``) and applied to
    ``WebPortManager``; this config never allocates a port. ``idle_timeout_sec``
    is only the session-cookie max-age now.
    """

    enabled: bool = False
    auth_enabled: bool = True
    listen_host: str = "0.0.0.0"
    advertise_host: str = ""
    idle_timeout_sec: int = DEFAULT_IDLE_TIMEOUT_SEC

    @classmethod
    def from_dict(cls, conf: dict[str, Any] | None) -> WebProxyChannelConfig:
        if not conf:
            return cls()

        def _int(key: str, default: int) -> int:
            try:
                value = int(conf.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            enabled=bool(conf.get("enabled", False)),
            auth_enabled=bool(conf.get("auth_enabled", True)),
            listen_host=str(conf.get("listen_host") or "0.0.0.0"),
            advertise_host=str(conf.get("advertise_host") or ""),
            idle_timeout_sec=_int("idle_timeout_sec", DEFAULT_IDLE_TIMEOUT_SEC),
        )


