import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenclaw.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenclaw.gateway.agent_client import (
    AgentServerConnectionError,
    WebSocketAgentServerClient,
)
from jiuwenclaw.schema.agent import AgentResponseChunk


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent_payloads.append(data)

    async def close(self) -> None:
        self.closed = True


class AgentClientHarness(WebSocketAgentServerClient):
    def set_ws_for_test(self, ws) -> None:
        self._ws = ws
        self._server_ready = True

    def has_message_queue_for_test(self, request_id: str) -> bool:
        return request_id in self._message_queues

    def get_message_queue_for_test(self, request_id: str):
        return self._message_queues[request_id]


@pytest.mark.asyncio
async def test_send_request_stream_keeps_tail_window_for_processing_status(monkeypatch):
    client = AgentClientHarness()
    client.set_ws_for_test(FakeWebSocket())

    monkeypatch.setattr(
        "jiuwenclaw.gateway.agent_client._STREAM_TRAILING_MESSAGE_GRACE_SECONDS",
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
        "jiuwenclaw.gateway.agent_client._STREAM_TRAILING_MESSAGE_GRACE_SECONDS",
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


class ScriptedWebSocket(FakeWebSocket):
    def __init__(self, frames) -> None:
        super().__init__()
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)

    async def recv(self):
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame


class ReconnectingAgentClient(AgentClientHarness):
    def __init__(self, sockets) -> None:
        super().__init__()
        self.sockets = list(sockets)
        self.open_attempts = 0

    async def _open_websocket(self, uri: str):
        self.open_attempts += 1
        item = self.sockets.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ack_frame() -> str:
    return json.dumps({"type": "event", "event": "connection.ack"})


def _closed_error() -> ConnectionClosedError:
    close = Close(1011, "keepalive ping timeout")
    return ConnectionClosedError(close, close, True)


@pytest.mark.asyncio
async def test_connection_close_fails_pending_request_and_reconnects_after_ack(monkeypatch):
    first_ws = ScriptedWebSocket([_ack_frame()])
    second_ws = ScriptedWebSocket([_ack_frame()])
    client = ReconnectingAgentClient([first_ws, RuntimeError("still down"), second_ws])
    monkeypatch.setattr(
        "jiuwenclaw.gateway.agent_client._RECONNECT_INITIAL_DELAY_SECONDS", 0.01
    )
    monkeypatch.setattr(
        "jiuwenclaw.gateway.agent_client._RECONNECT_MAX_DELAY_SECONDS", 0.02
    )

    await client.connect("ws://agent-server")
    env = e2a_from_agent_fields(
        request_id="rid-disconnect",
        channel_id="vibeskill",
        session_id="session-1",
        params={"task_id": "session-1"},
        is_stream=False,
    )
    request_task = asyncio.create_task(client.send_request(env))
    while not client.has_message_queue_for_test("rid-disconnect"):
        await asyncio.sleep(0)

    await first_ws.frames.put(_closed_error())
    with pytest.raises(AgentServerConnectionError, match="keepalive ping timeout"):
        await asyncio.wait_for(request_task, timeout=0.2)

    assert client.server_ready is False
    with pytest.raises(AgentServerConnectionError):
        client._ensure_connected()

    for _ in range(100):
        if client.server_ready:
            break
        await asyncio.sleep(0.005)
    assert client.server_ready is True
    assert client._ws is second_ws
    assert client.open_attempts == 3
    await client.disconnect()


@pytest.mark.asyncio
async def test_connect_rejects_missing_connection_ack() -> None:
    ws = ScriptedWebSocket([json.dumps({"type": "event", "event": "other"})])
    client = ReconnectingAgentClient([ws])

    with pytest.raises(AgentServerConnectionError, match="connection.ack"):
        await client.connect("ws://agent-server")

    assert client.server_ready is False
    assert client._ws is None
    assert ws.closed is True
