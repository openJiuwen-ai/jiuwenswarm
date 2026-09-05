# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the IMAP polling loop (E6 step 4)."""
import asyncio
from email.message import EmailMessage

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_connect import (
    EmailChannel,
    EmailChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.email.email_poller import poll_once


class _Fetcher:
    def __init__(self, mails=None, boom=False):
        self.mails = mails or []
        self.boom = boom

    def fetch_unseen(self):
        if self.boom:
            raise RuntimeError("imap down")
        return self.mails


def _raw(body="hello", sender="alice@x.com", subject="Deploy"):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "bot@x.com"
    mail["Subject"] = subject
    mail.set_content(body)
    return mail.as_bytes()


class _RecordingRouter(RobotMessageRouter):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def route_user_message(self, msg):
        self.sent.append(msg)


def _channel(**over):
    kwargs = {"enabled": True, "email_address": "bot@x.com", "token": "t"}
    kwargs.update(over)
    router = _RecordingRouter()
    channel = EmailChannel(
        EmailChannelConfig(**kwargs), router, smtp_sender=lambda _mail: None
    )
    return channel, router


def test_unseen_mail_is_dispatched_to_the_agent():
    channel, router = _channel()
    delivered = asyncio.run(poll_once(channel, _Fetcher([_raw("what is the status?")])))
    assert delivered == 1
    assert router.sent[0].params["content"] == "what is the status?"


def test_session_id_matches_the_mail_thread():
    channel, router = _channel()
    asyncio.run(poll_once(channel, _Fetcher([_raw("ok", subject="Re: Deploy")])))
    assert router.sent[0].session_id == "alice@x.com|deploy"


def test_thread_is_remembered_so_the_reply_goes_back():
    channel, _ = _channel()
    asyncio.run(poll_once(channel, _Fetcher([_raw("ok", subject="Deploy")])))
    assert channel.recipient_for("alice@x.com|deploy") == ("alice@x.com", "Deploy")


def test_quoted_reply_is_reduced_to_the_answer():
    channel, router = _channel()
    body = "1\n\n> Deploy v2.3?\n> Reply 1: Approve"
    asyncio.run(poll_once(channel, _Fetcher([_raw(body)])))
    assert router.sent[0].params["content"] == "1"


def test_mail_from_the_bot_itself_is_skipped():
    channel, router = _channel()
    delivered = asyncio.run(poll_once(channel, _Fetcher([_raw("loop", sender="bot@x.com")])))
    assert delivered == 0 and router.sent == []


def test_sender_outside_the_allow_list_is_rejected():
    channel, router = _channel(allow_from=["boss@x.com"])
    delivered = asyncio.run(poll_once(channel, _Fetcher([_raw("hello")])))
    assert delivered == 0 and router.sent == []


def test_empty_allow_list_accepts_everyone():
    channel, _ = _channel(allow_from=[])
    assert asyncio.run(poll_once(channel, _Fetcher([_raw("hello")]))) == 1


def test_mail_with_only_quoted_text_is_skipped():
    channel, router = _channel()
    delivered = asyncio.run(poll_once(channel, _Fetcher([_raw("> only quoted")])))
    assert delivered == 0 and router.sent == []


def test_imap_failure_is_swallowed():
    channel, router = _channel()
    assert asyncio.run(poll_once(channel, _Fetcher(boom=True))) == 0
    assert router.sent == []


def test_unreadable_mail_does_not_stop_the_batch():
    channel, router = _channel()
    delivered = asyncio.run(
        poll_once(channel, _Fetcher([b"\xff\xfe not a mail", _raw("second")]))
    )
    assert delivered == 1 and router.sent[0].params["content"] == "second"


def test_empty_mailbox_delivers_nothing():
    channel, router = _channel()
    assert asyncio.run(poll_once(channel, _Fetcher([]))) == 0
    assert router.sent == []


def test_metadata_carries_sender_and_subject():
    channel, router = _channel()
    asyncio.run(poll_once(channel, _Fetcher([_raw("hi", subject="Deploy")])))
    assert router.sent[0].metadata["email_sender"] == "alice@x.com"
    assert router.sent[0].metadata["email_subject"] == "Deploy"