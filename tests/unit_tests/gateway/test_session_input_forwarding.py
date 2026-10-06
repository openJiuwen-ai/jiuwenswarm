# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the running Gateway queue, not just the mode predicates."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


def message(rid, *, stream=False, mode=None, channel="web", **params):
    return Message(
        id=rid, type="req", channel_id=channel, session_id="sess-input-gateway",
        req_method=ReqMethod.CHAT_SEND, is_stream=stream, timestamp=0, ok=True,
        params={"query": rid, "mode": "agent", **({"input_mode": mode} if mode else {}), **params},
        metadata={"ws_id": "original-connection", "user_id": "test-user"},
    )


class GatedClient:
    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Queue()
        self.calls = []
        self.cancelled = []
        self.reject_input = False

    async def execute(self, env):
        self.calls.append(env)
        await self.started.put(env.request_id)
        if env.request_id == "original":
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(env.request_id)
                raise
        if env.params.get("input_mode"):
            if self.reject_input:
                raise ValueError("supplement rejected")
            return {"event_type": "runtime.accepted", "request_id": env.request_id}
        return {"event_type": "chat.final", "content": env.request_id}

    async def send_request(self, env):
        return AgentResponse(
            request_id=env.request_id, channel_id=env.channel, payload=await self.execute(env),
        )

    async def send_request_stream(self, env):
        yield AgentResponseChunk(
            request_id=env.request_id, channel_id=env.channel, payload=await self.execute(env),
        )
        yield AgentResponseChunk(
            request_id=env.request_id, channel_id=env.channel, payload=None, is_complete=True,
        )


@pytest.fixture
async def gateway(monkeypatch):
    monkeypatch.setattr(MessageHandler, "_instance", None)
    client = GatedClient()
    handler = MessageHandler(client)
    handler._sync_agentos_cron_jobs = AsyncMock()
    handler._broadcast_task_global_running = AsyncMock()
    handler._trigger_before_chat_request_hook = AsyncMock()
    await handler.start_forwarding()
    yield handler, client
    client.release.set()
    await handler.stop_forwarding()
    if handler._fire_and_forget_tasks:
        await asyncio.gather(*handler._fire_and_forget_tasks, return_exceptions=True)


async def receive(handler, rid, *, event_type=None, ok=None):
    async with asyncio.timeout(5):
        while True:
            msg = await handler.consume_robot_messages(timeout=None)
            if msg.id != rid:
                continue
            if event_type and (msg.payload or {}).get("event_type") != event_type:
                continue
            if ok is not None and msg.ok != ok:
                continue
            return msg


@pytest.mark.asyncio
@pytest.mark.parametrize("original_stream", [False, True])
@pytest.mark.parametrize("input_stream", [False, True])
async def test_input_reaches_running_request_with_independent_receipt(gateway, original_stream, input_stream):
    handler, client = gateway
    await handler.publish_user_messages(message("original", stream=original_stream))
    assert await asyncio.wait_for(client.started.get(), 5) == "original"
    await handler.publish_user_messages(message("supplement", stream=input_stream, mode="steer"))
    assert await asyncio.wait_for(client.started.get(), 5) == "supplement"
    ack = await receive(handler, "supplement", event_type="runtime.accepted")
    assert ack.metadata["ws_id"] == "original-connection"
    assert ack.session_id == "sess-input-gateway"
    assert not client.release.is_set() and not client.cancelled
    assert handler._session_has_streams_blocking_processing_false("sess-input-gateway")
    client.release.set()
    await receive(handler, "original", event_type="chat.final")


@pytest.mark.asyncio
async def test_normal_unary_order_is_preserved_while_steer_bypasses(gateway):
    handler, client = gateway
    await handler.publish_user_messages(message("original"))
    assert await asyncio.wait_for(client.started.get(), 5) == "original"
    await handler.publish_user_messages(message("second"))
    await handler.publish_user_messages(message("third"))
    await handler.publish_user_messages(message("supplement", mode="steer"))
    assert await asyncio.wait_for(client.started.get(), 5) == "supplement"
    await receive(handler, "supplement", event_type="runtime.accepted")
    assert [e.request_id for e in client.calls] == ["original", "supplement"]
    client.release.set()
    assert await asyncio.wait_for(client.started.get(), 5) == "second"
    assert await asyncio.wait_for(client.started.get(), 5) == "third"
    await receive(handler, "third", event_type="chat.final")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_input_preserves_original_and_forward_loop(gateway, stream):
    handler, client = gateway
    client.reject_input = True
    await handler.publish_user_messages(message("original"))
    await asyncio.wait_for(client.started.get(), 5)
    await handler.publish_user_messages(message("bad", mode="steer", stream=stream))
    error = await receive(handler, "bad", ok=False)
    assert "supplement rejected" in str(error.payload)
    assert "original" in handler._active_chat_tasks and not client.cancelled
    client.reject_input = False
    await handler.publish_user_messages(message("good", mode="steer"))
    await receive(handler, "good", event_type="runtime.accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "cancel"])
async def test_unary_active_and_queued_tasks_are_cleaned_up(gateway, action):
    handler, client = gateway
    await handler.publish_user_messages(message("original"))
    await asyncio.wait_for(client.started.get(), 5)
    await handler.publish_user_messages(message("queued"))
    # A bypassed input proves the queued task has been admitted before cleanup.
    await handler.publish_user_messages(message("barrier", mode="steer"))
    await receive(handler, "barrier", event_type="runtime.accepted")
    if action == "stop":
        await handler.stop_forwarding()
    else:
        cancel = message("cancel")
        cancel.req_method = ReqMethod.CHAT_CANCEL
        cancel.params = {"intent": "cancel"}
        await handler.publish_user_messages(cancel)
        await receive(handler, "cancel", event_type="chat.interrupt_result")
    assert client.cancelled == ["original"]
    assert "queued" not in [e.request_id for e in client.calls]
    assert not handler._non_stream_chat_tasks
    assert not handler._non_stream_chat_locks
    assert not handler._active_chat_tasks


@pytest.mark.asyncio
async def test_explicit_input_is_normalized_and_literal_commands_are_not_executed(gateway):
    handler, client = gateway
    handler._inbound_pipeline = AsyncMock()
    handler._handle_channel_control = AsyncMock(return_value=False)
    msg = message(
        "literal", runtime_mode=" STEER ", query="/new_session", channel="feishu",
    )
    handler.get_or_create_channel_state(msg).session_id = msg.session_id
    await handler.publish_user_messages(msg)
    await receive(handler, "literal", event_type="runtime.accepted")
    env = client.calls[-1]
    assert env.params["input_mode"] == "steer"
    assert "runtime_mode" not in env.params
    assert env.params["query"] == "/new_session"
    handler._handle_channel_control.assert_not_awaited()
    handler._inbound_pipeline.apply.assert_awaited_once()


@pytest.mark.asyncio
async def test_conflicting_modes_do_not_kill_forward_loop(gateway):
    handler, client = gateway
    await handler.publish_user_messages(message("bad", mode="steer", runtime_mode="follow_up"))
    await receive(handler, "bad", ok=False)
    await handler.publish_user_messages(message("good", mode="steer"))
    await receive(handler, "good", event_type="runtime.accepted")
    assert [e.request_id for e in client.calls] == ["good"]


_IM_CHANNELS = (
    "feishu",
    "wecom",
    "dingtalk",
    "slack",
    "discord",
    "telegram",
    "wechat",
    "whatsapp",
    "xiaoyi",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", _IM_CHANNELS)
async def test_im_steer_reaches_the_running_session(gateway, channel):
    handler, client = gateway
    original = message("original", stream=True, channel=channel)
    supplement = message("supplement", stream=True, channel=channel)
    supplement.params.pop("input_mode", None)
    supplement.metadata = dict(supplement.metadata)
    supplement.metadata["runtime_mode"] = "steer"
    handler.get_or_create_channel_state(original).session_id = original.session_id
    handler.get_or_create_channel_state(supplement).session_id = supplement.session_id
    await handler.publish_user_messages(original)
    assert await asyncio.wait_for(client.started.get(), 5) == "original"
    await handler.publish_user_messages(supplement)
    assert await asyncio.wait_for(client.started.get(), 5) == "supplement"
    ack = await receive(handler, "supplement", event_type="runtime.accepted")
    delivered = client.calls[-1]
    assert delivered.params["input_mode"] == "steer"
    assert delivered.params["query"] == "supplement"
    assert delivered.session_id == "sess-input-gateway"
    assert ack.session_id == "sess-input-gateway"
    assert not client.cancelled
    assert handler._session_has_streams_blocking_processing_false("sess-input-gateway")


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", _IM_CHANNELS)
async def test_busy_im_chat_send_steers_without_cancelling(gateway, channel):
    handler, client = gateway
    original = message("original", stream=True, channel=channel)
    follow = message("follow-up", stream=True, channel=channel)
    handler.get_or_create_channel_state(original).session_id = original.session_id
    handler.get_or_create_channel_state(follow).session_id = follow.session_id
    await handler.publish_user_messages(original)
    assert await asyncio.wait_for(client.started.get(), 5) == "original"
    await handler.publish_user_messages(follow)
    assert await asyncio.wait_for(client.started.get(), 5) == "follow-up"
    await receive(handler, "follow-up", event_type="runtime.accepted")
    delivered = client.calls[-1]
    assert delivered.params["input_mode"] == "steer"
    assert delivered.params["query"] == "follow-up"
    assert not client.cancelled
    assert handler._session_has_streams_blocking_processing_false("sess-input-gateway")


@pytest.mark.asyncio
async def test_idle_im_chat_send_stays_ordinary(gateway):
    handler, client = gateway
    await handler.publish_user_messages(message("only", channel="slack"))
    assert await asyncio.wait_for(client.started.get(), 5) == "only"
    assert client.calls[-1].params.get("input_mode") in (None, "")
