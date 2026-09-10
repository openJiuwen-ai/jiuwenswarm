# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ProgressiveToolRail must honor react.disabled_tools for deferred tools."""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import (
    DisabledToolsRail,
)
from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
)
from jiuwenswarm.agents.harness.common.tools.tools_search_tool import (
    ToolsSearchInput,
)


def _card(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        id=f"id-{name}",
        description=f"desc-{name}",
        input_params={},
    )


@pytest.mark.asyncio
async def test_refresh_deferred_cache_excludes_disabled_tools():
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool", "bash"],
        disabled_tools=["memory_search", "web_search"],
    )
    rail._deep_agent = SimpleNamespace(
        ability_manager=SimpleNamespace(
            list=lambda: [
                _card("bash"),
                _card("memory_search"),
                _card("read_file"),
                _card("web_search"),
            ]
        )
    )

    await rail._refresh_deferred_tool_cache()

    names = {tool.name for tool in rail._cached_deferred_tool_infos}
    assert names == {"read_file"}
    assert {tool.name for tool in rail._cached_all_tool_infos} == {
        "bash",
        "memory_search",
        "read_file",
        "web_search",
    }


@pytest.mark.asyncio
async def test_before_model_call_filters_disabled_eager_and_nav():
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool", "bash", "web_search"],
        disabled_tools=["web_search", "memory_search"],
    )
    tools = [_card("bash"), _card("web_search"), _card("memory_search")]
    spb = SimpleNamespace(sections=[], add_section=lambda section: spb.sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(
            list=lambda: [_card("bash"), _card("web_search"), _card("memory_search"), _card("read_file")]
        ),
        system_prompt_builder=spb,
    )
    rail._deep_agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=list(tools)),
    )

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["bash"]
    assert spb.sections
    nav_text = str(spb.sections[0].content)
    assert "memory_search" not in nav_text
    assert "read_file" in nav_text


@pytest.mark.asyncio
async def test_search_and_invoke_reject_disabled_tools():
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        disabled_tools=["memory_get"],
    )
    rail._cached_deferred_tool_infos = [_card("memory_get")]

    search = await rail._search_tools(None, ToolsSearchInput(tool_name="memory_get"))
    assert search["success"] is False
    assert search["matches"] == []

    invoke = await rail._invoke_target_tool(
        None,
        InvokeToolInput(tool_name="memory_get", arguments={}),
    )
    assert invoke["success"] is False
    assert "禁用" in invoke["error"]


@pytest.mark.asyncio
async def test_current_disabled_tools_merges_sibling_rail():
    progressive = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        disabled_tools=["a"],
    )
    disabled = DisabledToolsRail(disabled_tools=["b", "c"])
    progressive._deep_agent = SimpleNamespace(
        find_rails_by_type=lambda types: [disabled],
    )

    assert progressive._current_disabled_tools() == frozenset({"a", "b", "c"})


def test_build_progressive_rail_passes_disabled_tools():
    pytest.importorskip("google.genai")
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_progressive_tool_rail_from_config,
    )

    rail = build_progressive_tool_rail_from_config(
        {
            "tool_lazy_load": {
                "enabled": True,
                "eager_tools": ["bash"],
            },
            "disabled_tools": ["memory_search", "web_search"],
        },
        language="cn",
    )
    assert rail is not None
    assert rail._disabled_tools == {"memory_search", "web_search"}
