# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Only team-wide input gets the user-message envelope.

Member-addressed messages already travel inside the team's own
``<team-inbound from=... type=...>`` envelope, so they must reach the member
verbatim — nesting the user-input envelope inside that one puts two
contradicting headers on the same message.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
    _deliverable,
    _is_member_addressed,
)
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


def _turn() -> UserTurn:
    return UserTurn(
        text="",
        channel="web",
        language="zh",
        files={"uploaded_documents": [{"filename": "需求.md", "path": "/uploads/需求.md"}]},
    )


def _envelope(rendered: str) -> dict:
    return json.loads(rendered[rendered.index("{"):])


@pytest.mark.parametrize(
    "text",
    [
        "$human-member-1 @member-1 hello",
        "@member-1 看一下",
        "@all 停一下",
        "$human-reporter 我来说两句",
    ],
)
def test_member_addressed_messages_are_delivered_verbatim(text: str):
    assert _is_member_addressed(text) is True
    assert _deliverable(_turn(), text) == text


@pytest.mark.parametrize(
    "text",
    [
        "hello 团队",
        "# 大家注意",
        # A bare @name without the trailing space is god-view per openjiuwen's
        # own grammar, so it stays team-wide input here too.
        "@reviewer",
    ],
)
def test_team_wide_input_gets_the_envelope(text: str):
    assert _is_member_addressed(text) is False

    envelope = _envelope(_deliverable(_turn(), text))

    assert envelope["content"] == text
    assert "需求.md" in envelope["files_updated_by_user"]


def test_direct_message_keeps_its_own_wording():
    """Regression: the inner envelope contradicted the team-inbound header.

    A member received ``<team-inbound from="human-member-1">`` wrapping a
    ``{"source": "web", "type": "user input"}`` payload whose timestamp and
    files fields meant nothing on that channel.
    """
    delivered = _deliverable(_turn(), "$human-member-1 @member-1 hello")

    assert delivered == "$human-member-1 @member-1 hello"
    assert "你收到一条消息" not in delivered
    assert "files_updated_by_user" not in delivered


def test_non_text_payload_passes_through():
    marker = object()

    assert _deliverable(_turn(), marker) is marker


def test_empty_text_is_treated_as_team_wide():
    assert _is_member_addressed("") is False
