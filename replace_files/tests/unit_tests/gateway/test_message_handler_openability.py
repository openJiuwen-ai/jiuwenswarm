from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.gateway.message_handler import MessageHandler


class FakeAgentClient:
    def __init__(self) -> None:
        self.connection_lost_handler = None
        self.reconnected_handler = None
        self.reconnect_failed_handler = None
        self.sent: list[E2AEnvelope] = []

    def set_server_push_handler(self, handler) -> None:
        return None

    def set_openability_connection_lost_handler(self, handler) -> None:
        self.connection_lost_handler = handler

    def set_openability_reconnected_handler(self, handler) -> None:
        self.reconnected_handler = handler

    def set_openability_reconnect_failed_handler(self, handler) -> None:
        self.reconnect_failed_handler = handler

    async def send_request(self, envelope: E2AEnvelope) -> Any:
        self.sent.append(envelope)
        return MagicMock(ok=True, request_id=envelope.request_id)


@pytest.fixture(autouse=True)
def reset_message_handler_singleton() -> None:
    MessageHandler._instance = None
    yield
    MessageHandler._instance = None


@pytest.mark.asyncio
async def test_openability_connection_lost_publishes_vibeskill_error() -> None:
    client = FakeAgentClient()
    handler = MessageHandler(client)
    task = asyncio.create_task(asyncio.sleep(60))
    handler._stream_tasks["rid-1"] = task
    handler._stream_sessions["rid-1"] = "sid-a"
    handler._stream_channels["rid-1"] = "vibeskill"
    handler._stream_user_ids["rid-1"] = "user-a"

    assert client.connection_lost_handler is not None
    await client.connection_lost_handler({"session_ids": ["sid-a"]})

    msg = await handler.consume_robot_messages(timeout=0.1)
    assert msg is not None
    assert msg.channel_id == "vibeskill"
    assert msg.session_id == "sid-a"
    assert msg.payload["event_type"] == "skilldev.error"
    assert msg.payload["error"] == "后端网络连接中断，请重试请求"
    assert task.cancelled()
    assert handler._stream_sessions == {}
    assert handler._openability_disconnect_pending_cancel == {"sid-a": "user-a"}


@pytest.mark.asyncio
async def test_openability_reconnected_sends_skilldev_cancel_without_cancel_final() -> None:
    client = FakeAgentClient()
    handler = MessageHandler(client)
    task = asyncio.create_task(asyncio.sleep(60))
    handler._stream_tasks["rid-1"] = task
    handler._stream_sessions["rid-1"] = "sid-a"
    handler._stream_channels["rid-1"] = "vibeskill"
    handler._stream_user_ids["rid-1"] = "user-a"

    assert client.reconnected_handler is not None
    await client.reconnected_handler({"session_ids": ["sid-a"], "user_id": "user-a"})

    assert task.cancelled()
    assert client.sent
    cancel_env = client.sent[-1]
    assert cancel_env.method == "skilldev.cancel"
    assert cancel_env.session_id == "sid-a"
    assert cancel_env.user_id == "user-a"
    assert cancel_env.params["intent"] == "cancel"
    assert cancel_env.params["task_id"] == "sid-a"
    assert cancel_env.params["service_id"] == "sid-a"
    assert cancel_env.service_id == "sid-a"
    assert await handler.consume_robot_messages(timeout=0) is None


@pytest.mark.asyncio
async def test_openability_reconnected_cancels_agent_after_disconnect_closed_stream() -> None:
    client = FakeAgentClient()
    handler = MessageHandler(client)
    task = asyncio.create_task(asyncio.sleep(60))
    handler._stream_tasks["rid-1"] = task
    handler._stream_sessions["rid-1"] = "sid-a"
    handler._stream_channels["rid-1"] = "vibeskill"
    handler._stream_user_ids["rid-1"] = "user-a"

    assert client.connection_lost_handler is not None
    await client.connection_lost_handler({"session_ids": ["sid-a"]})
    assert task.cancelled()
    assert await handler.consume_robot_messages(timeout=0.1) is not None

    assert client.reconnected_handler is not None
    await client.reconnected_handler({"session_ids": ["sid-a"], "user_id": "user-a"})

    assert client.sent
    cancel_env = client.sent[-1]
    assert cancel_env.method == "skilldev.cancel"
    assert cancel_env.session_id == "sid-a"
    assert handler._openability_disconnect_pending_cancel == {}
    assert await handler.consume_robot_messages(timeout=0) is None


@pytest.mark.asyncio
async def test_openability_reconnect_failed_clears_pending_cancel() -> None:
    client = FakeAgentClient()
    handler = MessageHandler(client)
    handler._openability_disconnect_pending_cancel["sid-a"] = "user-a"
    handler._openability_disconnect_pending_cancel["sid-b"] = "user-b"

    assert client.reconnect_failed_handler is not None
    await client.reconnect_failed_handler({"session_ids": ["sid-a"]})

    assert handler._openability_disconnect_pending_cancel == {"sid-b": "user-b"}


@pytest.mark.asyncio
async def test_explicit_cancel_clears_pending_cancel() -> None:
    from jiuwenclaw.schema.message import Message, ReqMethod

    client = FakeAgentClient()
    handler = MessageHandler(client)
    handler._openability_disconnect_pending_cancel["sid-a"] = "user-a"
    msg = Message(
        id="cancel-1",
        type="req",
        channel_id="vibeskill",
        session_id="sid-a",
        params={"session_id": "sid-a", "task_id": "sid-a"},
        timestamp=0,
        ok=True,
        req_method=ReqMethod.SKILLDEV_CANCEL,
    )

    await handler._cancel_agent_work_for_session(msg, "sid-a")

    assert handler._openability_disconnect_pending_cancel == {}
