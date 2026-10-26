# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prefix-cache stability tests for ProgressiveToolRail schema/nav."""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest
from openjiuwen.harness.prompts.sections import SectionName

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


class _RecordingSPB:
    """Minimal SystemPromptBuilder stand-in with has_section (mirrors !2840)."""

    def __init__(self) -> None:
        self.sections: list = []
        self._by_name: dict = {}

    def add_section(self, section) -> None:
        self.sections.append(section)
        self._by_name[section.name] = section

    def has_section(self, name) -> bool:
        return name in self._by_name

    def remove_section(self, name) -> None:
        self._by_name.pop(name, None)
        self.sections = [s for s in self.sections if s.name != name]


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
    spb = _RecordingSPB()
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

    spb = _RecordingSPB()
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
    assert len(spb.sections) == 1
    first = spb.sections[0]
    assert spb.has_section(SectionName.TOOL_NAVIGATION)

    agent.ability_manager = SimpleNamespace(list=lambda: [new, read_file, _card("bash")])
    await rail.before_model_call(ctx)
    assert len(spb.sections) == 1
    assert spb.sections[0] is first
    assert rail._cached_deferred_tool_infos[0].id == "new-hash"


@pytest.mark.asyncio
async def test_navigation_rewrites_when_deferred_names_change():
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    spb = _RecordingSPB()
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
    assert len(spb.sections) == 1

    agent.ability_manager = SimpleNamespace(
        list=lambda: [_card("read_file"), _card("office_claw_x"), _card("bash")]
    )
    await rail.before_model_call(ctx)
    assert len(spb.sections) == 2
    assert "office_claw_x" in str(spb.sections[1].content)


@pytest.mark.asyncio
async def test_navigation_rewrites_when_system_prompt_builder_replaced():
    """Blank SPB after hot-reload must get TOOL_NAVIGATION even if fingerprint matches.

    Mirrors agent-core !2840 builder-replaced / has_section guard: DeepAgent
    rebuilds SystemPromptBuilder while keeping this rail instance.
    """
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    deferred = [_card("read_file"), _card("bash")]
    spb1 = _RecordingSPB()
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(deferred)),
        system_prompt_builder=spb1,
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=[_card("bash"), _card("tools_search")]),
    )

    await rail.before_model_call(ctx)
    assert spb1.has_section(SectionName.TOOL_NAVIGATION)
    assert rail._navigation_name_fingerprint is not None
    fingerprint_after_first = rail._navigation_name_fingerprint

    # Hot-reload: blank builder, same rail + same deferred names (no invalidate).
    spb2 = _RecordingSPB()
    agent.system_prompt_builder = spb2
    await rail.before_model_call(ctx)

    assert spb2.has_section(SectionName.TOOL_NAVIGATION)
    assert len(spb2.sections) == 1
    assert rail._navigation_name_fingerprint == fingerprint_after_first


@pytest.mark.asyncio
async def test_navigation_rewrites_after_agent_rebind_clears_fingerprint():
    """Agent swap via _resolve_runtime_agent must invalidate nav fingerprint."""
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    deferred = [_card("read_file"), _card("bash")]
    spb1 = _RecordingSPB()
    agent1 = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(deferred)),
        system_prompt_builder=spb1,
    )
    rail._deep_agent = agent1
    rail._runtime_agent = agent1
    ctx1 = SimpleNamespace(
        agent=agent1,
        inputs=SimpleNamespace(tools=[_card("bash"), _card("tools_search")]),
    )

    await rail.before_model_call(ctx1)
    assert spb1.has_section(SectionName.TOOL_NAVIGATION)
    assert rail._navigation_name_fingerprint is not None

    spb2 = _RecordingSPB()
    agent2 = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(deferred)),
        system_prompt_builder=spb2,
    )
    ctx2 = SimpleNamespace(
        agent=agent2,
        inputs=SimpleNamespace(tools=[_card("bash"), _card("tools_search")]),
    )

    await rail.before_model_call(ctx2)
    assert rail._deep_agent is agent2
    assert spb2.has_section(SectionName.TOOL_NAVIGATION)
    assert len(spb2.sections) == 1


def test_update_disabled_tools_clears_navigation_fingerprint():
    rail = ProgressiveToolRail(eager_tools=["bash"])
    rail._navigation_name_fingerprint = frozenset({"read_file"})
    rail._cached_deferred_tool_infos = [_card("read_file")]

    rail.update_disabled_tools(["memory_search"])

    assert rail._navigation_name_fingerprint is None
    assert rail._cached_deferred_tool_infos == []


def test_invalidate_deferred_tool_cache_clears_navigation_fingerprint():
    rail = ProgressiveToolRail(eager_tools=["bash"])
    rail._navigation_name_fingerprint = frozenset({"read_file"})
    rail._cached_all_tool_infos = [_card("bash")]
    rail._cached_deferred_tool_infos = [_card("read_file")]

    rail.invalidate_deferred_tool_cache()

    assert rail._navigation_name_fingerprint is None
    assert rail._cached_all_tool_infos == []
    assert rail._cached_deferred_tool_infos == []


@pytest.mark.asyncio
async def test_navigation_drops_removed_mcp_tool_after_list_tool_info_sync():
    """Deleted MCP tools must leave TOOL_NAVIGATION after ResourceMgr refresh.

    Regression: ability_manager.list() kept stale mcp_* ToolCards that
    list_tool_info only writes, never deletes — so mid-session removals still
    appeared in the system prompt navigation section.
    """
    echo = _card(
        "mcp_mock-dynamic_echo",
        "sid.mock-dynamic.echo",
    )
    ping = _card(
        "mcp_mock-dynamic_ping",
        "sid.mock-dynamic.ping",
    )
    new_tool = _card(
        "mcp_mock-dynamic_new_tool",
        "sid.mock-dynamic.new_tool",
    )
    tools = {
        echo.name: echo,
        ping.name: ping,
        new_tool.name: new_tool,
    }

    async def list_tool_info():
        # Live ResourceMgr snapshot no longer includes new_tool.
        return [
            SimpleNamespace(name=echo.name, description=echo.description, parameters={}),
            SimpleNamespace(name=ping.name, description=ping.description, parameters={}),
        ]

    def is_mcp(card_id: str) -> bool:
        return str(card_id).startswith("sid.")

    am = SimpleNamespace(
        _tools=tools,
        _mcp_servers={
            "mock-dynamic": SimpleNamespace(
                server_id="sid",
                server_name="mock-dynamic",
            )
        },
        list=lambda: list(tools.values()),
        list_tool_info=list_tool_info,
        _is_tool_in_mcp_server=is_mcp,
    )

    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    # Simulate prior turn that had already published a 3-tool navigation.
    rail._navigation_name_fingerprint = frozenset(
        {echo.name, ping.name, new_tool.name}
    )
    rail._cached_all_tool_infos = [echo, ping, new_tool]
    rail._cached_deferred_tool_infos = [echo, ping, new_tool]

    from openjiuwen.harness.prompts import PromptSection

    spb = _RecordingSPB()
    spb.add_section(
        PromptSection(
            name=SectionName.TOOL_NAVIGATION,
            content={
                "cn": f"- {echo.name}\n- {new_tool.name}\n- {ping.name}",
                "en": f"- {echo.name}\n- {new_tool.name}\n- {ping.name}",
            },
            priority=70,
        )
    )
    agent = SimpleNamespace(ability_manager=am, system_prompt_builder=spb)
    rail._deep_agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(
            tools=[_card("tools_search"), _card("invoke_tool")],
        ),
    )

    await rail.before_model_call(ctx)

    assert new_tool.name not in tools
    nav = spb._by_name[SectionName.TOOL_NAVIGATION]
    cn = nav.content["cn"] if isinstance(nav.content, dict) else str(nav.content)
    assert new_tool.name not in cn
    assert echo.name in cn
    assert ping.name in cn
    assert rail._navigation_name_fingerprint == frozenset({echo.name, ping.name})
