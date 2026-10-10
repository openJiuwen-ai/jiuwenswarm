import asyncio
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedError

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    parse_agent_server_wire_chunk,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.ws_limits import AGENT_WS_MAX_MESSAGE_BYTES
from jiuwenswarm.gateway.routing import agent_client
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent_payloads.append(data)


class ClosingSendWebSocket:
    async def send(self, data: str) -> None:
        raise ConnectionClosedError(None, None)


class ClosingRecvWebSocket:
    def __init__(self) -> None:
        self.recv_calls = 0

    async def recv(self) -> str:
        self.recv_calls += 1
        raise ConnectionClosedError(None, None)


class AckThenHangWebSocket:
    """首次 recv 返回 connection.ack 事件帧，之后挂起（模拟 ack 迟到但连接保持）。"""

    def __init__(self) -> None:
        self.recv_calls = 0

    async def recv(self) -> str:
        self.recv_calls += 1
        if self.recv_calls == 1:
            return json.dumps({"type": "event", "event": "connection.ack", "params": {}})
        await asyncio.Event().wait()  # 挂起，模拟连接保持


class PushFramesWebSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.read_both = asyncio.Event()
        self.read_count = 0

    async def recv(self) -> str:
        frame = await self.frames.get()
        self.read_count += 1
        if self.read_count == 2:
            self.read_both.set()
        return frame

    async def close(self) -> None:
        pass


class AgentClientHarness(WebSocketAgentServerClient):
    def set_ws_for_test(self, ws) -> None:
        self._ws = ws

    def set_uri_for_test(self, uri: str) -> None:
        self._uri = uri

    def set_running_for_test(self, running: bool) -> None:
        self._running = running

    def set_server_ready_for_test(self, ready: bool) -> None:
        self._server_ready = ready

    def get_server_ready_for_test(self) -> bool:
        return self._server_ready

    def is_running_for_test(self) -> bool:
        return self._running

    def get_ws_for_test(self):
        return self._ws

    def has_message_queue_for_test(self, request_id: str) -> bool:
        return request_id in self._message_queues

    def get_message_queue_for_test(self, request_id: str):
        return self._message_queues[request_id]

    def set_message_queue_for_test(self, request_id: str, queue) -> None:
        self._message_queues[request_id] = queue

    async def run_message_receiver_loop_for_test(self) -> None:
        await self._message_receiver_loop()

    async def stop_receiver_after_fatal_error_for_test(self, exc: BaseException) -> None:
        await self._stop_receiver_after_fatal_error(exc)


class ReconnectingAgentClientHarness(AgentClientHarness):
    def __init__(self) -> None:
        super().__init__()
        self.connect_calls: list[str] = []
        self.reconnected_ws = FakeWebSocket()

    async def connect(self, uri: str) -> None:
        self.connect_calls.append(uri)
        self._uri = uri
        self._ws = self.reconnected_ws
        self._server_ready = True


def test_agent_client_uses_shared_websocket_limit():
    assert not hasattr(agent_client, "_WS_MAX_SIZE")
    assert agent_client.AGENT_WS_MAX_MESSAGE_BYTES == AGENT_WS_MAX_MESSAGE_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_request_id", "final_is_complete", "second_event_type"),
    [
        ("cross-session-turn", False, "chat.final"),
        ("interaction-1", False, "chat.final"),
        ("cross-session-turn", True, "chat.final"),
        ("interaction-1", True, "chat.final"),
        ("interaction-1", True, "chat.error"),
    ],
)
async def test_server_push_frames_keep_wire_order_when_first_handler_waits(
    second_request_id, final_is_complete, second_event_type
):
    client = AgentClientHarness()
    ws = PushFramesWebSocket()
    client.set_ws_for_test(ws)
    client.set_running_for_test(True)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    both_handled = asyncio.Event()
    unrelated_push_handled = asyncio.Event()
    handled: list[int] = []
    request_queue: asyncio.Queue[dict] = asyncio.Queue()
    client.set_message_queue_for_test("foreground-turn", request_queue)
    visible_text = ""

    async def on_push(frame: dict) -> None:
        nonlocal visible_text
        index = frame["index"]
        if index == 2:
            unrelated_push_handled.set()
            return
        if index == 0:
            first_started.set()
            await release_first.wait()
        payload = parse_agent_server_wire_chunk(frame).payload
        assert payload["turn_request_id"] == "cross-session-turn"
        assert payload["cross_session"]["message_id"] == "sm-1"
        if payload["event_type"] == "chat.delta":
            visible_text += payload["content"]
        else:
            visible_text = payload["content"]
        handled.append(index)
        if len(handled) == 2:
            both_handled.set()

    client.set_server_push_handler(on_push)
    for index in (0, 1):
        request_id = "cross-session-turn" if index == 0 else second_request_id
        wire = encode_agent_chunk_for_wire(
            AgentResponseChunk(
                request_id=request_id,
                channel_id="web",
                payload={
                    "event_type": "chat.delta" if index == 0 else second_event_type,
                    "turn_request_id": "cross-session-turn",
                    "message_origin": "cross_session_agent",
                    "session_message_id": "sm-1",
                    "cross_session": {"message_id": "sm-1", "source_session_id": "source-1"},
                    "content": "杭州今日天气",
                },
                is_complete=index == 1 and final_is_complete,
            ),
            response_id=request_id,
            sequence=index,
        )
        wire["index"] = index
        wire["metadata"][E2A_WIRE_SERVER_PUSH_KEY] = True
        ws.frames.put_nowait(json.dumps(wire))
    ws.frames.put_nowait(json.dumps({
        "index": 2,
        "request_id": "unrelated-push",
        "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
    }))
    ws.frames.put_nowait(json.dumps({"request_id": "foreground-turn"}))

    receiver = asyncio.create_task(client.run_message_receiver_loop_for_test())
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(ws.read_both.wait(), 1)
        await asyncio.wait_for(unrelated_push_handled.wait(), 1)
        assert (await asyncio.wait_for(request_queue.get(), 1))["request_id"] == "foreground-turn"
        await asyncio.sleep(0)
        assert handled == []
        release_first.set()
        await asyncio.wait_for(both_handled.wait(), 1)
        assert handled == [0, 1]
        assert visible_text == "杭州今日天气"
    finally:
        release_first.set()
        receiver.cancel()
        await receiver
        await client.disconnect()


@pytest.mark.asyncio
async def test_send_request_stream_keeps_tail_window_for_processing_status(monkeypatch):
    client = AgentClientHarness()
    client.set_ws_for_test(FakeWebSocket())

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_client._STREAM_TRAILING_MESSAGE_GRACE_SECONDS",
        0.05,
    )

    env = e2a_from_agent_fields(
        request_id="rid-tail",
        channel_id="acp",
        session_id="sess-tail",
        params={"content": "hello"},
        is_stream=True,
    )

    async def inject_frames():
        while not client.has_message_queue_for_test("rid-tail"):
            await asyncio.sleep(0.001)
        queue = client.get_message_queue_for_test("rid-tail")
        await queue.put(
            encode_agent_chunk_for_wire(
                AgentResponseChunk(
                    request_id="rid-tail",
                    channel_id="acp",
                    payload={"content": "partial", "event_type": "chat.delta"},
                    is_complete=False,
                ),
                response_id="rid-tail",
                sequence=0,
            )
        )
        await queue.put(
            encode_agent_chunk_for_wire(
                AgentResponseChunk(
                    request_id="rid-tail",
                    channel_id="acp",
                    payload={"is_complete": True},
                    is_complete=True,
                ),
                response_id="rid-tail",
                sequence=1,
            )
        )
        await asyncio.sleep(0.01)
        await queue.put(
            encode_agent_chunk_for_wire(
                AgentResponseChunk(
                    request_id="rid-tail",
                    channel_id="acp",
                    payload={"event_type": "chat.processing_status", "is_processing": False},
                    is_complete=False,
                ),
                response_id="rid-tail",
                sequence=2,
            )
        )

    injector = asyncio.create_task(inject_frames())
    chunks = []
    async for chunk in client.send_request_stream(env):
        chunks.append(chunk)
    await injector

    assert [chunk.payload for chunk in chunks] == [
        {"content": "partial", "event_type": "chat.delta"},
        {"is_complete": True},
        {"event_type": "chat.processing_status", "is_processing": False},
    ]
    assert client.has_message_queue_for_test("rid-tail") is False


@pytest.mark.asyncio
async def test_send_request_stream_absorbs_duplicate_complete_frames(monkeypatch):
    client = AgentClientHarness()
    client.set_ws_for_test(FakeWebSocket())

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.agent_client._STREAM_TRAILING_MESSAGE_GRACE_SECONDS",
        0.05,
    )

    env = e2a_from_agent_fields(
        request_id="rid-complete",
        channel_id="acp",
        session_id="sess-complete",
        params={"content": "hello"},
        is_stream=True,
    )

    async def inject_frames():
        while not client.has_message_queue_for_test("rid-complete"):
            await asyncio.sleep(0.001)
        queue = client.get_message_queue_for_test("rid-complete")
        for seq in (0, 1):
            await queue.put(
                encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id="rid-complete",
                        channel_id="acp",
                        payload={"is_complete": True},
                        is_complete=True,
                    ),
                    response_id="rid-complete",
                    sequence=seq,
                )
            )

    injector = asyncio.create_task(inject_frames())
    chunks = []
    async for chunk in client.send_request_stream(env):
        chunks.append(chunk)
    await injector

    assert len(chunks) == 2
    assert all(chunk.is_complete for chunk in chunks)
    assert client.has_message_queue_for_test("rid-complete") is False


@pytest.mark.asyncio
async def test_message_receiver_loop_sets_ready_on_late_connection_ack():
    """迟到的 connection.ack（connect 5s 首帧等待超时后才到达）补置 server_ready。

    回归：e2a_proxy 等依赖 server_ready 的入口（Web session.list）在
    ack 迟到时不得永久返回 SERVICE_UNAVAILABLE。
    """
    client = AgentClientHarness()
    ws = AckThenHangWebSocket()
    client.set_ws_for_test(ws)
    client.set_running_for_test(True)
    client.set_server_ready_for_test(False)  # connect 首帧等待超时后的状态

    task = asyncio.create_task(client.run_message_receiver_loop_for_test())
    for _ in range(100):
        if client.get_server_ready_for_test():
            break
        await asyncio.sleep(0.001)

    assert client.get_server_ready_for_test() is True
    assert client.agent_ready is False
    assert ws.recv_calls >= 1  # ack 帧已被消费，连接保持（第二轮 recv 挂起中）

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_connection_ack_warming_is_not_agent_ready():
    client = AgentClientHarness()
    client._apply_connection_ack(
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "RUNTIME_WARMING"},
        }
    )
    assert client.server_ready is True
    assert client.agent_ready is False


def test_connection_ack_failed_keeps_server_ready_not_agent_ready():
    client = AgentClientHarness()
    client._apply_connection_ack(
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {"status": "failed", "readiness": "FAILED"},
        }
    )
    assert client.server_ready is True
    assert client.agent_ready is False


def test_connection_ack_agent_ready_sets_execution_flag():
    client = AgentClientHarness()
    client._apply_connection_ack(
        {
            "type": "event",
            "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "AGENT_READY"},
        }
    )
    assert client.server_ready is True
    assert client.agent_ready is True


@pytest.mark.asyncio
async def test_message_receiver_loop_stops_on_closed_websocket():
    client = AgentClientHarness()
    ws = ClosingRecvWebSocket()
    client.set_ws_for_test(ws)
    client.set_running_for_test(True)

    await asyncio.wait_for(client.run_message_receiver_loop_for_test(), timeout=0.1)

    assert client.is_running_for_test() is False
    assert ws.recv_calls == 1


@pytest.mark.asyncio
async def test_message_receiver_loop_logs_close_diagnostics(caplog):
    target_logger = logging.getLogger("jiuwenswarm.gateway.routing.agent_client")
    target_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=target_logger.name)

    client = AgentClientHarness()
    ws = ClosingRecvWebSocket()
    client.set_ws_for_test(ws)
    client.set_running_for_test(True)
    client.set_server_ready_for_test(True)
    client.set_message_queue_for_test("rid-pending", asyncio.Queue())

    try:
        await asyncio.wait_for(client.run_message_receiver_loop_for_test(), timeout=0.1)
    finally:
        target_logger.removeHandler(caplog.handler)

    assert "AgentServer WebSocket 已关闭" in caplog.text
    assert "exc_type='ConnectionClosedError'" in caplog.text
    assert "message='no close frame received or sent'" in caplog.text
    assert "close_code=1006" in caplog.text
    assert "pending_requests=1" in caplog.text
    assert "server_ready=True" in caplog.text


@pytest.mark.asyncio
async def test_send_request_fails_pending_request_when_receiver_stops():
    client = AgentClientHarness()
    ws = FakeWebSocket()
    client.set_ws_for_test(ws)

    env = e2a_from_agent_fields(
        request_id="rid-fatal-close",
        channel_id="acp",
        session_id="sess-fatal-close",
        params={"content": "hello"},
        is_stream=False,
    )

    task = asyncio.create_task(client.send_request(env))
    for _ in range(100):
        if ws.sent_payloads:
            break
        await asyncio.sleep(0.001)
    assert ws.sent_payloads

    await client.stop_receiver_after_fatal_error_for_test(ConnectionClosedError(None, None))

    with pytest.raises(RuntimeError, match="AgentServer WebSocket connection closed"):
        await asyncio.wait_for(task, timeout=0.1)
    assert client.has_message_queue_for_test("rid-fatal-close") is False


@pytest.mark.asyncio
async def test_send_request_reconnects_before_new_request_after_disconnect():
    client = ReconnectingAgentClientHarness()
    client.set_uri_for_test("ws://agent-server")
    client.set_ws_for_test(None)

    env = e2a_from_agent_fields(
        request_id="rid-reconnect",
        channel_id="acp",
        session_id="sess-reconnect",
        params={"content": "hello"},
        is_stream=False,
    )

    task = asyncio.create_task(client.send_request(env))
    for _ in range(100):
        if client.reconnected_ws.sent_payloads:
            break
        await asyncio.sleep(0.001)
    assert client.connect_calls == ["ws://agent-server"]
    assert client.reconnected_ws.sent_payloads

    queue = client.get_message_queue_for_test("rid-reconnect")
    await queue.put(
        encode_agent_response_for_wire(
            AgentResponse(
                request_id="rid-reconnect",
                channel_id="acp",
                ok=True,
                payload={"status": "reconnected"},
            ),
            response_id="rid-reconnect",
        )
    )

    response = await asyncio.wait_for(task, timeout=0.1)

    assert response.ok is True
    assert response.payload == {"status": "reconnected"}
    assert client.has_message_queue_for_test("rid-reconnect") is False


@pytest.mark.asyncio
async def test_send_request_clears_connection_when_send_fails():
    client = ReconnectingAgentClientHarness()
    client.set_uri_for_test("ws://agent-server")
    client.set_ws_for_test(ClosingSendWebSocket())
    client.set_running_for_test(True)
    client.set_server_ready_for_test(True)

    failed_env = e2a_from_agent_fields(
        request_id="rid-send-close",
        channel_id="acp",
        session_id="sess-send-close",
        params={"content": "hello"},
        is_stream=False,
    )

    with pytest.raises(RuntimeError, match="AgentServer WebSocket connection closed"):
        await client.send_request(failed_env)

    assert client.get_ws_for_test() is None
    assert client.is_running_for_test() is False
    assert client.has_message_queue_for_test("rid-send-close") is False

    reconnect_env = e2a_from_agent_fields(
        request_id="rid-after-send-close",
        channel_id="acp",
        session_id="sess-send-close",
        params={"content": "again"},
        is_stream=False,
    )

    task = asyncio.create_task(client.send_request(reconnect_env))
    for _ in range(100):
        if client.reconnected_ws.sent_payloads:
            break
        await asyncio.sleep(0.001)
    assert client.connect_calls == ["ws://agent-server"]
    assert client.reconnected_ws.sent_payloads

    queue = client.get_message_queue_for_test("rid-after-send-close")
    await queue.put(
        encode_agent_response_for_wire(
            AgentResponse(
                request_id="rid-after-send-close",
                channel_id="acp",
                ok=True,
                payload={"status": "reconnected"},
            ),
            response_id="rid-after-send-close",
        )
    )

    response = await asyncio.wait_for(task, timeout=0.1)
    assert response.ok is True
    assert client.has_message_queue_for_test("rid-after-send-close") is False
