# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 入口：默认关闭，路由直调处理函数，不经过 WebSocket 分发。"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_http_routes import _event_frames, build_http_app
from jiuwenswarm.server.agent_http_server import resolve_http_server_settings
from jiuwenswarm.server.app_services import AppServices
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.http_chat import handle_http_chat
from jiuwenswarm.server.http_invoke import invoke_stream
from jiuwenswarm.server.transports.http_sink import SSESink

_HTTP_DIR = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server"


def test_websocket_door_stays_open_unless_asked() -> None:
    from jiuwenswarm.server.startup_switches import resolve_websocket_enabled

    assert resolve_websocket_enabled(env={}, config_section={}) is True
    assert resolve_websocket_enabled(env={}, config_section={"enabled": False}) is False
    assert (
        resolve_websocket_enabled(
            env={"AGENT_WS_ENABLED": "false"},
            config_section={"enabled": True},
        )
        is False
    )
    assert resolve_websocket_enabled(env={"AGENT_WS_ENABLED": "true"}, config_section={}) is True


def test_websocket_door_off_skips_listen_but_starts_heartbeat() -> None:
    """关 WebSocket 只是不听端口。Runtime/心跳由 Worker 以 bind_transport=False 启动。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    app = (root / "jiuwenswarm/server/app_agentserver.py").read_text(encoding="utf-8")
    worker = (root / "jiuwenswarm/server/worker/lifecycle.py").read_text(encoding="utf-8")
    listen = app.split("if websocket_enabled:", 1)[1].split("else:", 1)[0]
    assert "await front.start()" in listen
    assert "await server.start(bind_transport=False)" in worker
    assert "await start_http_if_enabled(app_services)" in worker
    assert "await ProcessServices(server).start()" not in worker


@pytest.mark.asyncio
async def test_second_door_does_not_start_the_process_again() -> None:
    """进程只启动一次。后调用的那扇门看到已经启动，就不会再拉起运行环境。"""
    from jiuwenswarm.server.process_services import ProcessServices

    ProcessServices.note_stopped()
    ProcessServices._started = True
    try:
        await ProcessServices(object()).start()
        assert ProcessServices.started() is True
    finally:
        ProcessServices.note_stopped()
    assert ProcessServices.started() is False


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
        if name != "agent_http_routes.py":
            assert "server.dispatch" not in text
    routes_text = (_HTTP_DIR / "agent_http_routes.py").read_text(encoding="utf-8")
    assert "http_rpc" not in routes_text


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
    # Session binding is asserted via called[]; request-arrived logs may bypass
    # pytest caplog (async handlers / only httpx appears), so do not require them.
    assert called == ["sess-9"]
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
async def test_chat_keeps_the_user_id_from_the_body(monkeypatch) -> None:
    """正文里的 user_id 要进请求，和 WebSocket 解析同一条请求时一样。"""
    seen: list[str] = []

    async def _chat(ctx: RequestContext) -> None:
        seen.append(ctx.request.user_id)
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
                "request_id": "req-user",
                "session_id": "sess-user",
                "user_id": "  alice  ",
                "params": {"query": "你好"},
            },
        )
    assert response.status_code == 200
    assert seen == ["alice"]


@pytest.mark.asyncio
async def test_archived_session_chat_is_rejected(monkeypatch) -> None:
    """归档中的会话走 HTTP 聊天时，要带回和 WebSocket 相同的拒绝码，并且聊天还没开始。"""
    from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError

    started: list[str] = []

    def _guard(*_args: object, **_kwargs: object) -> None:
        raise LifecycleError("SESSION_ARCHIVED", "session is archived")

    async def _hook(_request: object) -> None:
        started.append("hook")

    monkeypatch.setattr("jiuwenswarm.server.http_chat.guard", _guard)
    monkeypatch.setattr("jiuwenswarm.server.http_chat.trigger_before_chat_request", _hook)
    app = build_http_app(AppServices())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat",
            json={
                "request_id": "req-archived",
                "session_id": "sess-archived",
                "params": {"query": "你好"},
            },
        )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["payload"]["code"] == "SESSION_ARCHIVED"
    assert started == []


@pytest.mark.asyncio
async def test_oversized_chat_body_is_rejected(monkeypatch) -> None:
    """请求体超过和 WebSocket 相同的上限时，返回 413，聊天不会开始。"""
    monkeypatch.setattr("jiuwenswarm.server.agent_http_routes.AGENT_WS_MAX_MESSAGE_BYTES", 32)
    started: list[str] = []

    async def _chat(_ctx: RequestContext) -> None:
        started.append("chat")

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    app = build_http_app(AppServices())
    transport = httpx.ASGITransport(app=app)
    body = b'{"request_id":"req-big","params":{"query":"' + (b"x" * 64) + b'"}}'
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/chat",
            content=body,
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["payload"]["code"] == "PAYLOAD_TOO_LARGE"
    assert started == []


@pytest.mark.asyncio
async def test_chunked_body_stops_at_the_limit(monkeypatch) -> None:
    """没有长度头时也按块计数，超过上限就停，不再把剩下的收进来。"""
    from jiuwenswarm.server.agent_http_routes import RequestBodyLimitMiddleware

    monkeypatch.setattr("jiuwenswarm.server.agent_http_routes.AGENT_WS_MAX_MESSAGE_BYTES", 8)
    seen: list[bytes] = []

    async def _app(scope, receive, send) -> None:  # noqa: ANN001
        message = await receive()
        seen.append(message.get("body", b""))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    pending = [
        {"type": "http.request", "body": b"0123", "more_body": True},
        {"type": "http.request", "body": b"456789", "more_body": False},
    ]

    async def _receive():
        return pending.pop(0)

    sent: list[dict] = []

    async def _send(message) -> None:  # noqa: ANN001
        sent.append(message)

    await RequestBodyLimitMiddleware(_app)(
        {"type": "http", "method": "POST", "headers": [], "path": "/api/v1/chat"},
        _receive,
        _send,
    )
    assert seen == []
    assert sent[0]["status"] == 413


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


@pytest.mark.asyncio
async def test_disconnect_cancels_the_stream_task() -> None:
    """网页请求被取消时，后台聊天也要停，不能继续跑完这一轮。"""
    from jiuwenswarm.server.agent_http_routes import _stop_background_chat

    child_started = asyncio.Event()
    parent_waiting = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def _child() -> None:
        child_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    async def _parent() -> None:
        task = asyncio.create_task(_child())
        await child_started.wait()
        try:
            parent_waiting.set()
            await asyncio.sleep(30)
        finally:
            await _stop_background_chat(task)

    parent = asyncio.create_task(_parent())
    await parent_waiting.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert child_cancelled.is_set()


@pytest.mark.asyncio
async def test_finished_stream_is_not_cancelled() -> None:
    """正常把流读完时，只等后台聊天结束，不把它取消。"""
    from jiuwenswarm.server.agent_http_routes import _stop_background_chat

    async def _child() -> None:
        await asyncio.sleep(0)

    async def _parent() -> None:
        task = asyncio.create_task(_child())
        await _stop_background_chat(task)
        assert task.cancelled() is False

    await _parent()


@pytest.mark.asyncio
async def test_sse_queue_stops_at_the_limit() -> None:
    sink = SSESink()
    pending = [
        asyncio.create_task(
            sink.send_chunk(
                AgentResponseChunk(
                    request_id="req-bound",
                    channel_id="http",
                    payload={"n": sequence},
                    is_complete=False,
                ),
                sequence=sequence,
            )
        )
        for sequence in range(40)
    ]
    await asyncio.sleep(0)
    assert sink.pending_frame_count() == 32
    assert any(not task.done() for task in pending)
    await sink.close()
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    assert sink.pending_frame_count() <= 32


def _app_client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_http_app(AppServices())),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_health_reports_ok() -> None:
    async with _app_client() as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_chat_completions_reads_headers_and_wraps_data(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def _chat(ctx: RequestContext) -> None:
        seen["method"] = ctx.request.req_method.value if ctx.request.req_method else ""
        seen["query"] = (ctx.request.params or {}).get("query")
        seen["session"] = ctx.request.session_id
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={"echo": True},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    async with _app_client() as client:
        response = await client.post(
            "/api/v1/chat/completions",
            headers={
                "x-request-id": "req-c",
                "x-channel-id": "web",
                "x-session-id": "sess-c",
            },
            json={"query": "你好"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == {"echo": True}
    assert body["request_id"] == "req-c"
    assert "payload" not in body
    assert seen == {"method": "chat.send", "query": "你好", "session": "sess-c"}


@pytest.mark.asyncio
async def test_runtime_headers_rebuild_identity(monkeypatch) -> None:
    """Gateway 经 X-* 头传来的身份，要写回请求里的 user_id 和 metadata。"""
    from jiuwenswarm.common.request_ext import encode_internal_header

    seen: dict[str, object] = {}

    async def _chat(ctx: RequestContext) -> None:
        seen["user_id"] = ctx.request.user_id
        seen["metadata"] = dict(ctx.request.metadata or {})
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    ext_header = encode_internal_header({"trace": "t-1"})
    assert ext_header
    async with _app_client() as client:
        response = await client.post(
            "/api/v1/chat/completions",
            headers={
                "x-request-id": "req-id",
                "x-channel-id": "web",
                "x-session-id": "sess-id",
                "x-user-id": "  alice  ",
                "x-group-id": "g1",
                "x-bot-id": "b1",
                "x-gateway-id": "gw1",
                "x-service-id": "svc-1",
                "x-agent-id": "ag-1",
                "x-workspace-key": "ws-1",
                "x-jiuwenswarm-request-ext": ext_header,
            },
            json={"query": "你好"},
        )
    assert response.status_code == 200
    assert seen["user_id"] == "alice"
    assert seen["metadata"] == {
        "user_id": "alice",
        "routing": {"group_id": "g1", "bot_id": "b1", "gateway_id": "gw1"},
        "service_id": "svc-1",
        "agent_id": "ag-1",
        "workspace_key": "ws-1",
        "ext": {"trace": "t-1"},
    }


@pytest.mark.asyncio
async def test_runtime_headers_without_user_stay_anonymous(monkeypatch) -> None:
    """没有 X-User-Id 时按匿名处理，不能假装成别人。"""
    seen: dict[str, object] = {}

    async def _chat(ctx: RequestContext) -> None:
        seen["user_id"] = ctx.request.user_id
        seen["metadata"] = ctx.request.metadata
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    async with _app_client() as client:
        response = await client.post(
            "/api/v1/chat/completions",
            headers={"x-request-id": "req-anon", "x-session-id": "sess-anon"},
            json={"query": "你好"},
        )
    assert response.status_code == 200
    assert seen["user_id"] == ""
    assert seen["metadata"] is None


@pytest.mark.asyncio
async def test_chat_resume_uses_the_same_adapter(monkeypatch) -> None:
    seen: list[str] = []

    async def _chat(ctx: RequestContext) -> None:
        seen.append(ctx.request.req_method.value if ctx.request.req_method else "")
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={},
            )
        )

    monkeypatch.setattr("jiuwenswarm.server.http_chat.handle_http_chat", _chat)
    async with _app_client() as client:
        response = await client.post(
            "/api/v1/chat/resume",
            headers={"x-request-id": "req-resume"},
            json={"query": "继续"},
        )
    assert response.status_code == 200
    assert response.json()["data"] == {}
    assert seen == ["chat.resume"]


@pytest.mark.asyncio
async def test_events_stream_starts_with_push_ready() -> None:
    from jiuwenswarm.server.agent_http_routes import _event_frames

    frames: list[str] = []
    async for frame in _event_frames(SimpleNamespace()):
        frames.append(frame)
        break
    assert frames == ['data: {"event_type": "gateway.push_ready"}\n\n']


@pytest.mark.asyncio
async def test_events_stream_stays_open_until_the_client_leaves(monkeypatch) -> None:
    from jiuwenswarm.server.agent_http_routes import _event_frames

    state = {"ticks": 0}

    async def _wait_for(awaitable, timeout=None):
        _ = timeout
        state["ticks"] += 1
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise asyncio.TimeoutError

    async def _gone() -> bool:
        return state["ticks"] >= 2

    monkeypatch.setattr(
        "jiuwenswarm.server.agent_http_routes.asyncio.wait_for",
        _wait_for,
    )
    request = SimpleNamespace(is_disconnected=_gone)
    frames = [frame async for frame in _event_frames(request)]
    assert "gateway.push_ready" in frames[0]
    # 先等超时发 keepalive，再检查断开；因此至少两轮超时才会离开。
    assert frames[1:] == [": keepalive\n\n", ": keepalive\n\n"]


@pytest.mark.asyncio
async def test_events_stream_forwards_http_hub_server_push() -> None:
    from jiuwenswarm.server.agent_http_routes import _event_frames
    from jiuwenswarm.server.gateway_push.http_hub import get_http_push_hub

    hub = get_http_push_hub()
    # 清掉其它测试可能留下的订阅，避免串扰。
    for leftover in list(getattr(hub, "_subscribers")):
        hub.unsubscribe(leftover)

    seen: list[str] = []

    async def _gone() -> bool:
        return len(seen) >= 2

    async def _consume() -> None:
        request = SimpleNamespace(is_disconnected=_gone)
        async for frame in _event_frames(request):
            seen.append(frame)
            if len(seen) >= 2:
                break

    task = asyncio.create_task(_consume())
    for _ in range(50):
        if hub.subscriber_count() >= 1:
            break
        await asyncio.sleep(0.01)
    assert hub.subscriber_count() >= 1
    assert hub.publish({"metadata": {"x-jiuwen-server-push": True}, "ok": True}) == 1
    await asyncio.wait_for(task, timeout=2)
    assert "gateway.push_ready" in seen[0]
    assert "x-jiuwen-server-push" in seen[1]


@pytest.mark.asyncio
async def test_events_route_is_an_sse_stream(monkeypatch) -> None:
    async def _frames(_request) -> Any:
        yield 'data: {"event_type": "gateway.push_ready"}\n\n'

    monkeypatch.setattr("jiuwenswarm.server.agent_http_routes._event_frames", _frames)
    async with _app_client() as client:
        response = await client.get("/api/v1/events/stream")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "gateway.push_ready" in response.text


@pytest.mark.asyncio
async def test_rpc_url_is_closed() -> None:
    async with _app_client() as client:
        unknown = await client.post("/api/v1/rpc/not.a.method", json={})
        skills = await client.post("/api/v1/rpc/skills.list", json={})
    assert unknown.status_code == 404
    assert skills.status_code == 404


@pytest.mark.asyncio
async def test_dedicated_session_url_calls_the_same_handler(monkeypatch) -> None:
    seen: list[dict[str, object]] = []

    async def _dispatch(ctx: RequestContext) -> None:
        params = ctx.request.params or {}
        seen.append(
            {
                "method": ctx.request.req_method,
                "session_id": params.get("session_id"),
                "title": params.get("title"),
            }
        )
        await ctx.sink.send_response(
            AgentResponse(
                request_id=ctx.request.request_id,
                channel_id=ctx.request.channel_id,
                ok=True,
                payload={"session_id": "sess-new"},
            )
        )

    from jiuwenswarm.server.dispatch import HANDLERS

    monkeypatch.setitem(HANDLERS, ReqMethod.SESSION_CREATE, _dispatch)
    monkeypatch.setitem(HANDLERS, ReqMethod.SESSION_DELETE, _dispatch)
    async with _app_client() as client:
        created = await client.post(
            "/api/v1/sessions",
            headers={"x-request-id": "req-c"},
            json={"title": "你好"},
        )
        removed = await client.delete(
            "/api/v1/sessions/sess-1",
            headers={"x-request-id": "req-d"},
        )
    assert created.status_code == 200
    assert created.json()["data"]["session_id"] == "sess-new"
    assert removed.status_code == 200
    assert seen[0]["method"] is ReqMethod.SESSION_CREATE
    assert seen[0]["title"] == "你好"
    assert seen[1]["method"] is ReqMethod.SESSION_DELETE
    assert seen[1]["session_id"] == "sess-1"


def test_unhandled_urls_are_not_registered() -> None:
    app = build_http_app(AppServices())
    routes = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
    }
    # Skills now have handlers via pod_rest_map; plugins/channels/updater still do not.
    assert ("GET", "/api/v1/skills") in routes
    assert ("POST", "/api/v1/skills/install") in routes
    assert ("GET", "/api/v1/plugins") not in routes
    assert ("POST", "/api/v1/plugins/install") not in routes
    assert ("GET", "/api/v1/channels/feishu/config") not in routes
    assert ("POST", "/api/v1/updater/actions/check") not in routes
    assert ("GET", "/api/v1/heartbeat/config") not in routes
    assert ("POST", "/api/v1/sessions") in routes
    assert ("POST", "/api/v1/mcp/actions/list") in routes
    assert ("POST", "/api/v1/sessions/{session_id}/swarmflow/actions/pause") in routes
    assert ("POST", "/api/v1/proactive/actions/feedback") in routes
    assert ("POST", "/api/v1/rpc/{method}") not in routes
