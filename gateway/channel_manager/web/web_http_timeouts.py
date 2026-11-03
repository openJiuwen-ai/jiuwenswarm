# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Timeouts for Gateway Web HTTP/SSE on the shared WebChannel port."""

from __future__ import annotations

import os


def _env_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if allow_zero:
        return value if value >= 0 else default
    return value if value > 0 else default


def resolve_web_http_sse_timeout() -> float:
    """Total SSE stream lifetime seconds (default 0 = no cap).

    Long agent turns can run for hours; a hard cap would drop a live
    ``chat.send`` stream. Set ``GATEWAY_WEB_HTTP_SSE_TIMEOUT`` to a
    positive value only when ops wants an explicit lifetime limit.
    """
    return _env_float("GATEWAY_WEB_HTTP_SSE_TIMEOUT", 0.0, allow_zero=True)


def resolve_web_http_sse_idle_timeout() -> float:
    """Idle seconds without frames before ending SSE (default 0 = disabled)."""
    return _env_float("GATEWAY_WEB_HTTP_SSE_IDLE_TIMEOUT", 0.0, allow_zero=True)


def resolve_web_http_sse_keepalive() -> float:
    """Seconds between SSE keepalive comments while waiting (default 30)."""
    return _env_float("GATEWAY_WEB_HTTP_SSE_KEEPALIVE", 30.0)


def resolve_web_http_unary_timeout() -> float:
    """Unary ``wait_response`` timeout seconds (default 120)."""
    return _env_float("GATEWAY_WEB_HTTP_UNARY_TIMEOUT", 120.0)


def resolve_web_http_history_timeout() -> float:
    """History JSON collector timeout seconds (default 60)."""
    return _env_float("GATEWAY_WEB_HTTP_HISTORY_TIMEOUT", 60.0)
