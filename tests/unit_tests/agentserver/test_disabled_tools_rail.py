# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for DisabledToolsRail lifecycle and configuration semantics.

Covers the ``react.disabled_tools`` blacklist rail ported from enterprise_dev:
- ``_unregister_tools`` detaches cards from only the current agent's
  ``ability_manager``, caching them for re-registration.
- ``_register_tools`` restores them.
- ``uninit`` restores cards and clears the rail's agent-local state.
- ``before_model_call`` catches tools registered after rail initialization.
- ``update_config`` computes a diff: newly disabled tools are unregistered,
  newly enabled tools are re-registered.
- ``resolve_string_or_list_config`` parses list / comma-separated string / None.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import (
    DisabledToolsRail,
)
from jiuwenswarm.agents.harness.common.tools.skill_toolkits import SkillToolkit
from jiuwenswarm.common.config import resolve_string_or_list_config
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


# ---------------------------------------------------------------------------
# SkillToolkit default registration
# ---------------------------------------------------------------------------


def test_skill_toolkit_registers_all_three_skill_tools_by_default(tmp_path):
    """search_skill / install_skill / uninstall_skill 均无条件注册。"""
    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    toolkit = SkillToolkit(manager)

    tool_names = [tool.card.name for tool in toolkit.get_tools()]

    assert tool_names == ["search_skill", "install_skill", "uninstall_skill"]


# ---------------------------------------------------------------------------
# resolve_string_or_list_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, []),
        ([], []),
        (["a", " b ", "", "c"], ["a", "b", "c"]),
        ("a,b;c", ["a", "b", "c"]),
        ("single", ["single"]),
        ("", []),
    ],
)
def test_resolve_string_or_list_config_branches(value, expected):
    assert resolve_string_or_list_config(value) == expected


@pytest.mark.parametrize("value", [123, {"tool": "search_skill"}, [123]])
def test_resolve_string_or_list_config_rejects_malformed_values(value):
    with pytest.raises(TypeError):
        resolve_string_or_list_config(value)


def test_build_disabled_tools_rail_fails_closed_on_malformed_config():
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    with patch.object(interface_deep.logger, "error") as error_log, pytest.raises(
        TypeError
    ):
        interface_deep.JiuWenSwarmDeepAdapter._build_disabled_tools_rail(
            {"disabled_tools": 123}
        )

    error_log.assert_called_once()
    assert error_log.call_args.kwargs["exc_info"] is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_card(name: str, tool_id: str | None = None):
    """A ToolCard-like object keyed by name; id defaults to name (matches SkillToolkit)."""
    return SimpleNamespace(id=tool_id or name, name=name)


def _make_agent(cards: dict[str, object]):
    """Fake agent: ability_manager backed by ``cards`` dict (name -> ToolCard)."""
    ability = SimpleNamespace(
        _cards=dict(cards),
        get=lambda n: ability._cards.get(n),
        remove=lambda n: ability._cards.pop(n, None),
        add=lambda card: ability._cards.update({card.name: card}),
    )
    return SimpleNamespace(ability_manager=ability)


# ---------------------------------------------------------------------------
# unregister / register
# ---------------------------------------------------------------------------


def test_unregister_detaches_only_from_agent():
    cards = {
        "search_skill": _make_card("search_skill"),
        "install_skill": _make_card("install_skill"),
    }
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["search_skill"])
    rail.init(agent)

    # search_skill is hidden from this agent; install_skill remains visible.
    assert "search_skill" not in agent.ability_manager._cards
    assert "install_skill" in agent.ability_manager._cards
    assert "search_skill" in rail._removed_cards


def test_unregister_keeps_real_shared_resource_registered():
    """A per-agent blacklist must not evict a process-shared tool instance."""
    from uuid import uuid4

    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    tool_name = f"disabled_tools_shared_{uuid4().hex}"
    tool = LocalFunction(
        ToolCard(
            id=tool_name,
            name=tool_name,
            description="test shared tool",
            stateless=True,
        ),
        lambda: None,
    )
    Runner.resource_mgr.add_tool(tool)
    try:
        ability_manager = AbilityManager()
        ability_manager.add(tool.card)
        rail = DisabledToolsRail(disabled_tools=[tool_name])
        rail.init(SimpleNamespace(ability_manager=ability_manager))

        assert ability_manager.get(tool_name) is None
        assert Runner.resource_mgr.get_tool(tool_name) is tool
    finally:
        Runner.resource_mgr.remove_tool(tool_name)


def test_unregister_missing_tool_is_skipped():
    cards = {"install_skill": _make_card("install_skill")}
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["nonexistent", "install_skill"])
    rail.init(agent)

    # nonexistent gracefully skipped; install_skill still removed
    assert "install_skill" not in agent.ability_manager._cards
    assert "nonexistent" not in rail._removed_cards


@pytest.mark.asyncio
async def test_before_model_call_filters_tool_registered_after_init():
    agent = _make_agent({})
    rail = DisabledToolsRail(disabled_tools=["late_tool"])
    rail.init(agent)

    # Runtime MCP/Cron/extensions can register after rail initialization.
    agent.ability_manager.add(_make_card("late_tool"))
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            tools=[
                SimpleNamespace(name="late_tool"),
                SimpleNamespace(name="allowed_tool"),
            ]
        )
    )

    await rail.before_model_call(ctx)

    assert "late_tool" not in agent.ability_manager._cards
    assert [tool.name for tool in ctx.inputs.tools] == ["allowed_tool"]


def test_register_restores_previously_removed():
    cards = {"search_skill": _make_card("search_skill")}
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["search_skill"])
    rail.init(agent)
    assert "search_skill" not in agent.ability_manager._cards

    rail._register_tools({"search_skill"})
    assert "search_skill" in agent.ability_manager._cards


def test_uninit_restores_cards_and_clears_state():
    card = _make_card("search_skill")
    agent = _make_agent({"search_skill": card})
    rail = DisabledToolsRail(disabled_tools=["search_skill"])

    rail.init(agent)
    assert "search_skill" not in agent.ability_manager._cards

    rail.uninit(agent)

    assert agent.ability_manager._cards["search_skill"] is card
    assert rail._agent is None
    assert rail._removed_cards == {}


# ---------------------------------------------------------------------------
# update_config (hot-reload diff)
# ---------------------------------------------------------------------------


def test_update_config_disables_new_and_enables_old():
    # start with search_skill disabled; install_skill registered
    cards = {
        "search_skill": _make_card("search_skill"),
        "install_skill": _make_card("install_skill"),
        "uninstall_skill": _make_card("uninstall_skill"),
    }
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["search_skill"])
    rail.init(agent)
    assert "search_skill" not in agent.ability_manager._cards
    assert "install_skill" in agent.ability_manager._cards

    # hot-reload: now disable install_skill too, re-enable search_skill
    rail.update_config(["install_skill"])

    assert "install_skill" not in agent.ability_manager._cards  # newly disabled
    assert "search_skill" in agent.ability_manager._cards  # re-enabled
    assert rail._disabled_tools == {"install_skill"}


def test_update_config_noop_when_unchanged():
    cards = {"search_skill": _make_card("search_skill")}
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["search_skill"])
    rail.init(agent)

    rail.update_config(["search_skill"])  # same set
    assert "search_skill" not in agent.ability_manager._cards
    assert rail._disabled_tools == {"search_skill"}


def test_update_config_empty_list_re_enables_all():
    cards = {"search_skill": _make_card("search_skill")}
    agent = _make_agent(cards)

    rail = DisabledToolsRail(disabled_tools=["search_skill"])
    rail.init(agent)
    assert "search_skill" not in agent.ability_manager._cards

    rail.update_config(None)  # clear blacklist
    assert "search_skill" in agent.ability_manager._cards
    assert rail._disabled_tools == set()


def test_code_mode_builds_disabled_tools_rail(monkeypatch):
    """Code mode must apply the same react.disabled_tools policy as agent mode."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    monkeypatch.setattr(
        adapter,
        "_instantiate_rails",
        lambda rail_infos, _config_base: rail_infos,
    )

    rail_infos = adapter._build_agent_rails(
        {"disabled_tools": ["search_skill"]},
        {"models": {}, "modes": {"code": {"rails": []}}},
    )

    disabled_info = next(
        info for info in rail_infos if info.attr_name == "_disabled_tools_rail"
    )
    rail = disabled_info.build_func(**disabled_info.params)
    assert isinstance(rail, DisabledToolsRail)
    assert rail._disabled_tools == {"search_skill"}


def test_init_prunes_subagent_tools_and_refreshes_subagent_rail():
    """After detach, GP SubAgentConfig.tools and task_tool ads must drop blacklist."""
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.schema.config import SubAgentConfig

    web = _make_card("web_search")
    bash = _make_card("bash")
    cards = {"web_search": web, "bash": bash}
    agent = _make_agent(cards)

    gp = SubAgentConfig(
        agent_card=AgentCard(name="general-purpose", description="gp"),
        system_prompt="",
        tools=[web, bash],
    )
    agent.deep_config = SimpleNamespace(subagents=[gp])

    refreshed = {"called": False}

    class _FakeSubagentRail:
        def refresh_available_agents(self, _agent):
            refreshed["called"] = True

    agent.find_rails_by_type = lambda types: [_FakeSubagentRail()]

    rail = DisabledToolsRail(disabled_tools=["web_search"])
    rail.init(agent)

    assert "web_search" not in agent.ability_manager._cards
    names = [getattr(t, "name", None) for t in (gp.tools or [])]
    assert names == ["bash"]
    assert refreshed["called"] is True
