import json
import time
import asyncio
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.gateway.app_gateway import (
    GatewayServer,
    GatewayServerConfig,
    RouteConfig,
    _normalize_gateway_message,
)
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod


class DummyBus:
    @staticmethod
    async def publish_user_messages(msg):
        return None


class _FakeRequestHeaders:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._mapping.get(key.lower(), default)


class FakeWebSocket:
    def __init__(self, *, user_id: str | None = None) -> None:
        self.sent_frames = []
        self.closed = False
        if user_id is not None:
            self._gateway_user_id = user_id
            self.request = type(
                "Request",
                (),
                {"headers": _FakeRequestHeaders({"X-User-Id": user_id})},
            )()

    async def send(self, data):
        self.sent_frames.append(json.loads(data))

    async def close(self, code=None, reason=None):
        self.closed = True
        return code, reason


class ClosedSendWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.closed = True

    async def send(self, data):
        raise ConnectionClosedError(None, None)


class IterableFakeWebSocket(FakeWebSocket):
    def __init__(self, frames=None):
        super().__init__()
        self.frames = list(frames or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class GatewayServerProbe(GatewayServer):
    def __init__(self, config: GatewayServerConfig, router) -> None:
        super().__init__(config, router)
        self._probe_on_message = None

    def on_message(self, callback) -> None:
        self._probe_on_message = callback
        super().on_message(callback)

    def bind_request_client(self, request_id: str, ws, *, channel_id: str = "acp") -> None:
        self._request_to_client[(channel_id, request_id)] = ws

    def bind_session_client(self, session_id: str, ws, *, channel_id: str = "acp") -> None:
        self._session_to_client[(channel_id, session_id)] = ws

    def bind_request_client_ws(self, request_id: str, ws, *, channel_id: str = "acp") -> None:
        self._request_to_client[(channel_id, request_id)] = ws

    async def handle_raw_message_public(self, ws, raw: str, *, path: str = "/acp") -> None:
        await self._handle_raw_message(ws, raw, path, self.config.routes[path])

    async def dispatch_public_message(self, msg: Any) -> bool:
        if self._probe_on_message is None:
            return False
        result = self._probe_on_message(msg)
        if hasattr(result, "__await__"):
            result = await result
        return bool(result)

    def get_acp_pending_request_contexts_for_test(self) -> list[Any]:
        return list(self._acp_bridge.request_contexts)

    @classmethod
    def extract_ws_user_id_for_test(cls, ws: Any) -> str | None:
        """Expose _extract_ws_user_id for unit tests (G.CLS.11: subclass wrapper)."""
        return cls._extract_ws_user_id(ws)


def build_server() -> GatewayServerProbe:
    config = GatewayServerConfig(
        enabled=True,
        host="127.0.0.1",
        port=19001,
        routes={
            "/acp": RouteConfig(
                path="/acp",
                channel_id="acp",
                forward_methods=frozenset({ReqMethod.CHAT_SEND.value, ReqMethod.HISTORY_GET.value}),
            ),
            "/tui": RouteConfig(
                path="/tui",
                channel_id="tui",
                forward_methods=frozenset({ReqMethod.CHAT_SEND.value, ReqMethod.HISTORY_GET.value}),
            ),
        },
    )
    server = GatewayServerProbe(config, DummyBus())
    return server


def test_normalize_gateway_message_maps_chat_resume_to_interrupt_resume():
    msg = Message(
        id="req-resume",
        type="req",
        channel_id="tui",
        session_id="sess-1",
        params={"session_id": "sess-1"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_RESUME,
    )

    normalized = _normalize_gateway_message(msg)

    assert normalized.req_method == ReqMethod.CHAT_CANCEL
    assert normalized.params["intent"] == "resume"
    assert normalized.session_id == "sess-1"


def test_normalize_gateway_message_preserves_user_id():
    msg = Message(
        id="req-chat",
        type="req",
        channel_id="tui",
        session_id="sess-1",
        params={"session_id": "sess-1", "content": "hi"},
        timestamp=time.time(),
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        user_id="testuser",
    )

    normalized = _normalize_gateway_message(msg)

    assert normalized.user_id == "testuser"
    assert normalized.is_stream is True


@pytest.mark.asyncio
async def test_schedule_gateway_restart_sets_event_without_execv(monkeypatch):
    import jiuwenswarm.gateway.app_gateway as gateway_module

    execv_calls = []
    monkeypatch.setattr(
        gateway_module.os,
        "execv",
        lambda executable, argv: execv_calls.append((executable, argv)),
    )
    restart_request = gateway_module.GatewayRestartRequest()

    gateway_module._schedule_gateway_restart(restart_request, delay=0.0)

    await asyncio.wait_for(restart_request.ready_event.wait(), timeout=1.0)
    assert restart_request.requested is True
    assert execv_calls == []


@pytest.mark.asyncio
async def test_wait_for_gateway_tasks_returns_false_when_services_finish():
    import jiuwenswarm.gateway.app_gateway as gateway_module

    service_task = asyncio.create_task(asyncio.sleep(0))
    restart_request = gateway_module.GatewayRestartRequest()

    result = await asyncio.wait_for(
        gateway_module._wait_for_gateway_tasks_or_restart([service_task], restart_request),
        timeout=1.0,
    )

    assert result is False


@pytest.mark.asyncio
async def test_wait_for_gateway_tasks_keeps_delayed_restart_when_services_finish():
    import jiuwenswarm.gateway.app_gateway as gateway_module

    service_task = asyncio.create_task(asyncio.sleep(0))
    restart_request = gateway_module.GatewayRestartRequest()

    gateway_module._schedule_gateway_restart(restart_request, delay=1.0)
    result = await asyncio.wait_for(
        gateway_module._wait_for_gateway_tasks_or_restart([service_task], restart_request),
        timeout=1.0,
    )

    assert result is True


@pytest.mark.asyncio
async def test_wait_for_gateway_tasks_keeps_delayed_restart_when_service_fails():
    import jiuwenswarm.gateway.app_gateway as gateway_module

    async def fail_service():
        raise RuntimeError("service failed before restart delay")

    service_task = asyncio.create_task(fail_service())
    restart_request = gateway_module.GatewayRestartRequest()

    gateway_module._schedule_gateway_restart(restart_request, delay=1.0)
    result = await asyncio.wait_for(
        gateway_module._wait_for_gateway_tasks_or_restart([service_task], restart_request),
        timeout=1.0,
    )

    assert result is True


@pytest.mark.asyncio
async def test_gateway_server_initialize_returns_current_acp_capabilities():
    server = build_server()
    ws = FakeWebSocket()

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            },
            ensure_ascii=False,
        ),
    )

    assert len(ws.sent_frames) == 1
    frame = ws.sent_frames[0]
    assert frame["jsonrpc"] == "2.0"
    assert frame["id"] == 1
    result = frame["result"]
    assert result["protocolVersion"] == 1
    assert result["agentInfo"]["name"] == "jiuwenswarm"
    assert result["agentCapabilities"] == {
        "loadSession": False,
        "promptCapabilities": {
            "image": False,
            "audio": False,
            "embeddedContext": False,
        },
        "sessionCapabilities": {"list": {}},
        "mcpCapabilities": {"http": False, "sse": False},
    }
    assert result["authMethods"] == []


@pytest.mark.asyncio
async def test_gateway_server_rejects_session_load_when_capability_is_disabled():
    server = build_server()
    ws = FakeWebSocket()

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/load",
                "params": {"sessionId": "sess-load"},
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames == [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32601,
                "message": "Method not supported by agent capabilities: session/load",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_session_new_and_list_return_known_sessions():
    server = build_server()
    ws = FakeWebSocket()

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/new",
                "params": {"sessionId": "sess-known"},
            },
            ensure_ascii=False,
        ),
    )
    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "session/list",
                "params": {},
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames == [
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"sessionId": "sess-known", "configOptions": []},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {"sessions": [{"sessionId": "sess-known"}]},
        },
    ]


@pytest.mark.asyncio
async def test_gateway_server_send_response_targets_request_client():
    server = build_server()
    ws = FakeWebSocket()
    server.bind_request_client("req-1", ws)

    await server.send(
        Message(
            id="req-1",
            type="res",
            channel_id="acp",
            session_id="sess-1",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"accepted": True},
        )
    )

    assert len(ws.sent_frames) == 1
    frame = ws.sent_frames[0]
    assert frame == {
        "type": "res",
        "id": "req-1",
        "ok": True,
        "payload": {"accepted": True},
    }


@pytest.mark.asyncio
async def test_gateway_server_send_event_targets_session_client():
    server = build_server()
    ws = FakeWebSocket()
    server.bind_session_client("sess-2", ws)

    await server.send(
        Message(
            id="req-2",
            type="event",
            channel_id="acp",
            session_id="sess-2",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "hello"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert len(ws.sent_frames) == 1
    frame = ws.sent_frames[0]
    assert frame == {
        "type": "event",
        "event": "chat.delta",
        "payload": {
            "content": "hello",
            "session_id": "sess-2",
        },
    }


@pytest.mark.asyncio
async def test_gateway_server_falls_back_to_session_client_when_request_ws_closed():
    server = build_server()
    old_ws = ClosedSendWebSocket()
    new_ws = FakeWebSocket()
    server.bind_request_client_ws("req-closed", old_ws, channel_id="tui")
    server.bind_session_client("sess-reconnected", new_ws, channel_id="tui")

    await server.send(
        Message(
            id="req-closed",
            type="event",
            channel_id="tui",
            session_id="sess-reconnected",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "still running"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert new_ws.sent_frames == [
        {
            "type": "event",
            "event": "chat.delta",
            "payload": {
                "content": "still running",
                "session_id": "sess-reconnected",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_binds_session_for_local_request_and_calls_hook():
    bound_sessions = []

    async def local_handler(ws, req_id, params, session_id):
        await server.send_response(ws, req_id, ok=True, payload={"session_id": session_id})

    async def session_bind_handler(channel_id, session_id):
        bound_sessions.append((channel_id, session_id))

    config = GatewayServerConfig(
        enabled=True,
        host="127.0.0.1",
        port=19001,
        routes={
            "/tui": RouteConfig(
                path="/tui",
                channel_id="tui",
                local_handlers={"config.get": local_handler},
                session_bind_handler=session_bind_handler,
            ),
        },
    )
    server = GatewayServerProbe(config, DummyBus())
    ws = FakeWebSocket()

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-config",
                "method": "config.get",
                "params": {"session_id": "sess-local"},
            }
        ),
        path="/tui",
    )

    await server.send(
        Message(
            id="req-after-reconnect",
            type="event",
            channel_id="tui",
            session_id="sess-local",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "resumed"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert bound_sessions == [("tui", "sess-local")]
    assert ws.sent_frames[-1] == {
        "type": "event",
        "event": "chat.delta",
        "payload": {"content": "resumed", "session_id": "sess-local"},
    }


@pytest.mark.asyncio
async def test_gateway_server_promotes_pending_session_client_after_stale_owner_cleanup():
    disconnected = []
    rebound = []

    async def disconnect_handler(ws, stale_session_keys, stale_request_keys):
        disconnected.append((stale_session_keys, stale_request_keys))

    def session_bind_handler(channel_id, session_id):
        rebound.append((channel_id, session_id))

    config = GatewayServerConfig(
        enabled=True,
        host="127.0.0.1",
        port=19001,
        routes={
            "/tui": RouteConfig(
                path="/tui",
                channel_id="tui",
                disconnect_handler=disconnect_handler,
                session_bind_handler=session_bind_handler,
            ),
        },
    )
    server = GatewayServerProbe(config, DummyBus())
    route = server.config.routes["/tui"]
    old_ws = IterableFakeWebSocket()
    new_ws = FakeWebSocket()
    server.bind_session_client("sess-race", old_ws, channel_id="tui")

    assert await server._bind_route_session_client(route, "sess-race", new_ws) is False

    await server._connection_handler(old_ws, "/tui")

    assert disconnected == [([("tui", "sess-race")], [])]
    assert rebound == [("tui", "sess-race")]
    assert server._session_to_client[("tui", "sess-race")] is new_ws


@pytest.mark.asyncio
async def test_gateway_server_accepts_legacy_single_route_config_and_session_map():
    server = GatewayServerProbe(
        GatewayServerConfig(
            enabled=True,
            host="127.0.0.1",
            port=19001,
            path="/acp",
            channel_id="acp",
        ),
        DummyBus(),
    )

    ws = FakeWebSocket()
    server.bind_session_client("sess-tool", ws)

    await server.send(
        Message(
            id="req-tool",
            type="event",
            channel_id="acp",
            session_id="sess-tool",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "acp.output_request",
                "jsonrpc": {
                    "jsonrpc": "2.0",
                    "id": "tool-legacy",
                    "method": "fs/read_text_file",
                    "params": {"path": "workspace/demo.txt", "sessionId": "sess-tool"},
                },
            },
        )
    )

    assert ws.sent_frames == [
        {
            "jsonrpc": "2.0",
            "id": "tool-legacy",
            "method": "fs/read_text_file",
            "params": {"path": "workspace/demo.txt", "sessionId": "sess-tool"},
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_passthroughs_acp_output_request_as_raw_jsonrpc():
    server = build_server()
    ws = FakeWebSocket()
    server.bind_session_client("sess-tool", ws)

    await server.send(
        Message(
            id="req-tool",
            type="event",
            channel_id="acp",
            session_id="sess-tool",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "acp.output_request",
                "jsonrpc": {
                    "jsonrpc": "2.0",
                    "id": "tool-1",
                    "method": "fs/read_text_file",
                    "params": {"path": "workspace/demo.txt", "sessionId": "sess-tool"},
                },
            },
        )
    )

    assert ws.sent_frames == [
        {
            "jsonrpc": "2.0",
            "id": "tool-1",
            "method": "fs/read_text_file",
            "params": {"path": "workspace/demo.txt", "sessionId": "sess-tool"},
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_defers_end_turn_until_pending_client_rpc_resolves():
    server = build_server()
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)
        if msg.req_method != ReqMethod.CHAT_SEND:
            return
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "final before client rpc"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={
                    "event_type": "acp.output_request",
                    "jsonrpc": {
                        "jsonrpc": "2.0",
                        "id": "tool-pending-2",
                        "method": "fs/write_text_file",
                        "params": {
                            "path": "workspace/demo.txt",
                            "content": "hello",
                            "sessionId": msg.session_id,
                        },
                    },
                },
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 211,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-pending-rpc",
                    "messageId": "user-msg-pending-rpc",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert [frame.get("method") for frame in ws.sent_frames] == [
        "session/update",
        "fs/write_text_file",
        "session/update",
    ]
    assert ws.sent_frames[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert ws.sent_frames[1] == {
        "jsonrpc": "2.0",
        "id": "tool-pending-2",
        "method": "fs/write_text_file",
        "params": {
            "path": "workspace/demo.txt",
            "content": "hello",
            "sessionId": "sess-pending-rpc",
        },
    }
    assert ws.sent_frames[2]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert not any(frame.get("id") == 211 and "result" in frame for frame in ws.sent_frames)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "tool-pending-2",
                "result": {"ok": True},
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[-1] == {
        "jsonrpc": "2.0",
        "id": 211,
        "result": {
            "stopReason": "end_turn",
            "userMessageId": "user-msg-pending-rpc",
        },
    }
    assert any(msg.req_method == ReqMethod.ACP_TOOL_RESPONSE for msg in seen)


@pytest.mark.asyncio
async def test_gateway_server_expired_pending_rpc_does_not_block_end_turn(monkeypatch):
    server = build_server()
    ws = FakeWebSocket()
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect._ACP_PENDING_RPC_TIMEOUT_SECONDS",
                        -1.0)

    async def on_message(msg):
        if msg.req_method != ReqMethod.CHAT_SEND:
            return
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "final before expired rpc"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={
                    "event_type": "acp.output_request",
                    "jsonrpc": {
                        "jsonrpc": "2.0",
                        "id": "tool-expired",
                        "method": "fs/write_text_file",
                        "params": {
                            "path": "workspace/demo.txt",
                            "content": "hello",
                            "sessionId": msg.session_id,
                        },
                    },
                },
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 212,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-expired-rpc",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[1] == {
        "jsonrpc": "2.0",
        "id": "tool-expired",
        "method": "fs/write_text_file",
        "params": {
            "path": "workspace/demo.txt",
            "content": "hello",
            "sessionId": "sess-expired-rpc",
        },
    }
    assert ws.sent_frames[2] == {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "sess-expired-rpc",
            "update": {"sessionUpdate": "session_info_update", "status": "idle"},
        },
    }
    assert ws.sent_frames[3] == {
        "jsonrpc": "2.0",
        "id": 212,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_handle_raw_jsonrpc_response_forwards_acp_tool_response():
    server = build_server()
    ws = FakeWebSocket()
    server.bind_session_client("sess-tool", ws)
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.send(
        Message(
            id="req-tool",
            type="event",
            channel_id="acp",
            session_id="sess-tool",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": "acp.output_request",
                "jsonrpc": {
                    "jsonrpc": "2.0",
                    "id": "tool-1",
                    "method": "fs/read_text_file",
                    "params": {"path": "workspace/demo.txt", "sessionId": "sess-tool"},
                },
            },
        )
    )

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "tool-1",
                "result": {"content": "hello"},
            },
            ensure_ascii=False,
        ),
    )

    assert len(seen) == 1
    msg = seen[0]
    assert msg.channel_id == "acp"
    assert msg.session_id == "sess-tool"
    assert msg.req_method == ReqMethod.ACP_TOOL_RESPONSE
    assert msg.params == {
        "jsonrpc_id": "tool-1",
        "response": {
            "jsonrpc": "2.0",
            "id": "tool-1",
            "result": {"content": "hello"},
        },
        "session_id": "sess-tool",
    }


@pytest.mark.asyncio
async def test_gateway_server_handle_unknown_jsonrpc_response_is_soft_ignored():
    server = build_server()
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "tool-unknown",
                "result": {"content": "late"},
            },
            ensure_ascii=False,
        ),
    )

    assert seen == []
    assert ws.sent_frames == [
        {
            "type": "res",
            "id": "tool-unknown",
            "ok": True,
            "payload": {
                "accepted": False,
                "ignored": True,
                "reason": "unknown_or_late_response",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_handles_acp_jsonrpc_prompt_and_streams_back_jsonrpc():
    server = build_server()
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "hello from gateway"},
                event_type=EventType.CHAT_DELTA,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "hello from gateway"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-jsonrpc",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert len(seen) == 1
    assert seen[0].req_method == ReqMethod.CHAT_SEND
    assert seen[0].session_id == "sess-jsonrpc"
    assert seen[0].params["query"] == "hello"

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["sessionId"] == "sess-jsonrpc"
    assert ws.sent_frames[-1] == {
        "jsonrpc": "2.0",
        "id": 99,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_emits_agent_message_chunk_from_chat_final_when_no_delta():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "gateway final only"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 199,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-gateway-final",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["sessionId"] == "sess-gateway-final"
    assert ws.sent_frames[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert ws.sent_frames[0]["params"]["update"]["content"] == {
        "type": "text",
        "text": "gateway final only",
    }
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[1]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert ws.sent_frames[2] == {
        "jsonrpc": "2.0",
        "id": 199,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_defers_end_turn_until_processing_idle_after_final_and_tool_updates():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "partial"},
                event_type=EventType.CHAT_DELTA,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "final"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={
                    "tool_name": "read_file",
                    "tool_call_id": "tool-call-9",
                    "result": "still running",
                },
                event_type=EventType.CHAT_TOOL_RESULT,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 299,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-final-tool",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[1]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert ws.sent_frames[1]["params"]["update"]["content"] == {
        "type": "text",
        "text": "final",
    }
    assert ws.sent_frames[2]["method"] == "session/update"
    assert ws.sent_frames[2]["params"]["update"] == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tool-call-9",
        "toolName": "read_file",
        "title": "Reading data",
        "kind": "read",
        "status": "completed",
        "result": "still running",
        "content": [{"type": "content", "content": {"type": "text", "text": "still running"}}],
    }
    assert ws.sent_frames[3]["method"] == "session/update"
    assert ws.sent_frames[3]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert ws.sent_frames[4] == {
        "jsonrpc": "2.0",
        "id": 299,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_waits_for_late_chat_final_after_idle_and_only_emits_missing_suffix():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "partial"},
                event_type=EventType.CHAT_DELTA,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "partial final answer"},
                event_type=EventType.CHAT_FINAL,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-gateway-late-final",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames == [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-gateway-late-final",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": ws.sent_frames[0]["params"]["update"]["messageId"],
                    "content": {"type": "text", "text": "partial"},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-gateway-late-final",
                "update": {
                    "sessionUpdate": "session_info_update",
                    "status": "idle",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-gateway-late-final",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": ws.sent_frames[0]["params"]["update"]["messageId"],
                    "content": {"type": "text", "text": " final answer"},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 301,
            "result": {"stopReason": "end_turn"},
        },
    ]


@pytest.mark.asyncio
async def test_gateway_server_prompt_result_echoes_user_message_id():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "gateway final"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 300,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-user-message-id",
                    "messageId": "user-msg-1",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[-1] == {
        "jsonrpc": "2.0",
        "id": 300,
        "result": {
            "stopReason": "end_turn",
            "userMessageId": "user-msg-1",
        },
    }


@pytest.mark.asyncio
async def test_gateway_server_does_not_end_turn_from_chat_final_before_late_tool_result(monkeypatch):
    import jiuwenswarm.gateway.app_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_PROMPT_IDLE_FINALIZE_SECONDS", 0.01)
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "final answer"},
                event_type=EventType.CHAT_FINAL,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={
                    "tool_name": "write_text_file",
                    "tool_call_id": "tool-call-late-1",
                    "result": "index.html written",
                },
                event_type=EventType.CHAT_TOOL_RESULT,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 399,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-idle-fallback",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )
    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[1]["params"]["update"] == {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tool-call-late-1",
        "toolName": "write_text_file",
        "title": "Editing files",
        "kind": "edit",
        "status": "completed",
        "result": "index.html written",
        "content": [{"type": "content", "content": {"type": "text", "text": "index.html written"}}],
    }
    assert ws.sent_frames[2]["method"] == "session/update"
    assert ws.sent_frames[2]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert ws.sent_frames[3] == {
        "jsonrpc": "2.0",
        "id": 399,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_emits_direct_reasoning_update_then_waits_for_idle():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "reasoning step", "event_type": "chat.reasoning"},
                event_type=EventType.CHAT_REASONING,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 401,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-reasoning-gateway",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    assert ws.sent_frames[0]["params"]["update"]["content"] == {
        "type": "text",
        "text": "reasoning step",
    }
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[1]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert ws.sent_frames[2] == {
        "jsonrpc": "2.0",
        "id": 401,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_emits_todo_update_then_waits_for_idle():
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={
                    "todos": [
                        {
                            "id": "todo-1",
                            "content": "Implement ACP todo update",
                            "activeForm": "Implementing ACP todo update",
                            "status": "in_progress",
                            "createdAt": "2026-04-16T00:00:00Z",
                            "updatedAt": "2026-04-16T00:05:00Z",
                        }
                    ]
                },
                event_type=EventType.TODO_UPDATED,
            )
        )
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"is_processing": False},
                event_type=EventType.CHAT_PROCESSING_STATUS,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 402,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-todo-gateway",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert ws.sent_frames[0]["method"] == "session/update"
    assert ws.sent_frames[0]["params"]["update"] == {
        "sessionUpdate": "todo_update",
        "todos": [
            {
                "id": "todo-1",
                "content": "Implement ACP todo update",
                "activeForm": "Implementing ACP todo update",
                "status": "in_progress",
                "createdAt": "2026-04-16T00:00:00Z",
                "updatedAt": "2026-04-16T00:05:00Z",
            }
        ],
    }
    assert ws.sent_frames[1]["method"] == "session/update"
    assert ws.sent_frames[1]["params"]["update"] == {
        "sessionUpdate": "session_info_update",
        "status": "idle",
    }
    assert ws.sent_frames[2] == {
        "jsonrpc": "2.0",
        "id": 402,
        "result": {"stopReason": "end_turn"},
    }


@pytest.mark.asyncio
async def test_gateway_server_delta_only_does_not_trigger_idle_finalize(monkeypatch):
    import jiuwenswarm.gateway.app_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_PROMPT_IDLE_FINALIZE_SECONDS", 0.01)
    server = build_server()
    ws = FakeWebSocket()

    async def on_message(msg):
        await server.send(
            Message(
                id=msg.id,
                type="event",
                channel_id="acp",
                session_id=msg.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": "partial answer"},
                event_type=EventType.CHAT_DELTA,
            )
        )

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 400,
                "method": "session/prompt",
                "params": {
                    "sessionId": "sess-delta-only",
                    "text": "hello",
                },
            },
            ensure_ascii=False,
        ),
    )

    pending_ctx = server.get_acp_pending_request_contexts_for_test()
    assert len(pending_ctx) == 1
    assert pending_ctx[0].idle_finalize_task is None

    assert len(ws.sent_frames) == 1
    assert ws.sent_frames[0]["jsonrpc"] == "2.0"
    assert ws.sent_frames[0]["method"] == "session/update"
    params = ws.sent_frames[0]["params"]
    assert params["sessionId"] == "sess-delta-only"
    update = params["update"]
    assert update["sessionUpdate"] == "agent_message_chunk"
    assert isinstance(update.get("messageId"), str)
    assert update["content"] == {"type": "text", "text": "partial answer"}


@pytest.mark.asyncio
async def test_gateway_server_handle_raw_message_forwards_request():
    server = build_server()
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-3",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-3",
                    "content": "hello",
                    "mode": "agent.fast",
                },
            },
            ensure_ascii=False,
        ),
    )

    assert len(seen) == 1
    msg = seen[0]
    assert msg.id == "req-3"
    assert msg.channel_id == "acp"
    assert msg.session_id == "sess-3"
    assert msg.req_method == ReqMethod.CHAT_SEND
    assert msg.params.get("content") == "hello"
    assert msg.mode.value == "agent"
    assert ws.sent_frames == []


@pytest.mark.asyncio
async def test_gateway_server_forwards_tui_client_timeout_as_metadata_only():
    server = build_server()
    server.config.routes["/tui"].forward_no_local_handler_methods = frozenset({"chat.send"})
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-tui-timeout",
                "method": "chat.send",
                "timeout_ms": 60_000,
                "params": {
                    "session_id": "sess-tui-timeout",
                    "content": "hello",
                    "mode": "code.normal",
                },
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert len(seen) == 1
    msg = seen[0]
    assert msg.channel_id == "tui"
    assert msg.metadata["client_timeout_ms"] == 60_000
    assert "timeout_ms" not in msg.params
    assert ws.sent_frames == []


@pytest.mark.asyncio
async def test_gateway_server_handle_raw_message_rejects_unknown_method():
    server = build_server()
    ws = FakeWebSocket()

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-4",
                "method": "unknown.method",
                "params": {"session_id": "sess-4"},
            },
            ensure_ascii=False,
        ),
    )

    assert len(ws.sent_frames) == 1
    frame = ws.sent_frames[0]
    assert frame["type"] == "res"
    assert frame["id"] == "req-4"
    assert frame["ok"] is False
    assert frame["error"] == "unknown method: unknown.method"


@pytest.mark.asyncio
async def test_gateway_server_send_event_routes_same_session_id_by_channel():
    server = build_server()
    acp_ws = FakeWebSocket()
    cli_ws = FakeWebSocket()
    server.bind_session_client("shared-session", acp_ws, channel_id="acp")
    server.bind_session_client("shared-session", cli_ws, channel_id="tui")

    await server.send(
        Message(
            id="req-cli",
            type="event",
            channel_id="tui",
            session_id="shared-session",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "hello cli"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert acp_ws.sent_frames == []
    assert cli_ws.sent_frames == [
        {
            "type": "event",
            "event": "chat.delta",
            "payload": {
                "content": "hello cli",
                "session_id": "shared-session",
            },
        }
    ]


@pytest.mark.asyncio
async def test_gateway_server_routes_event_by_payload_session_id_only():
    """Fallback routes by payload session_id when msg.session_id is unset."""
    server = build_server()
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    server.bind_session_client("sess-a", ws_a, channel_id="tui")
    server.bind_session_client("sess-b", ws_b, channel_id="tui")

    await server.send(
        Message(
            id="req-a",
            type="event",
            channel_id="tui",
            session_id=None,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "for a", "session_id": "sess-a"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert len(ws_a.sent_frames) == 1
    assert ws_b.sent_frames == []


@pytest.mark.asyncio
async def test_gateway_server_session_less_event_broadcasts_to_all_tui_clients():
    """Session-less channel notifications (e.g. cron) broadcast to all TUI clients."""
    server = build_server()
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    server.bind_session_client("sess-a", ws_a, channel_id="tui")
    server.bind_session_client("sess-b", ws_b, channel_id="tui")

    await server.send(
        Message(
            id="cron-push-1",
            type="event",
            channel_id="tui",
            session_id=None,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "cron reminder", "cron": {"job_id": "j1"}},
            event_type=EventType.CHAT_FINAL,
        )
    )

    assert len(ws_a.sent_frames) == 1
    assert len(ws_b.sent_frames) == 1
    assert ws_a.sent_frames[0]["payload"]["content"] == "cron reminder"
    assert ws_b.sent_frames[0]["payload"]["content"] == "cron reminder"


@pytest.mark.asyncio
async def test_gateway_server_drops_event_when_session_client_gone():
    """Known session_id with no connected client is dropped, not broadcast."""
    server = build_server()
    ws_b = FakeWebSocket()
    server.bind_session_client("sess-b", ws_b, channel_id="tui")

    await server.send(
        Message(
            id="req-gone",
            type="event",
            channel_id="tui",
            session_id="sess-gone",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "stale"},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert ws_b.sent_frames == []


@pytest.mark.asyncio
async def test_gateway_server_routes_by_params_session_id_when_payload_empty():
    """params.session_id is checked even when payload is an empty dict."""
    server = build_server()
    ws = FakeWebSocket()
    server.bind_session_client("sess-params", ws, channel_id="tui")

    await server.send(
        Message(
            id="req-params",
            type="event",
            channel_id="tui",
            session_id=None,
            params={"session_id": "sess-params"},
            timestamp=time.time(),
            ok=True,
            payload={},
            event_type=EventType.CHAT_DELTA,
        )
    )

    assert len(ws.sent_frames) == 1
    assert ws.sent_frames[0]["payload"].get("session_id") is None


@pytest.mark.asyncio
async def test_gateway_server_handle_raw_message_uses_connection_user_id():
    server = build_server()
    ws = FakeWebSocket(user_id="alice")
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-user",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-user",
                    "content": "hello",
                },
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert len(seen) == 1
    assert seen[0].user_id == "alice"


def test_gateway_server_extract_ws_user_id_case_insensitive():
    ws_lower = type(
        "Ws",
        (),
        {"request_headers": _FakeRequestHeaders({"x-user-id": "bob"})},
    )()
    ws_upper = type(
        "Ws",
        (),
        {"request": type("Request", (), {"headers": _FakeRequestHeaders({"X-User-Id": "  carol  "})})()},
    )()
    ws_empty = type("Ws", (), {"request_headers": _FakeRequestHeaders({})})()

    assert GatewayServerProbe.extract_ws_user_id_for_test(ws_lower) == "bob"
    assert GatewayServerProbe.extract_ws_user_id_for_test(ws_upper) == "carol"
    assert GatewayServerProbe.extract_ws_user_id_for_test(ws_empty) is None


@pytest.mark.asyncio
async def test_gateway_server_local_handler_receives_connection_user_id():
    captured_user_ids = []

    async def _session_list(ws, req_id, params, session_id, user_id=None):
        captured_user_ids.append(user_id)

    server = build_server()
    server.config.routes["/tui"].local_handlers["session.list"] = _session_list
    ws = FakeWebSocket(user_id="alice")

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-sess",
                "method": "session.list",
                "params": {},
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert captured_user_ids == ["alice"]


@pytest.mark.asyncio
async def test_gateway_server_ignores_frame_x_user_id_without_handshake_header():
    server = build_server()
    ws = FakeWebSocket()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-user",
                "method": "chat.send",
                "X-User-Id": "alice",
                "params": {
                    "session_id": "sess-user",
                    "content": "hello",
                },
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert len(seen) == 1
    assert seen[0].user_id is None


def _build_tui_server_with_agent_switch() -> GatewayServerProbe:
    config = GatewayServerConfig(
        enabled=True,
        host="127.0.0.1",
        port=19002,
        routes={
            "/tui": RouteConfig(
                path="/tui",
                channel_id="tui",
                forward_methods=frozenset(
                    {
                        ReqMethod.CHAT_SEND.value,
                    }
                ),
            ),
        },
    )
    return GatewayServerProbe(config, DummyBus())


@pytest.mark.asyncio
async def test_gateway_injects_default_agent_type_on_forward():
    server = _build_tui_server_with_agent_switch()
    ws = FakeWebSocket(user_id="alice")
    setattr(ws, "_gateway_agent_type", "jiuwenswarm")
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-chat",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-1",
                    "content": "hello",
                },
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert len(seen) == 1
    assert seen[0].params.get("agent_type") == "jiuwenswarm"


@pytest.fixture
def _third_agent_registry():
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

    from jiuwenswarm.extensions.registry import ExtensionRegistry

    ExtensionRegistry.reset_instance()
    registry = ExtensionRegistry.create_instance(AsyncCallbackFramework(), {}, None)
    yield registry
    ExtensionRegistry.reset_instance()


def _resolve_third_agent_for_test():
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.gateway.routing.third_agent import get_unsupported_third_agent

    third = ExtensionRegistry.get_instance().get_third_agent()
    return third if third is not None else get_unsupported_third_agent()


@pytest.mark.asyncio
async def test_gateway_agent_list_uses_local_handler_default_unsupported(
    _third_agent_registry,
):
    server = _build_tui_server_with_agent_switch()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    async def _list(ws, req_id, params, session_id, user_id=None):
        third = _resolve_third_agent_for_test()
        result = await third.thirdagent_list(
            user_id=str(user_id or ""),
            current_agent_type=str(getattr(ws, "_gateway_agent_type", "") or ""),
        )
        await server.send_response(
            ws,
            req_id,
            ok=bool(result.get("ok")),
            payload=result.get("payload"),
            error=result.get("error"),
            code=result.get("code"),
        )

    server.register_local_handler("/tui", "3rdagent.list", _list)
    ws = FakeWebSocket(user_id="alice")
    setattr(ws, "_gateway_agent_type", "claude")

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-list",
                "method": "3rdagent.list",
                "params": {"session_id": "sess-1"},
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert seen == []
    assert len(ws.sent_frames) == 1
    assert ws.sent_frames[0]["ok"] is False
    assert ws.sent_frames[0].get("code") == "UNSUPPORTED"


@pytest.mark.asyncio
async def test_gateway_agent_switch_local_handler_updates_connection(
    _third_agent_registry,
):
    from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
    from jiuwenswarm.gateway.routing.third_agent import ThirdAgent

    class _FakeThirdAgent(ThirdAgent):
        def normalize_agent_type(self, raw):
            return str(raw or "jiuwenswarm").strip().lower() or "jiuwenswarm"

        async def thirdagent_list(self, *, user_id, current_agent_type=""):
            del user_id, current_agent_type
            return {"ok": True, "payload": {"agents": []}}

        async def thirdagent_switch(self, *, user_id, agent_type, session_id="", params=None):
            del user_id, session_id, params
            normalized = self.normalize_agent_type(agent_type)
            if normalized == "unknown":
                return {
                    "ok": False,
                    "error": f"unsupported agent_type: {normalized}",
                    "code": "UNSUPPORTED_AGENT_TYPE",
                }
            return {
                "ok": True,
                "payload": {
                    "agent_id": "a1",
                    "agent_type": normalized,
                    "sandbox_id": "sbx-1",
                    "status": "ready",
                },
            }

    class _FakeThirdAgentExt(ThirdAgentExtension):
        def __init__(self, impl: ThirdAgent) -> None:
            self._impl = impl

        async def initialize(self, config) -> None:
            del config

        def get_third_agent(self) -> ThirdAgent:
            return self._impl

    _third_agent_registry.register_third_agent(_FakeThirdAgentExt(_FakeThirdAgent()))
    server = _build_tui_server_with_agent_switch()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    async def _switch(ws, req_id, params, session_id, user_id=None):
        params = params if isinstance(params, dict) else {}
        third = _resolve_third_agent_for_test()
        result = await third.thirdagent_switch(
            user_id=str(user_id or ""),
            agent_type=str(params.get("agent_type") or ""),
            session_id=str(session_id or ""),
            params=params,
        )
        if result.get("ok"):
            payload = dict(result.get("payload") or {})
            switched = str(payload.get("agent_type") or "").strip()
            if switched:
                setattr(ws, "_gateway_agent_type", switched)
            await server.send_response(ws, req_id, ok=True, payload=payload)
            return
        await server.send_response(
            ws,
            req_id,
            ok=False,
            error=result.get("error"),
            code=result.get("code"),
        )

    server.register_local_handler("/tui", "3rdagent.switch", _switch)
    ws = FakeWebSocket(user_id="alice")
    setattr(ws, "_gateway_agent_type", "jiuwenswarm")

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-switch",
                "method": "3rdagent.switch",
                "params": {"agent_type": "claude", "session_id": "sess-1"},
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert getattr(ws, "_gateway_agent_type") == "claude"
    assert seen == []
    assert ws.sent_frames[0]["ok"] is True

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-chat",
                "method": "chat.send",
                "params": {
                    "session_id": "sess-1",
                    "content": "hello",
                    "agent_type": "opencode",
                },
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert len(seen) == 1
    assert seen[0].params.get("agent_type") == "claude"


@pytest.mark.asyncio
async def test_gateway_agent_switch_rejects_unsupported_type(_third_agent_registry):
    from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
    from jiuwenswarm.gateway.routing.third_agent import ThirdAgent

    class _FakeThirdAgent(ThirdAgent):
        async def thirdagent_list(self, *, user_id, current_agent_type=""):
            del user_id, current_agent_type
            return {"ok": True, "payload": {"agents": []}}

        async def thirdagent_switch(self, *, user_id, agent_type, session_id="", params=None):
            del user_id, session_id, params
            return {
                "ok": False,
                "error": f"unsupported agent_type: {agent_type}",
                "code": "UNSUPPORTED_AGENT_TYPE",
            }

    class _FakeThirdAgentExt(ThirdAgentExtension):
        def __init__(self, impl: ThirdAgent) -> None:
            self._impl = impl

        async def initialize(self, config) -> None:
            del config

        def get_third_agent(self) -> ThirdAgent:
            return self._impl

    _third_agent_registry.register_third_agent(_FakeThirdAgentExt(_FakeThirdAgent()))
    server = _build_tui_server_with_agent_switch()
    seen = []

    async def on_message(msg):
        seen.append(msg)

    server.on_message(on_message)

    async def _switch(ws, req_id, params, session_id, user_id=None):
        params = params if isinstance(params, dict) else {}
        third = _resolve_third_agent_for_test()
        result = await third.thirdagent_switch(
            user_id=str(user_id or ""),
            agent_type=str(params.get("agent_type") or ""),
            session_id=str(session_id or ""),
            params=params,
        )
        await server.send_response(
            ws,
            req_id,
            ok=bool(result.get("ok")),
            payload=result.get("payload"),
            error=result.get("error"),
            code=result.get("code"),
        )

    server.register_local_handler("/tui", "3rdagent.switch", _switch)
    ws = FakeWebSocket(user_id="alice")
    setattr(ws, "_gateway_agent_type", "jiuwenswarm")

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-switch-bad",
                "method": "3rdagent.switch",
                "params": {"agent_type": "unknown", "session_id": "sess-1"},
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert getattr(ws, "_gateway_agent_type") == "jiuwenswarm"
    assert seen == []
    assert len(ws.sent_frames) == 1
    assert ws.sent_frames[0]["ok"] is False
    assert ws.sent_frames[0].get("code") == "UNSUPPORTED_AGENT_TYPE"


def test_resolve_3rdagent_switch_session_id_requires_explicit_param():
    from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
        resolve_3rdagent_switch_session_id,
    )

    assert resolve_3rdagent_switch_session_id(None) == ""
    assert resolve_3rdagent_switch_session_id({}) == ""
    assert resolve_3rdagent_switch_session_id({"session_id": "  "}) == ""
    assert resolve_3rdagent_switch_session_id({"session_id": "sess-1"}) == "sess-1"


@pytest.mark.asyncio
async def test_gateway_agent_switch_rejects_missing_session_id():
    server = _build_tui_server_with_agent_switch()
    from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
        resolve_3rdagent_switch_session_id,
    )

    async def _switch(ws, req_id, params, session_id, user_id=None):
        del session_id, user_id
        params = params if isinstance(params, dict) else {}
        if not resolve_3rdagent_switch_session_id(params):
            await server.send_response(
                ws,
                req_id,
                ok=False,
                error="session_id is required for 3rdagent.switch",
                code="BAD_REQUEST",
            )
            return
        await server.send_response(ws, req_id, ok=True, payload={})

    server.register_local_handler("/tui", "3rdagent.switch", _switch)
    ws = FakeWebSocket(user_id="alice")
    setattr(ws, "_gateway_agent_type", "jiuwenswarm")

    await server.handle_raw_message_public(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "req-switch-no-sid",
                "method": "3rdagent.switch",
                "params": {"agent_type": "claude"},
            },
            ensure_ascii=False,
        ),
        path="/tui",
    )

    assert getattr(ws, "_gateway_agent_type") == "jiuwenswarm"
    assert len(ws.sent_frames) == 1
    assert ws.sent_frames[0]["ok"] is False
    assert ws.sent_frames[0].get("code") == "BAD_REQUEST"