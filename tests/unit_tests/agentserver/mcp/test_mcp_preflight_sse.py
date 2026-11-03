# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the SSE branch of ``preflight_mcp_server_reachable``.

Legacy SSE MCP servers handshake GET-only (``GET /sse`` → ``event: endpoint``):
they answer 405 to the POSTed initialize the streamable-http probe uses. The
sse transport therefore probes with a streamed GET and judges by status code
only (a healthy SSE endpoint answers 2xx plus an endless event stream, so the
body is never fully read). Real-world reference: Baidu Netdisk MCP
``https://mcp-pan.baidu.com/sse`` — 200 event-stream without token, 400 +
``auth fail:token fail`` with a bad one, 405 to POST.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from jiuwenswarm.common.mcp_config import preflight_mcp_server_reachable


class _ClientFactory:
    """Build real ``httpx.AsyncClient``s wrapping a shared MockTransport."""

    def __init__(self, handler: Any) -> None:
        self._transport = httpx.MockTransport(handler)
        # Capture the real class at construction time — the factory is
        # installed via patch("httpx.AsyncClient", ...), so looking the class
        # up inside __call__ would recurse into the patch itself.
        self._real = httpx.AsyncClient

    def __call__(self, **kwargs: Any) -> httpx.AsyncClient:
        return self._real(transport=self._transport, **kwargs)


def _cfg(
    transport: str = "sse",
    url: str = "https://mcp-pan.baidu.com/sse",
    headers: dict[str, str] | None = None,
) -> SimpleNamespace:
    params: dict[str, Any] = {"timeout_s": 5}
    if headers:
        params["headers"] = headers
    return SimpleNamespace(
        client_type=transport,
        server_path=url,
        params=params,
        auth_headers=None,
        auth_query_params=None,
    )


@pytest.mark.asyncio
async def test_sse_healthy_endpoint_probes_with_get() -> None:
    """A healthy SSE endpoint answers 200 + event stream to a GET probe —
    reachable, and the request must be GET (POST would 405 on real servers)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["accept"] = request.headers.get("Accept")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"event: endpoint\ndata: /message?sessionId=x\n\n",
        )

    with patch("httpx.AsyncClient", _ClientFactory(handler)):
        ok, reason = await preflight_mcp_server_reachable(
            _cfg(headers={"Authorization": "Bearer valid-token"})
        )
    assert ok is True
    assert reason == "ok (http 200)"
    assert seen["method"] == "GET"
    assert seen["accept"] == "text/event-stream"
    assert seen["auth"] == "Bearer valid-token"


@pytest.mark.asyncio
async def test_sse_bad_token_surfaces_auth_failure_snippet() -> None:
    """An invalid token makes the real server answer 400 + an ``auth fail``
    JSON body. The probe must fail (never report a bad token reachable) and
    surface the server's message so the connect rollback path can wipe the
    stale token and re-prompt for credentials."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errno": 2003, "show_msg": "auth fail:token fail"},
        )

    with patch("httpx.AsyncClient", _ClientFactory(handler)):
        ok, reason = await preflight_mcp_server_reachable(
            _cfg(headers={"Authorization": "Bearer bad-token"})
        )
    assert ok is False
    assert "http 400" in reason
    assert "auth fail" in reason


@pytest.mark.asyncio
async def test_sse_401_is_auth_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with patch("httpx.AsyncClient", _ClientFactory(handler)):
        ok, reason = await preflight_mcp_server_reachable(_cfg())
    assert ok is False
    assert "auth rejected (HTTP 401)" in reason


@pytest.mark.asyncio
async def test_streamable_http_still_probes_with_post() -> None:
    """Regression guard: the streamable-http probe must keep POSTing the
    initialize body (GitHub Copilot-style gateways answer 2xx to that)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})

    with patch("httpx.AsyncClient", _ClientFactory(handler)):
        ok, reason = await preflight_mcp_server_reachable(
            _cfg(transport="streamable-http", url="https://api.example.com/mcp/")
        )
    assert ok is True
    assert reason == "ok (http 200)"
    assert seen["method"] == "POST"
    assert '"method": "initialize"' in seen["body"]
