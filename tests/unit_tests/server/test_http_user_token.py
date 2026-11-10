"""HTTP credentials remain local to the request, including failure paths."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jiuwenswarm.common.mcp_dynamic_token_patch import _current_user_authorization
from jiuwenswarm.common.request_ext import (
    INTERNAL_HEADER_NAME,
    encode_internal_header,
    get_ext,
    reset_ext,
    set_current,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.agent_http_routes import build_http_app
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.http_invoke import invoke_stream, invoke_unary


@pytest.mark.asyncio
@pytest.mark.parametrize("is_stream", [False, True], ids=["unary", "stream"])
@pytest.mark.parametrize(
    ("route", "forwarded_token", "expected_marker"),
    [
        ("/api/v1/chat", "", "kept"),
        ("/api/v1/chat/completions", "", None),
        ("/api/v1/chat/completions", "Bearer forwarded", None),
    ],
    ids=["legacy-body", "runtime-body", "runtime-header"],
)
async def test_http_credentials_use_only_the_internal_header(
    monkeypatch, is_stream, route, forwarded_token, expected_marker,
) -> None:
    seen = []

    async def handle_chat(ctx) -> None:
        metadata = ctx.request.metadata or {}
        seen.append((_current_user_authorization(), metadata.get("client_marker")))
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", handle_chat)
    headers = {"Accept": "text/event-stream"} if is_stream else {}
    if forwarded_token:
        headers[INTERNAL_HEADER_NAME] = encode_internal_header({"X-User-Token": forwarded_token})
    transport = httpx.ASGITransport(app=build_http_app(AppServices()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            route,
            headers=headers,
            json={
                "is_stream": is_stream,
                "metadata": {"ext": {"X-User-Token": "Bearer spoofed"}, "client_marker": "kept"},
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/event-stream" if is_stream else "application/json"
    )
    assert seen == [(forwarded_token, expected_marker)]


@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
async def test_concurrent_http_credentials_are_scoped(invoke):
    async def call(value: str | None) -> None:
        metadata = {"ext": {"X-User-Token": value}} if value else None
        request = AgentRequest(
            request_id=str(value), channel_id="web", session_id="test", metadata=metadata,
        )
        sink = AsyncMock()
        observed = []

        async def handler(ctx) -> None:
            observed.append(_current_user_authorization())
            await asyncio.sleep(0)
            observed.append(_current_user_authorization())

        await invoke(SimpleNamespace(request=request, sink=sink), handler)
        assert observed == [value or "", value or ""]
        sink.send_response.assert_not_awaited()

    outer = set_current({"X-User-Token": "Bearer outer"})
    try:
        await asyncio.gather(call("Bearer user-A"), call("Bearer user-B"), call(None))
        assert get_ext() == {"X-User-Token": "Bearer outer"}
    finally:
        reset_ext(outer)


@pytest.mark.asyncio
@pytest.mark.parametrize("invoke", [invoke_unary, invoke_stream])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_http_credentials_restore_after_failure(invoke, error):
    seen = []
    request = AgentRequest(
        request_id="failure", channel_id="web",
        metadata={"ext": {"X-User-Token": "Bearer current"}},
    )
    sink = AsyncMock()

    async def handler(ctx) -> None:
        seen.append(_current_user_authorization())
        raise error("test")

    outer = set_current({"X-User-Token": "Bearer outer"})
    try:
        if error is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await invoke(SimpleNamespace(request=request, sink=sink), handler)
        else:
            await invoke(SimpleNamespace(request=request, sink=sink), handler)
        assert seen == ["Bearer current"]
        assert get_ext() == {"X-User-Token": "Bearer outer"}
    finally:
        reset_ext(outer)
