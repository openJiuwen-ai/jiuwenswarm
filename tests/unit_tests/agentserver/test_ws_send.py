import asyncio
import io
import json
import tokenize
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server import ws_send
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_send_wire_payload_sends_small_wire_unchanged(monkeypatch):
    monkeypatch.setattr(ws_send, "AGENT_WS_SEND_BUDGET_BYTES", 1024)
    ws = FakeWebSocket()
    wire = {"request_id": "r1", "body": {"result": "ok"}}

    assert await ws_send.send_wire_payload(ws, wire) is True
    assert json.loads(ws.sent[0]) == wire


@pytest.mark.asyncio
async def test_send_wire_payload_counts_utf8_bytes(monkeypatch):
    wire = {"request_id": "r1", "body": {"result": "你" * 400}}
    character_size = len(json.dumps(wire, ensure_ascii=False))
    byte_size = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(ws_send, "AGENT_WS_SEND_BUDGET_BYTES", 1200)
    ws = FakeWebSocket()

    assert character_size < 1200 < byte_size
    assert await ws_send.send_wire_payload(ws, wire) is False
    assert len(ws.sent[0].encode("utf-8")) <= 1200


@pytest.mark.asyncio
async def test_oversized_unary_sends_e2a_error(monkeypatch):
    monkeypatch.setattr(ws_send, "AGENT_WS_SEND_BUDGET_BYTES", 2048)
    source = encode_agent_response_for_wire(
        AgentResponse(
            request_id="r-unary",
            channel_id="web",
            ok=True,
            payload={"content": "x" * 4096},
            agent_ref={"mode": "code", "id": "default"},
        ),
        response_id="r-unary",
    )
    source["session_id"] = "session-1"
    ws = FakeWebSocket()

    assert await ws_send.send_wire_payload(ws, source) is False

    fallback = json.loads(ws.sent[0])
    assert fallback["response_kind"] == "e2a.error"
    assert fallback["request_id"] == "r-unary"
    assert fallback["session_id"] == "session-1"
    assert fallback["agent_ref"] == {"mode": "code", "id": "default"}
    assert fallback["body"]["details"]["code"] == "response_too_large"
    assert fallback["body"]["details"]["actual_bytes"] > 2048
    assert fallback["body"]["details"]["max_bytes"] == 2048
    assert len(ws.sent[0].encode("utf-8")) <= 2048


@pytest.mark.asyncio
async def test_oversized_stream_sends_final_error_chunk(monkeypatch):
    monkeypatch.setattr(ws_send, "AGENT_WS_SEND_BUDGET_BYTES", 2048)
    source = encode_agent_chunk_for_wire(
        AgentResponseChunk(
            request_id="r-stream",
            channel_id="web",
            payload={"event_type": "chat.tool_result", "result": "x" * 4096},
            is_complete=False,
            agent_ref={"mode": "team", "id": "team-1"},
        ),
        response_id="r-stream",
        sequence=7,
    )
    ws = FakeWebSocket()

    assert await ws_send.send_wire_payload(ws, source) is False

    raw_fallback = json.loads(ws.sent[0])
    fallback = parse_agent_server_wire_chunk(raw_fallback)
    assert raw_fallback["sequence"] == 7
    assert raw_fallback["agent_ref"] == {"mode": "team", "id": "team-1"}
    assert fallback.is_complete is True
    assert fallback.payload["event_type"] == "chat.error"
    assert fallback.payload["code"] == "response_too_large"
    assert len(ws.sent[0].encode("utf-8")) <= 2048


@pytest.mark.asyncio
async def test_oversized_server_push_preserves_push_marker(monkeypatch):
    monkeypatch.setattr(ws_send, "AGENT_WS_SEND_BUDGET_BYTES", 2048)
    source = build_server_push_wire(
        {
            "request_id": "push-1",
            "channel_id": "web",
            "session_id": "session-push",
            "payload": {"result": "x" * 4096},
        }
    )
    ws = FakeWebSocket()

    assert await ws_send.send_wire_payload(ws, source) is False

    fallback = json.loads(ws.sent[0])
    assert fallback["metadata"][E2A_WIRE_SERVER_PUSH_KEY] is True
    assert fallback["session_id"] == "session-push"
    assert len(ws.sent[0].encode("utf-8")) <= 2048


@pytest.mark.asyncio
async def test_stream_stops_after_oversized_chunk_is_replaced(monkeypatch):
    stream_closed = False

    class FakeAgent:
        def __init__(self) -> None:
            self.yielded: list[int] = []

        async def process_message_stream(self, request):
            nonlocal stream_closed
            try:
                for index in range(2):
                    self.yielded.append(index)
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"content": str(index)},
                        is_complete=False,
                    )
            finally:
                stream_closed = True

    class RuntimeManager:
        def __init__(self):
            self.agent = FakeAgent()
            self.events: list[str] = []

        async def wait_for_session_prewarm(self, session_id):
            self.events.append("wait")

        async def get_agent(self, **kwargs):
            self.events.append("get")
            return self.agent

        async def begin_foreground_chat(self):
            self.events.append("begin")

        async def end_foreground_chat(self):
            self.events.append("end")

    class PlanController:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def ensure_state(self, *args):
            self.events.append("ensure")
            return PlanStateResult()

        async def check_post_process_exit(self, *args):
            self.events.append("check")
            return []

    async def no_runtime_initialization() -> None:
        return None

    async def no_extension_hook(request) -> None:
        return None

    runtime_manager = RuntimeManager()
    plan_controller = PlanController()
    runtime = AgentRuntime(
        agent_manager=runtime_manager,
        initializer=no_runtime_initialization,
        plan_controller=plan_controller,
    )
    runtime._trigger_before_chat_request_hook = no_extension_hook

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._session_stream_tasks = {}
    server._agent_manager = runtime_manager
    server._runtime = runtime

    send_count = 0

    async def replace_with_oversized_error(ws, wire):
        nonlocal send_count
        send_count += 1
        return False

    monkeypatch.setattr(
        agent_ws_server,
        "send_wire_payload",
        replace_with_oversized_error,
    )
    request = AgentRequest(
        request_id="stream-too-large",
        channel_id="web",
        session_id=None,
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "work_mode": "work"},
        is_stream=True,
    )

    await server._handle_stream(FakeWebSocket(), request, asyncio.Lock())
    await asyncio.sleep(0)

    assert send_count == 1
    assert stream_closed is True
    assert runtime.started is True
    assert runtime_manager.agent.yielded == [0]
    assert runtime_manager.events == ["begin", "wait", "get", "end"]
    assert plan_controller.events == ["ensure", "check"]
    assert server._session_stream_tasks == {}


@pytest.mark.asyncio
async def test_team_stream_admission_is_owned_by_actual_round_not_transport():
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    admission = SimpleNamespace(
        begin_user=AsyncMock(),
        end_user=AsyncMock(),
    )
    server._agent_manager = None
    server._heartbeat_runtime = SimpleNamespace(admission=admission)
    server._should_trigger_before_chat_request_hook = lambda request: False
    server._handle_stream_impl = AsyncMock()
    request = AgentRequest(
        request_id="team-persistent-stream",
        channel_id="web",
        session_id="team-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team"},
        is_stream=True,
    )

    await server._handle_stream(FakeWebSocket(), request, asyncio.Lock())

    admission.begin_user.assert_not_awaited()
    admission.end_user.assert_not_awaited()


def test_agent_ws_server_has_no_direct_websocket_send_calls():
    path = Path(agent_ws_server.__file__)
    source = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    direct_sends = [
        token.start[0]
        for index, token in enumerate(tokens[1:-1], start=1)
        if token.type == tokenize.NAME
        and token.string == "send"
        and tokens[index - 1].type == tokenize.OP
        and tokens[index - 1].string == "."
        and tokens[index + 1].type == tokenize.OP
        and tokens[index + 1].string == "("
    ]

    assert direct_sends == []


@pytest.mark.asyncio
async def test_send_runtime_event_preserves_metadata_for_unary_and_stream() -> None:
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    event = RuntimeEvent(
        request_id="metadata-event",
        channel_id="team",
        session_id="session-1",
        payload={"event_type": "chat.delta", "content": "hello"},
        metadata={"fan_out_targets": ["member-a", "member-b"]},
    )

    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=True,
        sequence=0,
    )
    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=False,
        sequence=0,
    )

    stream_wire, unary_wire = [json.loads(payload) for payload in ws.sent]
    expected = {"fan_out_targets": ["member-a", "member-b"]}
    assert stream_wire["metadata"] == expected
    assert unary_wire["metadata"] == expected


@pytest.mark.asyncio
async def test_none_payload_complete_chunk_keeps_terminal_sentinel_wire_semantics() -> None:
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    source = AgentResponseChunk(
        request_id="terminal-sentinel",
        channel_id="team",
        payload=None,
        is_complete=True,
        agent_ref={"mode": "team", "id": "default"},
        metadata={"route": "stream"},
    )
    event = RuntimeEvent.from_agent_message(
        source,
        request_id=source.request_id,
        channel_id=source.channel_id,
        session_id="session-1",
    )

    assert event.payload is None
    assert event.event_type == ""
    assert event.is_complete is True

    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=True,
        sequence=0,
    )

    wire = json.loads(ws.sent[0])
    assert wire["response_kind"] == "e2a.complete"
    assert wire["is_final"] is True
    assert wire["body"] == {"result": {}}
    assert "chat.final" not in ws.sent[0]

    decoded = parse_agent_server_wire_chunk(wire)
    assert decoded.payload == {"is_complete": True}
    assert decoded.is_complete is True
    assert decoded.agent_ref == {"mode": "team", "id": "default"}
    assert decoded.metadata == {"route": "stream"}


@pytest.mark.asyncio
async def test_none_payload_unary_response_keeps_wire_semantics() -> None:
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    source = AgentResponse(
        request_id="unary-none",
        channel_id="web",
        ok=True,
        payload=None,
        metadata={"route": "unary"},
    )
    event = RuntimeEvent.from_agent_message(
        source,
        request_id=source.request_id,
        channel_id=source.channel_id,
        session_id="session-1",
    )

    assert event.payload is None
    assert event.event_type == ""

    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=False,
        sequence=0,
    )

    wire = json.loads(ws.sent[0])
    assert wire["response_kind"] == "e2a.complete"
    assert wire["body"] == {"result": {}}
    assert "chat.final" not in ws.sent[0]

    decoded = parse_agent_server_wire_unary(wire)
    assert decoded.payload == {}
    assert decoded.ok is True
    assert decoded.metadata == {"route": "unary"}


@pytest.mark.asyncio
async def test_stream_runtime_error_uses_legacy_agent_error_wire_contract() -> None:
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    event = RuntimeEvent.error(
        request_id="runtime-error",
        channel_id="web",
        session_id="session-1",
        error=ValueError("boom"),
        metadata={"trace_id": "stream-error"},
    )

    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=True,
        sequence=0,
    )

    wire = json.loads(ws.sent[0])
    assert wire["response_kind"] == "e2a.error"
    assert wire["status"] == "failed"
    assert wire["is_final"] is True
    assert wire["is_stream"] is False
    assert wire["body"] == {
        "code": "E2A.AGENT_ERROR",
        "message": "boom",
        "details": {"error": "boom"},
    }
    assert wire["provenance"]["details"] == {
        "kind": "legacy_agent_response",
        "ok": False,
    }

    decoded = parse_agent_server_wire_chunk(wire)
    assert decoded.payload == {"error": "boom"}
    assert decoded.is_complete is True
    assert decoded.metadata == {"trace_id": "stream-error"}


@pytest.mark.asyncio
async def test_unary_runtime_error_uses_legacy_agent_error_wire_contract() -> None:
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    ws = FakeWebSocket()
    event = RuntimeEvent.error(
        request_id="runtime-error",
        channel_id="web",
        session_id="session-1",
        error=ValueError("boom"),
        metadata={"trace_id": "unary-error"},
    )

    await server._send_runtime_event(
        ws,
        event,
        asyncio.Lock(),
        streaming=False,
        sequence=0,
    )

    wire = json.loads(ws.sent[0])
    assert wire["response_kind"] == "e2a.error"
    assert wire["status"] == "failed"
    assert wire["is_final"] is True
    assert wire["is_stream"] is False
    assert wire["body"] == {
        "code": "E2A.AGENT_ERROR",
        "message": "boom",
        "details": {"error": "boom"},
    }

    decoded = parse_agent_server_wire_unary(wire)
    assert decoded.payload == {"error": "boom"}
    assert decoded.ok is False
    assert decoded.metadata == {"trace_id": "unary-error"}


@pytest.mark.asyncio
async def test_server_unary_disables_duplicate_runtime_hook() -> None:
    manager = object()

    class RecordingRuntime:
        agent_manager = manager

        def __init__(self) -> None:
            self.call: tuple[bool, object] | None = None

        async def invoke(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            self.call = (trigger_hook, on_control_event)
            return [
                RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "done"},
                    is_complete=True,
                )
            ]

    runtime = RecordingRuntime()
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._agent_manager = manager
    server._runtime = runtime
    request = AgentRequest(
        request_id="unary-hook-once",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
    )
    ws = FakeWebSocket()

    await server._handle_unary_impl(ws, request, asyncio.Lock())

    assert runtime.call is not None
    trigger_hook, control_handler = runtime.call
    assert trigger_hook is False
    assert callable(control_handler)
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_server_chat_answer_uses_runtime_interaction_api() -> None:
    manager = object()

    class RecordingRuntime:
        agent_manager = manager

        def __init__(self) -> None:
            self.call: tuple[object, bool, object] | None = None

        async def answer_interaction(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            self.call = (request, trigger_hook, on_control_event)
            return [
                RuntimeEvent(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={"event_type": "chat.final", "content": "answered"},
                    is_complete=True,
                )
            ]

        async def invoke(self, *_args, **_kwargs):
            raise AssertionError("CHAT_ANSWER must use answer_interaction")

    runtime = RecordingRuntime()
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._agent_manager = manager
    server._runtime = runtime
    request = AgentRequest(
        request_id="interaction-runtime-api",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_ANSWER,
        params={"answer": "approve"},
    )
    ws = FakeWebSocket()

    await server._handle_unary_impl(ws, request, asyncio.Lock())

    assert runtime.call is not None
    actual_request, trigger_hook, control_handler = runtime.call
    assert actual_request is request
    assert trigger_hook is False
    assert callable(control_handler)
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_stream_control_event_does_not_consume_chunk_sequence() -> None:
    manager = object()
    runtime_call: dict[str, object] = {}

    class RecordingRuntime:
        agent_manager = manager

        async def stream(
            self,
            request,
            *,
            trigger_hook=True,
            on_control_event=None,
        ):
            runtime_call["trigger_hook"] = trigger_hook
            runtime_call["control_handler"] = on_control_event
            await on_control_event(
                RuntimeEvent.control(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    payload={
                        "event_type": "plan.mode_exited",
                        "mode": "code.normal",
                    },
                )
            )
            yield RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={"event_type": "chat.delta", "content": "hello"},
            )

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._agent_manager = manager
    server._runtime = RecordingRuntime()
    server._session_stream_tasks = {}
    server.send_push = AsyncMock()
    request = AgentRequest(
        request_id="stream-control-sequence",
        channel_id="tui",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent"},
        is_stream=True,
    )
    ws = FakeWebSocket()

    await server._handle_stream_impl(ws, request, asyncio.Lock())

    assert runtime_call["trigger_hook"] is False
    assert callable(runtime_call["control_handler"])
    server.send_push.assert_awaited_once()
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0])["sequence"] == 0
