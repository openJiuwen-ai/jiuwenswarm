# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team rounds require a streaming channel; the gateway refuses the rest."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import (
    _NON_STREAM_TEAM_NOTICE,
    MessageHandler,
)


def _chat_send(*, mode: str, enable_streaming: bool) -> Message:
    return Message(
        id="msg-1",
        type="request",
        channel_id="feishu",
        session_id="sess-team",
        req_method=ReqMethod.CHAT_SEND,
        params={"content": "拆解一下这个需求", "mode": mode},
        timestamp=0.0,
        ok=True,
        enable_streaming=enable_streaming,
    )


def test_non_streaming_team_send_is_rejected():
    msg = _chat_send(mode="team", enable_streaming=False)

    assert MessageHandler._is_unsupported_non_stream_team_send(msg) is True


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
def test_every_team_mode_variant_is_rejected(mode: str):
    msg = _chat_send(mode=mode, enable_streaming=False)

    assert MessageHandler._is_unsupported_non_stream_team_send(msg) is True


def test_streaming_team_send_is_allowed():
    msg = _chat_send(mode="team", enable_streaming=True)

    assert MessageHandler._is_unsupported_non_stream_team_send(msg) is False


def test_non_streaming_single_agent_send_is_allowed():
    msg = _chat_send(mode="agent", enable_streaming=False)

    assert MessageHandler._is_unsupported_non_stream_team_send(msg) is False


def test_non_streaming_non_chat_request_is_untouched():
    msg = _chat_send(mode="team", enable_streaming=False)
    msg.req_method = ReqMethod.CHAT_CANCEL

    assert MessageHandler._is_unsupported_non_stream_team_send(msg) is False


@pytest.mark.asyncio
async def test_rejection_replies_with_an_explanation():
    published: list[Message] = []
    handler = object.__new__(MessageHandler)
    handler.publish_robot_messages = lambda out: published.append(out) or _noop()

    await MessageHandler._reject_non_stream_team_send(handler, _chat_send(mode="team", enable_streaming=False))

    assert len(published) == 1
    reply = published[0]
    assert reply.event_type == EventType.CHAT_FINAL
    assert reply.payload["content"] == _NON_STREAM_TEAM_NOTICE
    assert reply.payload["is_complete"] is True
    assert reply.enable_streaming is False
    assert reply.session_id == "sess-team"


async def _noop() -> None:
    return None
