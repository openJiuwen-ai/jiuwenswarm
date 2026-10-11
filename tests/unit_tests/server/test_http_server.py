# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 入口：默认关闭，路由直调处理函数，不经过 WebSocket 分发。"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_KV_CACHE_MOD = "openjiuwen.core.kv_cache"
if _KV_CACHE_MOD not in sys.modules:
    try:
        __import__(_KV_CACHE_MOD)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_KV_CACHE_MOD)
        _stub.KVCacheAffinityConfig = type("KVCacheAffinityConfig", (), {})
        sys.modules[_KV_CACHE_MOD] = _stub

import httpx

from jiuwenswarm.common.log_context import NO_SESSION_ID, current_log_session_id
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_http_routes import build_http_app
from jiuwenswarm.server.agent_http_server import resolve_http_server_settings
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.http_chat import handle_http_chat
from jiuwenswarm.server.http_invoke import invoke_stream
from jiuwenswarm.server.transports.http_sink import SSESink

_HTTP_DIR = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server"


def test_http_is_off_unless_asked() -> None:
    assert resolve_http_server_settings(env={}, config_section={}) == (
        False,
        "127.0.0.1",
        8766,
    )
    enabled, host, port = resolve_http_server_settings(
        env={"AGENT_HTTP_ENABLED": "true", "AGENT_HTTP_PORT": "9001"},
        config_section={"enabled": False, "host": "0.0.0.0"},
    )
    assert (enabled, host, port) == (True, "0.0.0.0", 9001)


def test_http_modules_do_not_use_the_websocket_dispatch() -> None:
    for name in (
        "agent_http_server.py",
        "agent_http_routes.py",
        "http_chat.py",
        "http_invoke.py",
    ):
        text = (_HTTP_DIR / name).read_text(encoding="utf-8")
        assert "dispatch_parsed_request" not in text
        assert "agent_ws_server" not in text
        assert "server.dispatch" not in text


@pytest.mark.asyncio
async def test_agents_list_skips_chat_steps(monkeypatch, caplog) -> None:
    called: list[str] = []

    async def _list(ctx: RequestContext) -> None:
        called.append(current_log_session_id())
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={"agents": []},
            )
        )

    def _forbid(*_args, **_kwargs):
        raise AssertionError("列 Agent 不应跑聊天前的步骤")

    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.agents.handle_agents_list",
        _list,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.http_chat.trigger_before_chat_request",
        _forbid,
    )
    app = build_http_app(AppServices())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level("INFO"):
            response = await client.post(
                "/api/v1/agents/list",
                headers={"x-session-id": "sess-9"},
                json={"request_id": "req-1", "channel_id": "http", "params": {}},
            )
    assert response.status_code == 200
    assert response.json()["payload"] == {"agents": []}
    assert called == ["sess-9"]
    assert any("session_id=sess-9" in record.message for record in caplog.records)
    assert current_log_session_id() == NO_SESSION_ID


@pytest.mark.asyncio
async def test_chat_route_uses_the_chat_adapter(monkeypatch) -> None:
    seen: list[str] = []

    async def _chat(ctx: RequestContext) -> None:
        seen.append(ctx.request.req_method.value if ctx.request.req_method else "")
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={"echo": True},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    app = build_http_app(AppServices())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat",
            json={
                "request_id": "req-2",
                "session_id": "sess-2",
                "req_method": "chat.send",
                "params": {"query": "你好"},
            },
        )
    assert response.status_code == 200
    assert seen == ["chat.send"]


@pytest.mark.asyncio
async def test_chat_adapter_runs_prelude_before_the_reply(monkeypatch) -> None:
    order: list[str] = []

    async def _hook(_request) -> None:
        order.append("hook")

    async def _bind(_request) -> None:
        order.append("bind")

    async def _feedback(_request) -> None:
        order.append("feedback")

    async def _unary(ctx: RequestContext) -> None:
        order.append("unary")
        await ctx.sink.send_response(
            AgentResponse(request_id="req-3", channel_id="http", ok=True, payload={})
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.trigger_before_chat_request", _hook)
    monkeypatch.setattr("jiuwenswarm.server.http_chat.ensure_auto_team_binding", _bind)
    monkeypatch.setattr("jiuwenswarm.server.http_chat.try_record_implicit_feedback", _feedback)
    monkeypatch.setattr("jiuwenswarm.server.http_chat.chat_default.handle_unary", _unary)

    class _Runtime:
        def begin_chat_request(self, session_id: str, request_id: str) -> None:
            order.append("begin")

        def end_chat_request(self, session_id: str, request_id: str) -> None:
            order.append("end")

    sink = SSESink()
    ctx = RequestContext(
        request=AgentRequest(
            request_id="req-3",
            channel_id="http",
            session_id="sess-3",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "你好"},
        ),
        sink=sink,
        connection_id="http",
        services=SimpleNamespace(execution_runtime=lambda: _Runtime()),
    )
    await handle_http_chat(ctx)
    assert order == ["begin", "hook", "bind", "feedback", "unary", "end"]


@pytest.mark.asyncio
async def test_escaped_error_becomes_http_500(monkeypatch) -> None:
    async def _boom(_ctx: RequestContext) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("jiuwenswarm.server.handlers.agents.handle_agents_list", _boom)
    app = build_http_app(AppServices())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/agents/list", json={"request_id": "req-4"})
    assert response.status_code == 500
    assert response.json()["payload"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_stream_cancel_is_reraised() -> None:
    async def _cancel(_ctx: RequestContext) -> None:
        raise asyncio.CancelledError()

    ctx = RequestContext(
        request=AgentRequest(request_id="req-5", channel_id="http", req_method=ReqMethod.CHAT_SEND),
        sink=SSESink(),
        connection_id="http",
        services=SimpleNamespace(),
    )
    with pytest.raises(asyncio.CancelledError):
        await invoke_stream(ctx, _cancel)
