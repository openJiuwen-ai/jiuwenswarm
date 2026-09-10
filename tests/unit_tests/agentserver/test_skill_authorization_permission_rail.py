# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leftover bash HITL must not treat the next chat string as a confirm payload."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
    is_unparseable_permission_resume_text,
)

_PLAIN_CHAT = "调用bash工具，不用大模型，直接执行命令'ls /tmpppp'"
_CONFIRM_DICT = {"approved": True, "auto_confirm": True, "persist_allow": False}
_CONFIRM_JSON = (
    '{"approved": true, "auto_confirm": true, "persist_allow": false}'
)


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        (_PLAIN_CHAT, True),
        ("hello", True),
        ("", True),
        (_CONFIRM_JSON, False),
        (_CONFIRM_DICT, False),
        (None, False),
        (SimpleNamespace(approved=True), False),
    ],
)
def test_unparseable_permission_resume_text(user_input: Any, expected: bool) -> None:
    assert is_unparseable_permission_resume_text(user_input) is expected


@pytest.mark.asyncio
async def test_plain_chat_resume_falls_back_to_first_check() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        captured["auto_confirm_config"] = auto_confirm_config
        return "first_check"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_PLAIN_CHAT,
            auto_confirm_config={"bash:ls /tmpppp": True},
        )

    assert result == "first_check"
    assert captured["user_input"] is None
    assert captured["auto_confirm_config"] == {"bash:ls /tmpppp": True}


@pytest.mark.asyncio
async def test_structured_confirm_payload_is_not_rewritten() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        return "approved"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_CONFIRM_DICT,
        )

    assert result == "approved"
    assert captured["user_input"] is _CONFIRM_DICT


@pytest.mark.asyncio
async def test_json_confirm_string_is_not_rewritten() -> None:
    rail = object.__new__(SkillAuthorizationPermissionRail)
    captured: dict[str, Any] = {}

    async def _parent_resolve(self, ctx, tool_call, user_input, auto_confirm_config=None):
        captured["user_input"] = user_input
        return "approved"

    with patch.object(PermissionInterruptRail, "resolve_interrupt", _parent_resolve):
        result = await rail.resolve_interrupt(
            ctx=SimpleNamespace(),
            tool_call=SimpleNamespace(name="bash"),
            user_input=_CONFIRM_JSON,
        )

    assert result == "approved"
    assert captured["user_input"] == _CONFIRM_JSON
