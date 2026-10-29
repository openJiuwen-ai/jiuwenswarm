# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""GP inject-time filtering of leader-exclusive tools."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.common.tools.subagent_executor.excluded_spawn_tools import (
    apply_inject_general_purpose_excluded_tools_patch,
    filter_excluded_spawn_tools,
    tool_card_name,
)


def _card(name: str) -> SimpleNamespace:
    return SimpleNamespace(id=name, name=name)


def test_filter_excluded_spawn_tools_drops_send_file_keeps_others() -> None:
    tools = [
        _card("read_file"),
        _card("send_file_to_user"),
        _card("write_file"),
        _card("spawn_subagent"),
    ]
    filtered = filter_excluded_spawn_tools(tools)
    assert filtered is not None
    names = [card.name for card in filtered]
    assert names == ["read_file", "write_file"]


def test_filter_excluded_spawn_tools_preserves_none() -> None:
    assert filter_excluded_spawn_tools(None) is None


def test_filter_excluded_spawn_tools_empty_list() -> None:
    assert filter_excluded_spawn_tools([]) == []


def test_tool_card_name_supports_card_wrapper() -> None:
    wrapped = SimpleNamespace(card=_card("send_file_to_user"))
    assert tool_card_name(wrapped) == "send_file_to_user"


def test_code_agent_style_parent_cards_drop_send_file() -> None:
    """CodeAgentRail passes parent ToolCards into create_deep_agent tools=."""
    parent_tool_cards = [
        _card("bash"),
        _card("send_file_to_user"),
        _card("read_file"),
    ]
    filtered = filter_excluded_spawn_tools(parent_tool_cards) or []
    assert [tool_card_name(t) for t in filtered] == ["bash", "read_file"]


def test_apply_inject_patch_filters_tools_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def _orig_inject(subagents: Any, **kwargs: Any) -> list[Any]:
        calls["tools"] = kwargs.get("tools")
        return list(subagents or [])

    import openjiuwen.harness.factory as factory

    monkeypatch.setattr(factory, "_inject_general_purpose_subagent", _orig_inject)

    assert apply_inject_general_purpose_excluded_tools_patch() is True
    assert apply_inject_general_purpose_excluded_tools_patch() is True  # idempotent

    patched = getattr(factory, "_inject_general_purpose_subagent")
    patched(
        [],
        add_general_purpose_agent=True,
        resolved_language="cn",
        rails=None,
        system_prompt="",
        tools=[_card("bash"), _card("send_file_to_user")],
        mcps=None,
        model=None,
        skills=None,
    )
    assert [tool_card_name(t) for t in calls["tools"]] == ["bash"]


@pytest.mark.asyncio
async def test_subagent_permission_rail_rejects_excluded_spawn_tool() -> None:
    """Execution-time deep defense: EXCLUDED_TOOLS_SPAWN always rejected."""
    from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
        SubagentPermissionRail,
    )

    rail = SubagentPermissionRail(agent_scope_id="sub-test")
    tool_call = SimpleNamespace(id="call-1")
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name="send_file_to_user",
            tool_call=tool_call,
            tool_args={},
        ),
        extra={},
        agent=None,
        session=None,
    )
    rail._skip_tool = MagicMock()  # type: ignore[method-assign]

    await rail.before_tool_call(ctx)  # type: ignore[arg-type]

    rail._skip_tool.assert_called_once()
    assert "_interrupt_decision" in ctx.extra
