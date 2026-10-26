"""Unit tests for the Slack Socket Mode channel."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)
from jiuwenswarm.gateway.routing.keys import SlackDeliveryTarget, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget


def _message(
    *,
    event_type: EventType = EventType.CHAT_FINAL,
    content: str = "response",
    metadata: dict[str, Any] | None = None,
    session_id: str = "slack_T1_C1_1710000000.000100",
    payload: dict[str, Any] | None = None,
) -> Message:
    return Message(
        id="response-1",
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": content} if payload is None else payload,
        event_type=event_type,
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_app_mention_creates_thread_scoped_message_and_deduplicates() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
            reply_in_thread=True,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@B1> summarize this",
        "ts": "1710000000.000100",
    }
    body = {"event_id": "Ev1", "team_id": "T1"}

    await channel._handle_app_mention(event, body)
    await channel._handle_app_mention(event, body)

    assert len(received) == 1
    message = received[0]
    assert message.params == {"content": "summarize this", "query": "summarize this"}
    assert message.session_id == "slack_T1_C1_1710000000.000100"
    assert message.chat_id == "C1"
    assert message.user_id == "U1"
    assert message.metadata == {
        "user_id": "U1",
        "slack_event_id": "Ev1",
        "slack_team_id": "T1",
        "slack_channel_id": "C1",
        "slack_channel_type": "channel",
        "slack_user_id": "U1",
        "slack_message_ts": "1710000000.000100",
        "slack_thread_ts": "1710000000.000100",
    }


@pytest.mark.asyncio
async def test_direct_message_is_not_restricted_by_channel_allowlist() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C-ONLY"],
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "hello",
            "ts": "1710000001.000200",
        },
        {"event_id": "Ev2", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].session_id == "slack_T1_D1_U1"
    assert received[0].metadata["slack_thread_ts"] == ""


@pytest.mark.asyncio
async def test_event_filters_reject_bots_subtypes_users_and_channels() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)
    base_event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "text": "<@B1> hello",
        "ts": "1710000002.000300",
    }

    await channel._handle_app_mention(
        {**base_event, "bot_id": "B1"}, {"event_id": "Ev3", "team_id": "T1"}
    )
    await channel._handle_app_mention(
        {**base_event, "subtype": "message_changed"},
        {"event_id": "Ev4", "team_id": "T1"},
    )
    await channel._handle_app_mention(
        {**base_event, "user": "U2"},
        {"event_id": "Ev5", "team_id": "T1"},
    )
    await channel._handle_app_mention(
        {**base_event, "channel": "C2"},
        {"event_id": "Ev6", "team_id": "T1"},
    )

    assert received == []


class _FakeSlackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_send_uses_routing_target_chunks_text_and_ignores_delta() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    target = RoutingTarget(
        intent="godview",
        delivery=SlackDeliveryTarget(
            target_channel_id="C-TARGET",
            thread_ts="1710000003.000400",
        ),
    )

    await channel.send(_message(content="x" * 4100), routing_target=target)
    await channel.send(
        _message(event_type=EventType.CHAT_DELTA, content="partial"),
        routing_target=target,
    )

    assert len(client.calls) == 2
    assert client.calls[0] == {
        "channel": "C-TARGET",
        "text": "x" * 4000,
        "thread_ts": "1710000003.000400",
    }
    assert client.calls[1] == {
        "channel": "C-TARGET",
        "text": "x" * 100,
        "thread_ts": "1710000003.000400",
    }


# Payload shapes as the runtime builds them: every event other than the text
# events keys its body under something that is not "content".
_NON_TEXT_EVENT_PAYLOADS: dict[EventType, dict[str, Any]] = {
    EventType.CHAT_TOOL_CALL: {"tool_call": {"name": "Bash", "arguments": {}}},
    EventType.CHAT_TOOL_UPDATE: {
        "tool_update": {"tool_name": "Bash", "status": "in_progress"}
    },
    EventType.CHAT_TOOL_RESULT: {"result": "ok", "tool_name": "Bash"},
    EventType.TODO_UPDATED: {"todos": [{"id": "1", "status": "done"}]},
    EventType.CHAT_RETRACT: {"reason": "interrupt_snapshot_canceled"},
    EventType.CHAT_MEDIA: {"files": [{"name": "clip.mp4"}]},
    EventType.CHAT_MESSAGE_UPDATED: {"updates": {"mode": "agent.work.normal"}},
    EventType.CHAT_SESSION_RESULT: {"status": "completed", "result": "done"},
    EventType.CHAT_INTERRUPT_RESULT: {"intent": "cancel", "message": "cancelled"},
    EventType.CHAT_ASK_USER_QUESTION: {"questions": [{"question": "Which ledger?"}]},
    EventType.CHAT_PROCESSING_STATUS: {"is_processing": True},
}


@pytest.mark.asyncio
async def test_send_skips_stream_chunks_and_posts_the_completed_answer() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id="C-DEFAULT"),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(
        _message(
            event_type=EventType.CHAT_REASONING,
            content="The user wants the quarterly figure. Open the ledger first.",
        )
    )
    await channel.send(
        _message(event_type=EventType.CHAT_DELTA, content="the answer so f")
    )
    for event_type, payload in _NON_TEXT_EVENT_PAYLOADS.items():
        await channel.send(
            _message(
                event_type=event_type,
                payload={"event_type": event_type.value, **payload},
            )
        )
    await channel.send(_message(content="The figure is 42."))

    assert [call["text"] for call in client.calls] == ["The figure is 42."]


@pytest.mark.asyncio
async def test_send_falls_back_to_metadata_session_and_default_channel() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id="C-DEFAULT"),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(
        _message(
            metadata={
                "slack_channel_id": "C-META",
                "slack_thread_ts": "1710000004.000500",
            },
        ),
    )
    await channel.send(
        _message(metadata={}, session_id="slack_T1_C-SESSION_1710000005.000600")
    )
    await channel.send(_message(metadata={}, session_id="unknown"))

    assert [call["channel"] for call in client.calls] == [
        "C-META",
        "C-SESSION",
        "C-DEFAULT",
    ]
    assert client.calls[0]["thread_ts"] == "1710000004.000500"
    assert client.calls[1]["thread_ts"] == "1710000005.000600"
    assert "thread_ts" not in client.calls[2]


@pytest.mark.asyncio
async def test_start_and_stop_socket_mode_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    registered_events: list[str] = []
    fake_client = _FakeSlackClient()

    class FakeAsyncApp:
        def __init__(self, token: str) -> None:
            assert token == "xoxb-test"
            self.client = fake_client

        def event(self, event_name: str):
            registered_events.append(event_name)

            def register(listener):
                return listener

            return register

    class FakeSocketModeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            assert isinstance(app, FakeAsyncApp)
            assert app_token == "xapp-test"

        async def start_async(self) -> None:
            started.set()
            await closed.wait()

        async def close_async(self) -> None:
            closed.set()

    monkeypatch.setattr(slack_connect, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(slack_connect, "AsyncApp", FakeAsyncApp)
    monkeypatch.setattr(slack_connect, "AsyncSocketModeHandler", FakeSocketModeHandler)

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            bot_token="xoxb-test",
            app_token="xapp-test",
        ),
        RobotMessageRouter(),
    )
    task = asyncio.create_task(channel.start())
    await asyncio.wait_for(started.wait(), timeout=1)

    assert channel.is_running
    assert registered_events == ["app_mention", "message"]

    await channel.stop()
    await asyncio.wait_for(task, timeout=1)

    assert not channel.is_running
    assert closed.is_set()


def test_make_delivery_target_builds_slack_thread_target() -> None:
    target = make_delivery_target(
        "slack",
        chat_id="C1",
        physical_user_id="U1",
        thread_ts="1710000006.000700",
    )

    assert isinstance(target, SlackDeliveryTarget)
    assert target.target_channel_id == "C1"
    assert target.thread_ts == "1710000006.000700"
    assert target.physical_user_id == "U1"
    assert target.get_container_id() == "C1:1710000006.000700"
