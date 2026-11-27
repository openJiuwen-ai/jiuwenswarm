import json
import time
from typing import Any

import pytest

from jiuwenclaw.app_gateway import (
    GatewayServer,
    GatewayServerConfig,
    RouteConfig,
    _normalize_gateway_message,
)
from jiuwenclaw.schema.message import EventType, Message, ReqMethod


class DummyBus:
    @staticmethod
    async def publish_user_messages(msg):
        return None


class FakeWebSocket:
    def __init__(self):
        self.sent_frames = []
        self.closed = False

    async def send(self, data):
        self.sent_frames.append(json.loads(data))

    async def close(self, code=None, reason=None):
        self.closed = True
        return code, reason


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
    assert result["agentInfo"]["name"] == "jiuwenclaw"
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
    monkeypatch.setattr("jiuwenclaw.channel.acp_channel._ACP_PENDING_RPC_TIMEOUT_SECONDS", -1.0)

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
    import jiuwenclaw.app_gateway as gateway_module

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
    import jiuwenclaw.app_gateway as gateway_module

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
    assert msg.mode.value == "agent.fast"
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
