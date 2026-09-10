# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RuntimeRoutedAgentClient：route → HTTP base_url → touch。"""

from __future__ import annotations

import importlib.util
import asyncio
import json
import socket
import sys
import types
from pathlib import Path

import pytest

import httpx

from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod

_EXT_DIR = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "jiuwenclaw-ee"
    / "gateway"
    / "extensions"
    / "runtime_management_extension"
)
_PKG = "_ee_runtime_management_ext"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_EXT_DIR)]
    _pkg.__package__ = _PKG
    sys.modules[_PKG] = _pkg


def _load(name: str):
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _EXT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_route_mod = _load("session_route_client")
_routed_mod = _load("runtime_routed_client")
FatalRouteError = _route_mod.FatalRouteError
RetryableRouteError = _route_mod.RetryableRouteError
RouteResult = _route_mod.RouteResult
RuntimeRoutedAgentClient = _routed_mod.RuntimeRoutedAgentClient
http_base_from_pod_sse_url = _routed_mod.http_base_from_pod_sse_url
identity_from_envelope = _routed_mod.identity_from_envelope


def _chat_env():
    from jiuwenswarm.common.request_identity import apply_routing_metadata

    return e2a_from_agent_fields(
        request_id="req-1",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.CHAT_SEND,
        # group/bot 仍放 params 供 identity_from_envelope 做 route（与 invoke_ids 权威源解耦）
        params={"query": "hi", "group_id": "grp-1", "bot_id": "bot-1"},
        is_stream=True,
        user_id="user-1",
        metadata=apply_routing_metadata(
            {},
            {"user_id": "user-1", "group_id": "grp-1", "bot_id": "bot-1"},
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_reverse_rpc_over_real_http_sse(monkeypatch, stream):
    """First chat waits for registration; tool result returns over HTTP to its Pod."""
    import uvicorn
    from jiuwenswarm.agents.harness.common.tools.a2a_outbound_tools import (
        GatewayA2AOutboundToolBackend,
    )
    from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
        get_acp_output_manager,
    )
    from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a
    from jiuwenswarm.gateway.a2a_manager.tool_rpc import A2A_TOOL_FIND_AGENTS
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.server.agent_http_routes import build_fastapi_app
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
    from jiuwenswarm.server.transports import push_registry

    registry = push_registry.PushRegistry()
    monkeypatch.setattr(push_registry, "get_push_registry", lambda: registry)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.message_handler.is_enterprise",
        lambda: stream,
    )
    output = get_acp_output_manager()
    output.reset_state()

    async def push(message):
        return await registry.push_reverse_rpc(build_server_push_wire(message))

    monkeypatch.setattr(output, "_send_push_callback", push)
    monkeypatch.setattr("jiuwenswarm.gateway.routing.http_agent_client._PUSH_RETRY_SECONDS", 0.01)
    received = []

    class Agent:
        async def iter_stream(self, method, params, **ctx):
            from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire

            result, _ = await self.invoke_unary(method, params, **ctx)
            wire = encode_agent_chunk_for_wire(
                AgentResponseChunk(
                    request_id=ctx["request_id"],
                    channel_id="web",
                    payload=result["data"],
                    is_complete=True,
                ),
                response_id="response",
                sequence=0,
            )
            yield {"data": json.dumps(wire)}

        async def invoke_unary(self, method, params, **ctx):
            if method == "acp.tool_response":
                received.append(params["jsonrpc_id"])
                assert output.complete_jsonrpc_response(params["jsonrpc_id"], params["response"])
                return {"ok": True, "data": {}}, 200
            assert registry.reverse_rpc_ready(), "chat must not race subscriber registration"
            result = await GatewayA2AOutboundToolBackend().call(
                A2A_TOOL_FIND_AGENTS,
                {"query": "weather", "resource_id": "bot-1"},
                session_id=ctx["session_id"],
                channel_id="web",
            )
            return {"ok": True, "data": result}, 200

    class Manager:
        async def outbound_find_agents(self, **kwargs):
            assert kwargs["query"] == "weather"
            if stream:
                assert kwargs["source_resource_id"] == "bot-1"
            return {"items": [{"agent_id": "weather"}], "total": 1}

        async def outbound_dispatch_task(self, **kwargs):
            dispatch_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                dispatch_cancelled.set()

    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(build_fastapi_app(Agent()), log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    route = _FakeRoute()

    async def route_to_pod(**kwargs):
        route.routes.append(kwargs)
        return RouteResult(
            pod_sse_url=f"{base}/sse", pod_id="pod-1", request_id=kwargs["request_id"]
        )

    route.route = route_to_pod
    client = RuntimeRoutedAgentClient(route_client=route)
    handler = object.__new__(MessageHandler)
    handler._stream_sessions = {"req-1": "sess-1"}
    handler._stream_metadata = {"req-1": {"routing": {"bot_id": "bot-1"}}}
    handler._stream_channels = {"req-1": "web"}
    handler.set_a2a_outbound_tool_manager(Manager())

    async def publish(reply):
        assert (await client.send_request(message_to_e2a(reply))).ok

    handler.publish_user_messages = publish
    client.set_server_push_handler(handler._handle_agent_server_push)
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        await client.connect("unused")
        chat_number = 0

        async def chat():
            nonlocal chat_number
            chat_number += 1
            envelope = _chat_env()
            envelope.request_id = f"chat-{chat_number}"
            if stream:
                chunks = [chunk async for chunk in client.send_request_stream(envelope)]
                return chunks[-1]
            return await client.send_request(envelope)

        result = await asyncio.wait_for(chat(), 5)
        assert result.payload["total"] == 1
        assert received and len(route.routes) == 1, "reverse response must not re-route"
        assert not client._rpc_origins
        # Reusing a Pod does not add duplicate consumers.
        await chat()
        assert registry.subscriber_count() == 1
        # Force SSE EOF, then verify the next chat waits for re-registration.
        old_subscriber = next(iter(registry._subscribers))
        await registry._subscribers[old_subscriber].sink.finish()
        async with asyncio.timeout(5):
            while old_subscriber in registry._subscribers:
                await asyncio.sleep(0.01)
        assert (await asyncio.wait_for(chat(), 5)).payload["total"] == 1
        if not stream:
            from jiuwenswarm.gateway.a2a_manager.tool_rpc import A2A_TOOL_DISPATCH_TASK

            dispatch = asyncio.create_task(
                GatewayA2AOutboundToolBackend().call(
                    A2A_TOOL_DISPATCH_TASK,
                    {"agent_id": "weather", "task": "weather", "mode": "sync"},
                    session_id="sess-1",
                    channel_id="web",
                )
            )
            await asyncio.wait_for(dispatch_started.wait(), 5)
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(dispatch, 2)
            await asyncio.wait_for(dispatch_cancelled.wait(), 2)
            assert not client._rpc_origins
    finally:
        await client.disconnect()
        output.reset_state()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()
    assert registry.subscriber_count() == 0


@pytest.mark.asyncio
async def test_concurrent_pod_subscription_and_failed_registration_cleanup(monkeypatch):
    created = []

    class PodClient:
        def __init__(self):
            created.append(self)
            self.closed = False
            self.fail = False

        def set_server_push_handler(self, handler):
            self.handler = handler

        async def connect(self, uri):
            self.fail = "bad" in uri
            await asyncio.sleep(0)

        async def wait_push_ready(self):
            if self.fail:
                raise TimeoutError("no subscriber acknowledgement")

        async def disconnect(self):
            self.closed = True

    monkeypatch.setattr(_routed_mod, "HttpSseAgentServerClient", PodClient)
    client = RuntimeRoutedAgentClient(route_client=_FakeRoute(), http_client=_FakeHttp())
    client.set_server_push_handler(lambda wire: asyncio.sleep(0))
    await client.connect("unused")
    try:
        await asyncio.gather(*(client._ensure_pod_push("http://pod") for _ in range(3)))
        assert len(created) == 1
        with pytest.raises(TimeoutError):
            await client._ensure_pod_push("http://bad")
        assert created[-1].closed
        assert "http://bad" not in client._pod_clients
    finally:
        await client.disconnect()
    assert all(pod.closed for pod in created)


@pytest.mark.asyncio
async def test_reverse_rpc_reply_is_pinned_to_origin_pod():
    from jiuwenswarm.common.e2a.adapters import build_acp_tool_response_message
    from jiuwenswarm.common.e2a.gateway_normalize import message_to_e2a

    route, http = _FakeRoute(), _FakeHttp()
    client = RuntimeRoutedAgentClient(route_client=route, http_client=http)
    client.set_server_push_handler(lambda wire: asyncio.sleep(0))
    await client.connect("unused")
    try:
        for session, base in (("s1", "http://pod1:8080"), ("s2", "http://pod2:8080")):
            await client._handle_pod_push(
                base,
                {
                    "protocol_version": "1.0",
                    "response_id": "response",
                    "request_id": "r",
                    "session_id": session,
                    "channel": "web",
                    "response_kind": "acp.output_request",
                    "body": {
                        "jsonrpc": "2.0",
                        "id": "same-id",
                        "method": "a2a.outbound.tool.find_agents",
                    },
                },
            )
        for session in ("s2", "s1"):
            reply = build_acp_tool_response_message("same-id", {"result": {}}, session, "web")
            await client.send_request(message_to_e2a(reply))
        assert [call[1] for call in http.calls] == [
            "http://pod2:8080",
            "http://pod1:8080",
        ]
        assert not route.routes
        with pytest.raises(RuntimeError, match="origin Pod"):
            await client.send_request(message_to_e2a(reply))
    finally:
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["resource", "session", "channel", "ambiguous"])
async def test_reverse_rpc_identity_fallback_rejects_untrusted_scope(monkeypatch, mismatch):
    from types import SimpleNamespace
    from jiuwenswarm.gateway.a2a_manager.tool_rpc import A2A_TOOL_FIND_AGENTS
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

    monkeypatch.setattr(
        "jiuwenswarm.gateway.message_handler.message_handler.is_enterprise", lambda: True
    )
    handler = object.__new__(MessageHandler)
    handler._stream_sessions = {"chat": "session"}
    handler._stream_channels = {"chat": "web"}
    handler._stream_metadata = {"chat": {"routing": {"bot_id": "allowed"}}}
    if mismatch == "ambiguous":
        handler._stream_sessions["other"] = "session"
        handler._stream_channels["other"] = "web"
        handler._stream_metadata["other"] = {"routing": {"bot_id": "other"}}
    calls = []

    async def find(**kwargs):
        calls.append(kwargs)
        return {"total": 0}

    replies = []

    async def publish(reply):
        replies.append(reply)

    handler.set_a2a_outbound_tool_manager(SimpleNamespace(outbound_find_agents=find))
    handler.publish_user_messages = publish
    await handler._handle_a2a_outbound_tool_push(
        chunk=SimpleNamespace(
            channel_id="other" if mismatch == "channel" else "web",
            payload={
                "jsonrpc": "2.0", "id": "rpc", "method": A2A_TOOL_FIND_AGENTS,
                "params": {"resource_id": "other" if mismatch == "resource" else "allowed"},
            },
        ),
        session_id="other" if mismatch == "session" else "session",
    )
    assert not calls
    assert replies[0].params["response"]["error"]["data"]["code"] == "A2A_AGENT_NOT_AUTHORIZED"


class _FakeRoute:
    def __init__(self) -> None:
        self.routes: list[dict] = []
        self.touches: list[dict] = []
        self.fail_times = 0

    async def route(self, **kwargs):
        self.routes.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RetryableRouteError("full", code="SCOPE_QUEUE_FULL", retry_after=0)
        return RouteResult(
            pod_sse_url="http://10.1.2.3:8080/sse",
            pod_id="pod-1",
            request_id=kwargs["request_id"],
        )

    async def touch(self, **kwargs):
        self.touches.append(kwargs)
        return True

    async def aclose(self) -> None:
        return None


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_first = False
        self.fail_exc: BaseException = httpx.ConnectError("http down")

    async def send_request(self, envelope, *, base_url=None):
        self.calls.append(("unary", base_url, envelope.request_id))
        if self.fail_first:
            self.fail_first = False
            raise self.fail_exc
        return AgentResponse(
            request_id=str(envelope.request_id),
            channel_id="web",
            ok=True,
            payload={"ok": True},
        )

    async def send_request_stream(self, envelope, *, base_url=None):
        self.calls.append(("stream", base_url, envelope.request_id))
        if self.fail_first:
            self.fail_first = False
            raise self.fail_exc
        yield AgentResponseChunk(
            request_id=str(envelope.request_id),
            channel_id="web",
            payload={"content": "hi"},
            is_complete=True,
        )

    async def disconnect(self) -> None:
        return None


def test_http_base_from_pod_sse_url() -> None:
    assert http_base_from_pod_sse_url("http://10.0.0.1:8080/sse") == "http://10.0.0.1:8080"
    with pytest.raises(FatalRouteError):
        http_base_from_pod_sse_url("not-a-url")


def test_identity_from_envelope() -> None:
    env = _chat_env()
    session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(env)
    assert (session_id, group_id, bot_id, request_id, user_id) == (
        "sess-1",
        "grp-1",
        "bot-1",
        "req-1",
        "user-1",
    )


@pytest.mark.asyncio
async def test_unary_routes_then_http_then_touch() -> None:
    route = _FakeRoute()
    http = _FakeHttp()
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999
    )
    await client.connect("")
    env = _chat_env()
    env.is_stream = False
    result = await client.send_request(env)
    assert result.ok is True
    assert route.routes[0]["session_id"] == "sess-1"
    assert http.calls[0] == ("unary", "http://10.1.2.3:8080", "req-1")
    assert route.touches
    # 发往 Agent 前应已补齐 MD5 workspace_key（TenantAgentPool.workspace_key）
    import hashlib

    assert env.workspace_key == hashlib.md5(b"grp-1bot-1user-1").hexdigest()
    assert env.service_id == hashlib.md5(b"grp-1bot-1").hexdigest()
    assert env.agent_id == hashlib.md5(b"grp-1bot-1user-1").hexdigest()
    await client.disconnect()


@pytest.mark.asyncio
async def test_stream_and_route_retry() -> None:
    route = _FakeRoute()
    route.fail_times = 1
    http = _FakeHttp()
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=http,
        touch_interval_seconds=999,
        route_attempts=2,
    )
    await client.connect("")
    chunks = [c async for c in client.send_request_stream(_chat_env())]
    assert len(chunks) == 1
    assert len(route.routes) == 2
    assert http.calls[0][0] == "stream"
    await client.disconnect()


@pytest.mark.asyncio
async def test_http_fail_before_chunk_reroutes_with_new_id() -> None:
    route = _FakeRoute()
    http = _FakeHttp()
    http.fail_first = True
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999
    )
    await client.connect("")
    chunks = [c async for c in client.send_request_stream(_chat_env())]
    assert len(chunks) == 1
    assert len(route.routes) == 2
    assert route.routes[0]["request_id"] == "req-1"
    assert route.routes[1]["request_id"] != "req-1"
    await client.disconnect()


@pytest.mark.asyncio
async def test_unary_http_fail_reroutes_with_new_id() -> None:
    route = _FakeRoute()
    http = _FakeHttp()
    http.fail_first = True
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999
    )
    await client.connect("")
    assert client.server_ready is True
    env = _chat_env()
    env.is_stream = False
    result = await client.send_request(env)
    assert result.ok is True
    assert len(route.routes) == 2
    assert route.routes[0]["request_id"] == "req-1"
    assert route.routes[1]["request_id"] != "req-1"
    await client.disconnect()
    assert client.server_ready is False


def test_identity_from_chat_id_query_and_agent_ref() -> None:
    env = e2a_from_agent_fields(
        request_id="req-2",
        channel_id="web",
        session_id="sess-2",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
        user_id="user-2",
    )
    env.chat_id = "grp-chat"
    env.agent_ref = {"mode": "agent", "id": "bot-ref"}
    session_id, group_id, bot_id, request_id, user_id = identity_from_envelope(env)
    assert (session_id, group_id, bot_id, request_id, user_id) == (
        "sess-2",
        "grp-chat",
        "bot-ref",
        "req-2",
        "user-2",
    )

    env2 = e2a_from_agent_fields(
        request_id="req-3",
        channel_id="web",
        session_id="sess-3",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hi"},
    )
    env2.channel_context = {"query": {"group_id": ["g-q"], "bot_id": ["b-q"]}}
    _, group_id, bot_id, _, _ = identity_from_envelope(env2)
    assert (group_id, bot_id) == ("g-q", "b-q")


def test_identity_fallback_session_id() -> None:
    env = e2a_from_agent_fields(
        request_id="req-4",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={"group_id": "grp-1", "bot_id": "bot-1"},
        user_id="user-1",
    )
    session_id, group_id, bot_id, _, user_id = identity_from_envelope(env)
    assert session_id == "grp-1:bot-1:user-1"
    assert (group_id, bot_id, user_id) == ("grp-1", "bot-1", "user-1")


@pytest.mark.asyncio
async def test_heartbeat_skips_route() -> None:
    route = _FakeRoute()
    http = _FakeHttp()
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999
    )
    await client.connect("")
    env = e2a_from_agent_fields(
        request_id="heartbeat-abc",
        channel_id="web",
        session_id="heartbeat_1",
        params={"heartbeat": "tick", "run": {"kind": "heartbeat"}},
    )
    result = await client.send_request(env)
    assert result.ok is True
    assert route.routes == []
    assert http.calls == []
    await client.disconnect()


@pytest.mark.asyncio
async def test_unary_value_error_not_rerouted() -> None:
    route = _FakeRoute()
    http = _FakeHttp()
    http.fail_first = True
    http.fail_exc = ValueError("assemble failed")
    client = RuntimeRoutedAgentClient(
        route_client=route, http_client=http, touch_interval_seconds=999
    )
    await client.connect("")
    env = _chat_env()
    env.is_stream = False
    with pytest.raises(ValueError, match="assemble"):
        await client.send_request(env)
    assert len(route.routes) == 1
    await client.disconnect()
