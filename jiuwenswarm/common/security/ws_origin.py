# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Shared WebSocket Origin validation helpers."""

from __future__ import annotations

import os
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

_ENABLE_ORIGIN_CHECK_ENV = "JIUWENSWARM_ENABLE_ORIGIN_CHECK"
_ALLOWED_ORIGIN_HOSTS_ENV = "JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS"
_FORBIDDEN_BODY = b"Forbidden: Origin not allowed\n"

# Default-secure allowlist for local-mode deployments: the loopback hostnames
# the packaged desktop/web UI actually runs on, plus "none" for native/
# embedded clients that send no Origin header at all. An operator who serves
# the UI from a different hostname (a LAN IP, a reverse-proxy domain) must
# opt in explicitly via JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS, same as before.
_DEFAULT_ALLOWED_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "none"})


def is_origin_check_enabled() -> bool:
    """Return whether WebSocket Origin validation is enabled.

    Defaults to enabled: an unset handshake Origin check combined with no
    default handshake authentication (see ``ChannelBase.authenticate_handshake``)
    lets any cross-site page open this WebSocket and invoke privileged local
    RPCs. Set JIUWENSWARM_ENABLE_ORIGIN_CHECK=0 to opt back out.
    """
    return os.getenv(_ENABLE_ORIGIN_CHECK_ENV, "1").strip() == "1"


def get_allowed_origin_hosts() -> set[str]:
    """Return the global WebSocket Origin hostname allowlist from environment.

    Defaults to the loopback hostnames the local-mode UI runs on when unset,
    rather than an empty set that would allow nothing once the check above
    defaults to enabled.
    """
    raw = os.getenv(_ALLOWED_ORIGIN_HOSTS_ENV)
    if raw is None:
        return set(_DEFAULT_ALLOWED_ORIGIN_HOSTS)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_allowed_browser_origin(origin: str | None) -> bool:
    """校验浏览器 Origin 是否允许访问 WebSocket 服务。"""
    allowed_hosts = get_allowed_origin_hosts()
    if origin is None:
        return "none" in allowed_hosts

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False

    return (parsed.hostname or "").lower() in allowed_hosts


def extract_handshake_request(args: tuple[Any, ...]) -> tuple[str, Any]:
    """Extract path and headers from legacy/new websockets process_request args."""
    path = ""
    headers = None

    if len(args) >= 2:
        first, second = args[0], args[1]
        if isinstance(first, str):
            path = first
            headers = second
        else:
            path = getattr(second, "path", "") or ""
            headers = getattr(second, "headers", second)

    return path, headers


def get_header_value(headers: Any, key: str) -> str | None:
    """Read a header from either legacy or modern websockets header containers."""
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    if callable(get):
        value = get(key)
        if value is None:
            value = get(key.lower())
        return str(value) if value is not None else None
    return None


def forbidden_origin_response(process_request_args: tuple[Any, ...]) -> Any:
    """Build a 403 response for legacy/new websockets process_request APIs."""
    status = HTTPStatus.FORBIDDEN
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(_FORBIDDEN_BODY))),
    ]

    if process_request_args and not isinstance(process_request_args[0], str):
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        return Response(status.value, status.phrase, Headers(headers), _FORBIDDEN_BODY)

    return status, headers, _FORBIDDEN_BODY
