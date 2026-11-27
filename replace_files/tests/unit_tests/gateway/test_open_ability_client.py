import asyncio
import logging
import pytest
from unittest.mock import AsyncMock

from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.e2a.link_heartbeat import build_link_heartbeat_wire
from jiuwenclaw.gateway.open_ability_client import OpenAbilityWebSocketClient


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class FailingWebSocket:
    def __init__(self) -> None:
        self.close_called = False

    async def recv(self):
        raise RuntimeError("boom")

    async def close(self) -> None:
        self.close_called = True


@pytest.mark.asyncio
async def test_receiver_loop_runtime_error_does_not_trigger_reconnect() -> None:
    client = OpenAbilityWebSocketClient("sb-1")
    ws = FailingWebSocket()
    connection_lost_handler = AsyncMock()

    client._ws = ws
    client._uri = "ws://127.0.0.1:9001/ws"
    client._running = True
    client._server_ready = True
    client.set_connection_lost_handler(connection_lost_handler)

    await client._message_receiver_loop()

    connection_lost_handler.assert_not_awaited()
    assert ws.close_called is True
    assert client._ws is None
    assert client._uri is None
    assert client.server_ready is False


@pytest.mark.asyncio
async def test_receiver_loop_dispatches_link_heartbeat_without_server_push() -> None:
    wire = build_link_heartbeat_wire(sandbox_id="sb-hb")
    link_handler = AsyncMock()
    client = OpenAbilityWebSocketClient("sb-hb")
    client.set_link_heartbeat_handler(link_handler)

    assert client._dispatch_link_heartbeat(wire) is True
    await asyncio.sleep(0)
    link_handler.assert_awaited_once_with(wire)


@pytest.mark.asyncio
async def test_stream_request_logs_out_in_with_identity_fields() -> None:
    client = OpenAbilityWebSocketClient("sb-stream")
    client._ws = RecordingWebSocket()
    envelope = E2AEnvelope(
        request_id="req-stream",
        channel="vibeskill",
        session_id="sess-stream",
        method="skilldev.chat",
        params={"task_id": "sess-stream"},
        is_stream=False,
    )

    async def feed_final_chunk() -> None:
        while "req-stream" not in client._message_queues:
            await asyncio.sleep(0)
        await client._message_queues["req-stream"].put(
            {
                "request_id": "req-stream",
                "channel_id": "vibeskill",
                "payload": {"ok": True},
                "is_complete": True,
            }
        )

    feeder = asyncio.create_task(feed_final_chunk())
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target_logger = logging.getLogger("jiuwenclaw.gateway.open_ability_client")
    handler = ListHandler()
    old_level = target_logger.level
    target_logger.addHandler(handler)
    target_logger.setLevel(logging.INFO)
    try:
        chunks = [chunk async for chunk in client.send_request_stream(envelope)]
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(old_level)
    await feeder

    assert len(chunks) == 1
    messages = [record.getMessage() for record in records]
    assert any(
        "[E2A][oa][stream][out] sandbox_id=sb-stream session_id=sess-stream "
        "request_id=req-stream method=skilldev.chat" in message
        for message in messages
    )
