import ast
import asyncio
import json
from pathlib import Path

import pytest

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
    class FakeAgent:
        async def process_message_stream(self, request):
            for index in range(2):
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"content": str(index)},
                    is_complete=False,
                )

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
    assert foreground_manager.events == ["begin", "end"]


@pytest.mark.asyncio
async def test_desktop_sixth_parallel_session_is_rejected_before_agent_start(
    monkeypatch,
):
    blocking_session_ids = {
        "desktop-a",
        "desktop-b",
        "desktop-c",
        "desktop-d",
        "desktop-e",
    }
    stream_started = {
        session_id: asyncio.Event() for session_id in blocking_session_ids
    }
    release_blocking_streams = asyncio.Event()

    class FakeAgent:
        async def process_message_stream(self, request):
            if request.session_id in blocking_session_ids:
                stream_started[request.session_id].set()
                await release_blocking_streams.wait()

            answer = "正常回答"
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.delta", "content": answer},
            )
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.final", "content": answer},
            )
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class ForegroundManager:
        async def begin_foreground_chat(self):
            return None

        async def end_foreground_chat(self):
            return None

    async def no_persistent_checkpointer():
        return None

    prepared_session_ids = []

    async def prepare_turn(request, channel_id, *, sync_metadata):
        prepared_session_ids.append(request.session_id)
        return "work", None, FakeAgent()

    async def no_plan_restore(request, mode, sub_mode, agent):
        return False

    async def no_plan_exit_check(request, agent):
        return None

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        interface_deep,
        "ensure_persistent_checkpointer",
        no_persistent_checkpointer,
    )
    server = agent_ws_server.AgentWebSocketServer.__new__(
        agent_ws_server.AgentWebSocketServer
    )
    server._agent_manager = ForegroundManager()
    server._session_stream_tasks = {}
    server._is_stateless_method_request = lambda request: False
    server._is_readonly_goal_get_request = lambda request: False
    server._prepare_code_mode_chat_turn = prepare_turn
    server._ensure_code_mode_state = no_plan_restore
    server._check_post_process_plan_exit = no_plan_exit_check

    def desktop_request(request_id, session_id):
        return AgentRequest(
            request_id=request_id,
            channel_id="desktop",
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "hello"},
            is_stream=True,
        )

    blocking_streams = []
    for session_id in sorted(blocking_session_ids):
        ws = FakeWebSocket()
        task = asyncio.create_task(
            server._handle_stream(
                ws,
                desktop_request(f"{session_id}-first", session_id),
                asyncio.Lock(),
            )
        )
        blocking_streams.append((ws, task))
        await stream_started[session_id].wait()

    rejected_ws = FakeWebSocket()
    try:
        await server._handle_stream(
            rejected_ws,
            desktop_request("desktop-f-first", "desktop-f"),
            asyncio.Lock(),
        )
    finally:
        release_blocking_streams.set()
        await asyncio.gather(*(task for _, task in blocking_streams))

    warning = (
        "当前检测到多个会话正在并行处理，可能会导致所有任务的响应变慢或机器性能下降，"
        "请等待其他会话结束后再发起新会话。"
    )
    rejected_frames = [json.loads(payload) for payload in rejected_ws.sent]

    assert prepared_session_ids == [
        "desktop-a",
        "desktop-b",
        "desktop-c",
        "desktop-d",
        "desktop-e",
    ]
    assert len(rejected_frames) == 1
    assert rejected_frames[0]["is_final"] is True
    assert rejected_frames[0]["body"]["result"] == {
        "event_type": "chat.final",
        "content": warning,
    }


def test_agent_ws_server_has_no_direct_websocket_send_calls():
    path = Path(agent_ws_server.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    direct_sends = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send"
    ]

    assert direct_sends == []
