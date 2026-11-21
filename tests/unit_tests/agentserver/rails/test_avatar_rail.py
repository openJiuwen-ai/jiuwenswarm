# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for AvatarPromptRail memory-blocking behaviour.

Covers the group-chat digital-avatar constraints:
- memory write tools (write_memory, edit_memory, coding_memory_write,
  coding_memory_edit, experience_learn) are blocked in group digital-avatar mode,
- read tools are still allowed in that mode,
- all memory tools (read + write) are blocked when memory is fully disabled.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.avatar_rail import (
    AvatarPromptRail,
    _MEMORY_READ_TOOLS,
    _MEMORY_WRITE_TOOLS,
)
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    PermissionContext,
    TOOL_PERMISSION_CONTEXT,
)

_DENY = "[PERMISSION_DENIED]"


def _perm(*, group_digital_avatar: bool, avatar_mode: bool,
          enable_memory: bool = True) -> PermissionContext:
    return PermissionContext(
        group_digital_avatar=group_digital_avatar,
        avatar_mode=avatar_mode,
        enable_memory=enable_memory,
    )


@pytest.fixture(autouse=True)
def _reset_permission_context():
    TOOL_PERMISSION_CONTEXT.set(None)
    yield
    TOOL_PERMISSION_CONTEXT.set(None)


def _run_before_tool_call(tool_name: str, perm: PermissionContext) -> SimpleNamespace:
    """Run AvatarPromptRail.before_tool_call and return the mutated ctx."""
    token = TOOL_PERMISSION_CONTEXT.set(perm)
    try:
        ctx = SimpleNamespace(
            agent=SimpleNamespace(),
            session=SimpleNamespace(session_id="sess1"),
            inputs=SimpleNamespace(
                tool_name=tool_name,
                tool_call=SimpleNamespace(id="call-1"),
                tool_result=None,
                tool_msg=None,
            ),
            extra={},
        )
        asyncio.run(AvatarPromptRail().before_tool_call(ctx))
        return ctx
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)


def _is_rejected(ctx: SimpleNamespace) -> bool:
    return bool(ctx.extra.get("_skip_tool")) and str(
        ctx.inputs.tool_result
    ).startswith(_DENY)


def test_group_avatar_blocks_write_memory_tools():
    perm = _perm(group_digital_avatar=True, avatar_mode=True)
    for tool in sorted(_MEMORY_WRITE_TOOLS):
        ctx = _run_before_tool_call(tool, perm)
        assert _is_rejected(ctx), f"{tool} should be blocked in group avatar mode"


def test_group_avatar_blocks_coding_memory_and_experience_tools():
    """Regression: before this change only write_memory/edit_memory were blocked,
    so coding_memory_write/experience_learn could bypass the group-chat
    memory-write prohibition."""
    perm = _perm(group_digital_avatar=True, avatar_mode=True)
    for tool in ("coding_memory_write", "coding_memory_edit", "experience_learn"):
        ctx = _run_before_tool_call(tool, perm)
        assert _is_rejected(ctx), f"{tool} must not bypass the memory-write ban"


def test_group_avatar_allows_read_memory_tools():
    perm = _perm(group_digital_avatar=True, avatar_mode=True)
    for tool in sorted(_MEMORY_READ_TOOLS):
        ctx = _run_before_tool_call(tool, perm)
        assert not _is_rejected(ctx), f"{tool} should be allowed (read only)"
        assert ctx.extra.get("_skip_tool") is None


def test_memory_disabled_blocks_all_memory_tools():
    perm = _perm(group_digital_avatar=True, avatar_mode=True, enable_memory=False)
    blocked = sorted(_MEMORY_WRITE_TOOLS | _MEMORY_READ_TOOLS)
    for tool in blocked:
        ctx = _run_before_tool_call(tool, perm)
        assert _is_rejected(ctx), f"{tool} should be blocked when memory is disabled"


def test_memory_disabled_allows_unrelated_tools():
    perm = _perm(group_digital_avatar=True, avatar_mode=True, enable_memory=False)
    ctx = _run_before_tool_call("read_file", perm)
    assert not _is_rejected(ctx)


def test_non_group_avatar_allows_write_memory_tools():
    perm = _perm(group_digital_avatar=False, avatar_mode=False)
    ctx = _run_before_tool_call("write_memory", perm)
    assert not _is_rejected(ctx)