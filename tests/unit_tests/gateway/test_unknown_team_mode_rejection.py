# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unknown "team.*" modes are refused with an error (issue #4168)."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.message_handler.message_handler import (
    _KNOWN_TEAM_MODE_INPUTS,
    MessageHandler,
)


def _chat_send(*, mode: str) -> Message:
    return Message(
        id="msg-1",
        type="request",
        channel_id="web",
        session_id="sess-team",
        req_method=ReqMethod.CHAT_SEND,
        params={"content": "拆解一下这个需求", "mode": mode},
        timestamp=0.0,
        ok=True,
        enable_streaming=True,
    )


@pytest.mark.parametrize("mode", ["team.foo", "team.work.fast", "TEAM.FOO", "team."])
def test_unknown_team_mode_send_is_rejected(mode: str):
    assert MessageHandler._is_unknown_team_mode_send(_chat_send(mode=mode)) is True


@pytest.mark.parametrize("mode", _KNOWN_TEAM_MODE_INPUTS)
def test_every_known_team_mode_input_is_allowed(mode: str):
    """Every known team mode passes."""
    assert MessageHandler._is_unknown_team_mode_send(_chat_send(mode=mode)) is False


@pytest.mark.parametrize(
    "mode",
    [
        "agent",
        "agent.plan",
        "agent.work",
        "agent.code",
        "code.normal",
        "code.team",
        # auto_harness is an internal mode sent by the scheduler; not guarded
        "auto_harness",
        # only team.* is tightened; other unknown modes keep the lenient path
        "agent.foo",
    ],
)
def test_non_team_and_internal_modes_are_untouched(mode: str):
    assert MessageHandler._is_unknown_team_mode_send(_chat_send(mode=mode)) is False


def test_missing_mode_is_untouched():
    msg = _chat_send(mode="agent")
    msg.params = {"content": "hi"}

    assert MessageHandler._is_unknown_team_mode_send(msg) is False


def test_non_chat_request_is_untouched():
    msg = _chat_send(mode="team.foo")
    msg.req_method = ReqMethod.CHAT_CANCEL

    assert MessageHandler._is_unknown_team_mode_send(msg) is False


@pytest.mark.asyncio
async def test_rejection_replies_with_error_listing_valid_modes():
    published: list[Message] = []
    handler = object.__new__(MessageHandler)
    handler.publish_robot_messages = lambda out: published.append(out) or _noop()

    await MessageHandler._reject_unknown_team_mode_send(
        handler, _chat_send(mode="team.foo")
    )

    assert len(published) == 1
    reply = published[0]
    assert reply.event_type == EventType.CHAT_ERROR
    assert reply.ok is False
    assert reply.payload["is_complete"] is True
    assert reply.enable_streaming is False
    assert reply.session_id == "sess-team"
    error = str(reply.payload["error"])
    assert "team.foo" in error
    for known in ("team", "team.work.normal", "team.code.normal"):
        assert known in error


def test_known_team_mode_inputs_all_resolve_to_team():
    """Every value listed in the error must really be a team mode."""
    assert len(_KNOWN_TEAM_MODE_INPUTS) >= 8
    for mode in _KNOWN_TEAM_MODE_INPUTS:
        assert is_team_mode(mode), mode


async def _noop() -> None:
    return None
