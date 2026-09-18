# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Microsoft Teams channel (E7 step 2)."""
import asyncio

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.teams.teams_cards import (
    ACTION_KEY,
    ADAPTIVE_CARD_CONTENT_TYPE,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.teams.teams_connect import (
    TeamsChannel,
    TeamsChannelConfig,
)

CARD = {
    "text": "Deploy v2.3?",
    "buttons": ({"label": "Approve", "action": "approve"},
                {"label": "Deny", "action": "deny"}),
}


def _channel(**over):
    posted = []
    kwargs = {
        "enabled": True,
        "app_id": "app",
        "app_password": "pw",
        "bot_name": "Jiuwen",
        "service_url": "https://smba.example.com",
    }
    kwargs.update(over)
    channel = TeamsChannel(
        TeamsChannelConfig(**kwargs),
        RobotMessageRouter(),
        poster=lambda url, activity: posted.append((url, activity)),
    )
    return channel, posted


def _msg(content=None, card=None, session_id="s1"):
    return Message(
        id="m1",
        type="req",
        channel_id="teams",
        session_id=session_id,
        params={"content": content} if content else {},
        timestamp=0.0,
        ok=True,
        payload={"interactive_card": card} if card else {},
    )


def test_capability_sheet_declares_buttons_and_rich_text():
    channel, _ = _channel()
    assert channel.capabilities.buttons and channel.capabilities.rich_text
    assert channel.capabilities.streaming is False


def test_plain_text_reply_is_posted_as_a_message_activity():
    channel, posted = _channel()
    channel.remember_conversation("s1", "conv1", "https://smba.example.com")
    asyncio.run(channel.send(_msg("done")))
    url, activity = posted[0]
    assert url.endswith("/v3/conversations/conv1/activities")
    assert activity == {"type": "message", "text": "done"}


def test_card_is_posted_as_an_adaptive_card_attachment():
    channel, posted = _channel()
    channel.remember_conversation("s1", "conv1", "https://smba.example.com")
    asyncio.run(channel.send(_msg(card=CARD)))
    _url, activity = posted[0]
    attachment = activity["attachments"][0]
    assert attachment["contentType"] == ADAPTIVE_CARD_CONTENT_TYPE
    assert [a["title"] for a in attachment["content"]["actions"]] == ["Approve", "Deny"]


def test_unknown_session_posts_nothing():
    channel, posted = _channel()
    asyncio.run(channel.send(_msg("hello", session_id="ghost")))
    assert posted == []


def test_empty_reply_posts_nothing():
    channel, posted = _channel()
    channel.remember_conversation("s1", "conv1", "https://smba.example.com")
    asyncio.run(channel.send(_msg("   ")))
    assert posted == []


def test_send_swallows_transport_errors():
    def boom(_url, _activity):
        raise RuntimeError("bot framework down")

    channel = TeamsChannel(
        TeamsChannelConfig(
            enabled=True, app_id="a", app_password="p", service_url="https://s"
        ),
        RobotMessageRouter(),
        poster=boom,
    )
    channel.remember_conversation("s1", "conv1", "https://s")
    asyncio.run(channel.send(_msg("hello")))


def test_start_requires_credentials():
    channel, _ = _channel(app_id="")
    asyncio.run(channel.start())
    assert channel.is_running is False


def test_start_marks_running_then_stop_clears():
    channel, _ = _channel()
    asyncio.run(channel.start())
    assert channel.is_running is True
    asyncio.run(channel.stop())
    assert channel.is_running is False


def test_parse_activity_strips_the_bot_mention():
    channel, _ = _channel()
    parsed = channel.parse_activity({
        "text": "<at>Jiuwen</at> what is the status?",
        "conversation": {"id": "conv1"},
        "channelData": {"tenant": {"id": "t1"}},
        "from": {"id": "user1"},
        "serviceUrl": "https://smba.example.com",
    })
    assert parsed["content"] == "what is the status?"
    assert parsed["session_id"] == "t1|conv1"
    assert parsed["is_action"] is False


def test_parse_activity_reads_a_card_click_as_the_action():
    channel, _ = _channel()
    parsed = channel.parse_activity({
        "value": {ACTION_KEY: "approve"},
        "conversation": {"id": "conv1"},
        "channelData": {"tenant": {"id": "t1"}},
        "from": {"id": "user1"},
    })
    assert parsed["content"] == "approve"
    assert parsed["is_action"] is True


def test_parse_activity_returns_none_without_conversation_or_text():
    channel, _ = _channel()
    assert channel.parse_activity({"text": "hi"}) is None
    assert channel.parse_activity({"conversation": {"id": "c"}, "text": "  "}) is None
    assert channel.parse_activity("not a dict") is None


def test_remembering_a_conversation_makes_the_reply_routable():
    channel, _ = _channel()
    channel.remember_conversation("t1|conv1", "conv1", "https://smba.example.com")
    assert channel.target_for("t1|conv1") == ("conv1", "https://smba.example.com")