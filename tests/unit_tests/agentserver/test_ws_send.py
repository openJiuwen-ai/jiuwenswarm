import asyncio
import io
import json
import tokenize
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.execution import (
    SessionRunAdmission,
)
from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    parse_agent_server_wire_chunk,
)
from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.common.schema.message import ReqMethod
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
        async def process_message_stream(self, request):
            nonlocal stream_closed
            try:
                for index in range(2):
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"content": str(index)},
                        is_complete=False,
                    )
            finally:
                stream_closed = True

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._session_stream_tasks = {}
    server._is_stateless_method_request = lambda request: True

    class ForegroundManager:
        def __init__(self):
            self.events = []

        async def begin_foreground_chat(self):
            self.events.append("begin")

        async def end_foreground_chat(self):
            self.events.append("end")

    foreground_manager = ForegroundManager()
    server._agent_manager = foreground_manager
    server._heartbeat_runtime = SimpleNamespace(
        is_available=True,
        admission=SimpleNamespace(
            begin_user=AsyncMock(),
            end_user=AsyncMock(),
        )
    )

    async def get_agent(channel_id):
        return FakeAgent()

    async def no_plan_exit_check(request, agent):
        return None

    send_count = 0

    async def replace_with_oversized_error(ws, wire):
        nonlocal send_count
        send_count += 1
        return False

    server._get_stateless_agent = get_agent
    server._check_post_process_plan_exit = no_plan_exit_check
    monkeypatch.setattr(
        agent_ws_server,
        "send_wire_payload",
        replace_with_oversized_error,
    )
    request = AgentRequest(
        request_id="stream-too-large",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={},
        is_stream=True,
    )

    await server._handle_stream(FakeWebSocket(), request, asyncio.Lock())

    assert send_count == 1
    assert stream_closed is True
    assert foreground_manager.events == ["begin", "end"]


@pytest.mark.asyncio
async def test_stream_releases_admission_before_transport_cleanup_finishes(
    monkeypatch,
):
    keepalive_send_started = asyncio.Event()
    keepalive_cancelled = asyncio.Event()
    release_keepalive_send = asyncio.Event()

    class FakeAgent:
        async def process_message_stream(self, request):
            await keepalive_send_started.wait()
            if release_keepalive_send.is_set():
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                )

    async def blocking_keepalive_send(ws, wire):
        if wire.get("sequence") != -1:
            return True
        keepalive_send_started.set()
        try:
            await release_keepalive_send.wait()
        except asyncio.CancelledError:
            keepalive_cancelled.set()
            await release_keepalive_send.wait()
            raise
        return True

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    admission = SessionRunAdmission()
    server._agent_manager = None
    server._heartbeat_runtime = SimpleNamespace(admission=admission)
    server._session_stream_tasks = {}
    server._should_trigger_before_chat_request_hook = lambda request: False
    server._is_stateless_method_request = lambda request: True
    server._is_readonly_goal_get_request = lambda request: False
    server._try_start_heartbeat_runtime = AsyncMock()
    server._check_post_process_plan_exit = AsyncMock()
    server._get_stateless_agent = AsyncMock(return_value=FakeAgent())
    monkeypatch.setattr(
        agent_ws_server,
        "_STREAM_KEEPALIVE_INTERVAL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        agent_ws_server,
        "send_wire_payload",
        blocking_keepalive_send,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep."
        "ensure_persistent_checkpointer",
        AsyncMock(),
    )
    request = AgentRequest(
        request_id="ask-user-stream",
        channel_id="web",
        session_id="ask-user-session",
        req_method=ReqMethod.CHAT_SEND,
        params={},
        is_stream=True,
    )
    handler = asyncio.create_task(
        server._handle_stream(FakeWebSocket(), request, asyncio.Lock())
    )

    try:
        await asyncio.wait_for(keepalive_cancelled.wait(), timeout=1.0)
        assert handler.done() is False
        assert admission.is_user_active("ask-user-session") is False
        assert (
            await admission.try_begin_heartbeat(
                "ask-user-session",
                "heartbeat-after-answer",
            )
            is True
        )
        await admission.end_heartbeat(
            "ask-user-session",
            "heartbeat-after-answer",
        )
    finally:
        release_keepalive_send.set()
        await asyncio.wait_for(handler, timeout=1.0)


@pytest.mark.asyncio
async def test_pending_interaction_blocks_heartbeat_until_matching_resume(
    monkeypatch,
):
    question_id = "call_ask_user"

    class FakeAgent:
        async def process_message_stream(self, request):
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.ask_user_question",
                    "request_id": question_id,
                },
                is_complete=True,
            )

    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    admission = SessionRunAdmission()
    server._agent_manager = None
    server._heartbeat_runtime = SimpleNamespace(admission=admission)
    server._session_stream_tasks = {}
    server._should_trigger_before_chat_request_hook = lambda request: False
    server._is_stateless_method_request = lambda request: True
    server._is_readonly_goal_get_request = lambda request: False
    server._try_start_heartbeat_runtime = AsyncMock()
    server._check_post_process_plan_exit = AsyncMock()
    server._get_stateless_agent = AsyncMock(return_value=FakeAgent())
    monkeypatch.setattr(
        agent_ws_server,
        "send_wire_payload",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep."
        "ensure_persistent_checkpointer",
        AsyncMock(),
    )
    request = AgentRequest(
        request_id="ask-user-stream",
        channel_id="web",
        session_id="ask-user-session",
        req_method=ReqMethod.CHAT_SEND,
        params={},
        is_stream=True,
    )

    await server._handle_stream(FakeWebSocket(), request, asyncio.Lock())

    assert admission.has_pending_interaction("ask-user-session") is True
    assert (
        await admission.try_begin_heartbeat(
            "ask-user-session",
            "heartbeat-before-answer",
        )
        is False
    )

    server._handle_stream_impl = AsyncMock()
    answer = AgentRequest(
        request_id="answer-dispatch",
        channel_id="web",
        session_id="ask-user-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": question_id,
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
        },
        is_stream=True,
    )
    await server._handle_stream(FakeWebSocket(), answer, asyncio.Lock())

    assert admission.has_pending_interaction("ask-user-session") is False
    assert (
        await admission.try_begin_heartbeat(
            "ask-user-session",
            "heartbeat-after-answer",
        )
        is True
    )
    await admission.end_heartbeat(
        "ask-user-session",
        "heartbeat-after-answer",
    )


@pytest.mark.asyncio
async def test_unmatched_interrupt_resume_keeps_heartbeat_blocked():
    admission = SessionRunAdmission()
    await admission.mark_interaction_pending("session-1", "current-question")

    await admission.begin_user("session-1")
    await admission.clear_interaction_pending("session-1", "stale-question")
    await admission.end_user("session-1")

    assert admission.has_pending_interaction("session-1") is True
    assert await admission.try_begin_heartbeat("session-1", "heartbeat-1") is False


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


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["_handle_unary", "_handle_stream"])
async def test_interrupt_resume_skips_existing_user_admission(handler_name):
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    admission = SimpleNamespace(
        is_user_active=lambda session_id: True,
        begin_user=AsyncMock(),
        end_user=AsyncMock(),
        clear_interaction_pending=AsyncMock(),
    )
    server._agent_manager = None
    server._heartbeat_runtime = SimpleNamespace(admission=admission)
    server._should_trigger_before_chat_request_hook = lambda request: False
    server._handle_unary_impl = AsyncMock()
    server._handle_stream_impl = AsyncMock()
    request = AgentRequest(
        request_id="answer-dispatch",
        channel_id="web",
        session_id="ask-user-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_ask_user",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
        },
        is_stream=handler_name == "_handle_stream",
    )

    await getattr(server, handler_name)(FakeWebSocket(), request, asyncio.Lock())

    admission.begin_user.assert_not_awaited()
    admission.end_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["_handle_unary", "_handle_stream"])
async def test_stale_interrupt_resume_retains_normal_admission(handler_name):
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    admission = SimpleNamespace(
        is_user_active=lambda session_id: False,
        begin_user=AsyncMock(),
        end_user=AsyncMock(),
        clear_interaction_pending=AsyncMock(),
    )
    server._agent_manager = None
    server._heartbeat_runtime = SimpleNamespace(admission=admission)
    server._should_trigger_before_chat_request_hook = lambda request: False
    server._handle_unary_impl = AsyncMock()
    server._handle_stream_impl = AsyncMock()
    request = AgentRequest(
        request_id="stale-answer-dispatch",
        channel_id="web",
        session_id="stale-answer-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "",
            "request_id": "call_stale_ask_user",
            "answers": [{"question": "choose", "selected_options": ["A"]}],
            "source": "ask_user_interrupt",
        },
        is_stream=handler_name == "_handle_stream",
    )

    await getattr(server, handler_name)(FakeWebSocket(), request, asyncio.Lock())

    admission.begin_user.assert_awaited_once_with("stale-answer-session")
    admission.end_user.assert_awaited_once_with("stale-answer-session")


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
