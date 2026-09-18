# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prefix-cache stability tests for ProgressiveToolRail schema/nav."""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)


def _card(name: str, tool_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        id=tool_id or f"id-{name}",
        description=f"desc-{name}",
        input_params={},
    )


@pytest.mark.asyncio
async def test_before_model_call_orders_tools_by_eager_list():
    """ability_manager / MCP rebind order must not change the tools schema."""
    rail = ProgressiveToolRail(
        eager_tools=[
            "tools_search",
            "invoke_tool",
            "bash",
            "send_file_to_user",
        ],
    )
    tools = [
        _card("send_file_to_user"),
        _card("bash"),
        _card("invoke_tool"),
        _card("tools_search"),
    ]
    spb = SimpleNamespace(sections=[], add_section=lambda section: spb.sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(tools) + [_card("office_claw_x")]),
        system_prompt_builder=spb,
    )
    rail._deep_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(tools)))

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == [
        "tools_search",
        "invoke_tool",
        "bash",
        "send_file_to_user",
    ]


@pytest.mark.asyncio
async def test_navigation_skips_rewrite_when_only_tool_ids_change():
    """OfficeClaw request-scoped id rebind must refresh instances, not nav text."""
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    old = _card("office_claw_co_writing_initialize", "old-hash")
    new = _card("office_claw_co_writing_initialize", "new-hash")
    read_file = _card("read_file")

    sections: list = []
    spb = SimpleNamespace(add_section=lambda section: sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: [old, read_file, _card("bash")]),
        system_prompt_builder=spb,
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=[_card("bash"), _card("tools_search"), _card("invoke_tool")]),
    )

    await rail.before_model_call(ctx)
    assert len(sections) == 1
    first = sections[0]

    agent.ability_manager = SimpleNamespace(list=lambda: [new, read_file, _card("bash")])
    await rail.before_model_call(ctx)
    assert len(sections) == 1
    assert sections[0] is first
    assert rail._cached_deferred_tool_infos[0].id == "new-hash"


@pytest.mark.asyncio
async def test_navigation_rewrites_when_deferred_names_change():
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    sections: list = []
    spb = SimpleNamespace(add_section=lambda section: sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(
            list=lambda: [_card("read_file"), _card("bash")]
        ),
        system_prompt_builder=spb,
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=[_card("bash"), _card("tools_search")]),
    )

    await rail.before_model_call(ctx)
    assert len(sections) == 1

    agent.ability_manager = SimpleNamespace(
        list=lambda: [_card("read_file"), _card("office_claw_x"), _card("bash")]
    )
    await rail.before_model_call(ctx)
    assert len(sections) == 2
    assert "office_claw_x" in str(sections[1].content)
