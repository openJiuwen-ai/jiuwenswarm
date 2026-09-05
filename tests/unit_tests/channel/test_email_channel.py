# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the SMTP e-mail channel (E6 step 2)."""
import asyncio

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_connect import (
    DEFAULT_SUBJECT,
    EmailChannel,
    EmailChannelConfig,
)


def _channel(**over):
    sent = []
    kwargs = {
        "enabled": True,
        "email_address": "bot@x.com",
        "token": "t",
        "default_recipient": "fallback@x.com",
    }
    kwargs.update(over)
    channel = EmailChannel(
        EmailChannelConfig(**kwargs), RobotMessageRouter(), smtp_sender=sent.append
    )
    return channel, sent


def _msg(content, session_id="s1"):
    return Message(
        id="m1",
        type="req",
        channel_id="email",
        session_id=session_id,
        params={"content": content},
        timestamp=0.0,
        ok=True,
    )


def test_capability_sheet_declares_nothing():
    channel, _ = _channel()
    caps = channel.capabilities
    assert not caps.buttons and not caps.streaming and not caps.rich_text


def test_send_composes_plain_text_mail_to_remembered_thread():
    channel, sent = _channel()
    channel.remember_thread("s1", "alice@x.com", "Deploy v2.3")
    asyncio.run(channel.send(_msg("done")))
    assert len(sent) == 1
    assert sent[0]["To"] == "alice@x.com"
    assert sent[0]["Subject"] == "Re: Deploy v2.3"
    assert sent[0].get_content().strip() == "done"


def test_reply_subject_never_stacks_on_a_reply_thread():
    channel, sent = _channel()
    channel.remember_thread("s1", "alice@x.com", "Re: Deploy")
    asyncio.run(channel.send(_msg("ok")))
    assert sent[0]["Subject"] == "Re: Deploy"


def test_unknown_session_falls_back_to_default_recipient():
    channel, sent = _channel()
    asyncio.run(channel.send(_msg("hello", session_id="unknown")))
    assert sent[0]["To"] == "fallback@x.com"
    assert sent[0]["Subject"] == f"Re: {DEFAULT_SUBJECT}"


def test_empty_body_sends_nothing():
    channel, sent = _channel()
    channel.remember_thread("s1", "a@x.com", "Deploy")
    asyncio.run(channel.send(_msg("   ")))
    assert sent == []


def test_no_recipient_at_all_sends_nothing():
    channel, sent = _channel(default_recipient="")
    asyncio.run(channel.send(_msg("hello")))
    assert sent == []


def test_degraded_card_text_is_delivered_verbatim():
    channel, sent = _channel()
    channel.remember_thread("s1", "alice@x.com", "Deploy")
    body = "Deploy v2.3?\nReply 1: Approve · Reply 2: Deny"
    asyncio.run(channel.send(_msg(body)))
    assert "Reply 1: Approve" in sent[0].get_content()


def test_session_id_is_stable_across_reply_prefixes():
    channel, _ = _channel()
    assert channel.session_id_for("A@X.com", "Deploy") == channel.session_id_for(
        "a@x.com", "Re: Deploy"
    )


def test_start_requires_credentials():
    channel, _ = _channel(email_address="")
    asyncio.run(channel.start())
    assert channel.is_running is False


def test_start_marks_running_when_configured():
    channel, _ = _channel()
    asyncio.run(channel.start())
    assert channel.is_running is True
    asyncio.run(channel.stop())
    assert channel.is_running is False


def test_send_swallows_smtp_errors():
    def boom(_mail):
        raise RuntimeError("smtp down")

    channel = EmailChannel(
        EmailChannelConfig(
            enabled=True, email_address="bot@x.com", token="t", default_recipient="a@x.com"
        ),
        RobotMessageRouter(),
        smtp_sender=boom,
    )
    asyncio.run(channel.send(_msg("hello")))