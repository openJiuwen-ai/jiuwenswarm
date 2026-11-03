# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for member-scoped skill toolkit rail."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.ability_manager import AbilityManager

from jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail import (
    MemberSkillToolkitRail,
)


class _FakeResourceManager:
    def __init__(self) -> None:
        self.tools = {}
        self.removed = []

    def add_tool(self, tools, *, tag=None, refresh=False, skip_if_exists=False):
        items = tools if isinstance(tools, list) else [tools]
        for tool in items:
            if skip_if_exists and tool.card.id in self.tools:
                continue
            self.tools[tool.card.id] = tool

    def remove_tool(self, tool_id):
        self.removed.append(tool_id)
        return self.tools.pop(tool_id, None)


class _FakeSkillManager:
    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir


class _FakeSkillToolkit:
    def __init__(self, manager) -> None:
        self.manager = manager

    def get_tools(self):
        return [
            self._make_tool("search_skill"),
            self._make_tool("install_skill"),
            self._make_tool("uninstall_skill"),
        ]

    @staticmethod
    def _make_tool(name: str):
        card = ToolCard(
            id=name,
            name=name,
            description=f"{name} desc",
            input_params={"type": "object"},
        )
        return LocalFunction(card=card, func=lambda **_: None)


def _make_agent(agent_id: str):
    # The owner id drives ``add_ability`` id qualification (mirrors how
    # BaseAgent / DeepAgent.configure wire it from the agent card in production).
    ability_manager = AbilityManager(owner_id=agent_id)
    return SimpleNamespace(
        card=SimpleNamespace(id=agent_id, name=agent_id),
        ability_manager=ability_manager,
    )


def test_team_member_skill_toolkit_rail_init_registers_member_scoped_tools(monkeypatch, tmp_path):
    """Rail init should replace inherited global cards with member-scoped tool ids."""
    resource_mgr = _FakeResourceManager()
    rail_module = "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail"

    monkeypatch.setattr("openjiuwen.core.runner.Runner.resource_mgr", resource_mgr, raising=False)
    monkeypatch.setattr(f"{rail_module}.SkillManager", _FakeSkillManager)
    monkeypatch.setattr(f"{rail_module}.SkillToolkit", _FakeSkillToolkit)

    agent = _make_agent("member-agent-1")
    rail = MemberSkillToolkitRail(workspace_dir=str(tmp_path))

    rail.init(agent)

    install_card = agent.ability_manager.get("install_skill")
    assert install_card is not None
    assert install_card.id == "install_skill_member-agent-1"
    assert sorted(resource_mgr.tools) == [
        "install_skill_member-agent-1",
        "search_skill_member-agent-1",
        "uninstall_skill_member-agent-1",
    ]


def test_team_member_skill_toolkit_rail_uninit_cleans_up_registered_tools(monkeypatch, tmp_path):
    """Rail uninit should remove member-scoped tools from ability and resource managers."""
    resource_mgr = _FakeResourceManager()
    rail_module = "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail"

    monkeypatch.setattr("openjiuwen.core.runner.Runner.resource_mgr", resource_mgr, raising=False)
    monkeypatch.setattr(f"{rail_module}.SkillManager", _FakeSkillManager)
    monkeypatch.setattr(f"{rail_module}.SkillToolkit", _FakeSkillToolkit)

    agent = _make_agent("member-agent-2")
    rail = MemberSkillToolkitRail(workspace_dir=str(tmp_path))

    rail.init(agent)
    rail.uninit(agent)

    assert agent.ability_manager.get("search_skill") is None
    assert agent.ability_manager.get("install_skill") is None
    assert agent.ability_manager.get("uninstall_skill") is None
    assert resource_mgr.tools == {}
    assert resource_mgr.removed == [
        "search_skill_member-agent-2",
        "install_skill_member-agent-2",
        "uninstall_skill_member-agent-2",
    ]


class _MutatingSkillToolkit(_FakeSkillToolkit):
    """Toolkit exposing only the library-mutating tools the rail has to wrap."""

    SUCCESS_BY_TOOL = {
        "install_skill": {"success": True, "skill": {"name": "new-skill"}},
        "uninstall_skill": {"success": True, "skill": {"name": "old-skill"}},
    }

    async def install_skill(self, **_):
        return self.SUCCESS_BY_TOOL["install_skill"]

    async def uninstall_skill(self, **_):
        return self.SUCCESS_BY_TOOL["uninstall_skill"]

    async def search_skill(self, **_):
        return {"success": True, "skills": []}

    def get_tools(self):
        return [
            self._make_bound_tool("install_skill", self.install_skill),
            self._make_bound_tool("uninstall_skill", self.uninstall_skill),
            self._make_bound_tool("search_skill", self.search_skill),
        ]

    @staticmethod
    def _make_bound_tool(name: str, func):
        card = ToolCard(
            id=name,
            name=name,
            description=f"{name} desc",
            input_params={"type": "object", "properties": {}},
        )
        return LocalFunction(card=card, func=func)


def _install_mutating_toolkit(monkeypatch, resource_mgr, toolkit_managers):
    """Wire the fake toolkit and resource manager the mutation tests share."""
    rail_module = "jiuwenswarm.agents.harness.team.rails.team_member_skill_toolkit_rail"
    monkeypatch.setattr("openjiuwen.core.runner.Runner.resource_mgr", resource_mgr, raising=False)
    monkeypatch.setattr(f"{rail_module}.SkillToolkit", _MutatingSkillToolkit)
    monkeypatch.setattr(
        _MutatingSkillToolkit,
        "__init__",
        lambda self, manager: toolkit_managers.append(manager),
    )


@pytest.mark.asyncio
async def test_team_member_skill_toolkit_rail_notifies_after_successful_install(monkeypatch, tmp_path):
    """A successful install writes into the one library, so the view is reloaded."""
    resource_mgr = _FakeResourceManager()
    change_calls = []
    shared_manager = object()
    toolkit_managers = []
    _install_mutating_toolkit(monkeypatch, resource_mgr, toolkit_managers)

    agent = _make_agent("member-agent-3")
    rail = MemberSkillToolkitRail(
        workspace_dir=str(tmp_path),
        manager=shared_manager,
        on_skill_library_changed=lambda result: change_calls.append(result),
    )

    rail.init(agent)
    install_tool = resource_mgr.tools["install_skill_member-agent-3"]
    result = await install_tool.invoke({})

    assert result["success"] is True
    assert toolkit_managers == [shared_manager]
    assert change_calls == [result]


@pytest.mark.asyncio
async def test_team_member_skill_toolkit_rail_notifies_after_successful_uninstall(monkeypatch, tmp_path):
    """Uninstall mutates the same single library and must reload the view too."""
    resource_mgr = _FakeResourceManager()
    change_calls = []
    toolkit_managers = []
    _install_mutating_toolkit(monkeypatch, resource_mgr, toolkit_managers)

    agent = _make_agent("member-agent-4")
    rail = MemberSkillToolkitRail(
        workspace_dir=str(tmp_path),
        manager=object(),
        on_skill_library_changed=lambda result: change_calls.append(result),
    )

    rail.init(agent)
    uninstall_tool = resource_mgr.tools["uninstall_skill_member-agent-4"]
    result = await uninstall_tool.invoke({})

    assert result["success"] is True
    assert change_calls == [result]


@pytest.mark.asyncio
async def test_team_member_skill_toolkit_rail_ignores_non_mutating_tools(monkeypatch, tmp_path):
    """Read-only Skill tools leave the library untouched, so nothing is reloaded."""
    resource_mgr = _FakeResourceManager()
    change_calls = []
    toolkit_managers = []
    _install_mutating_toolkit(monkeypatch, resource_mgr, toolkit_managers)

    agent = _make_agent("member-agent-5")
    rail = MemberSkillToolkitRail(
        workspace_dir=str(tmp_path),
        manager=object(),
        on_skill_library_changed=lambda result: change_calls.append(result),
    )

    rail.init(agent)
    search_tool = resource_mgr.tools["search_skill_member-agent-5"]
    result = await search_tool.invoke({})

    assert result["success"] is True
    assert change_calls == []
