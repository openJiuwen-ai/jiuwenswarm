# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host mode assembly keeps PCS search callable without widening Core defaults."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gateway.common.mode_matrix import NEW_CANONICAL_MODES
from jiuwenswarm.agents.swarm import register_swarm_providers
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import member_rails
from jiuwenswarm.common import utils
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.harness.prompts import PromptAttachmentManager
from openjiuwen.harness.rails.agent_mode_rail import AgentModeRail


_MODE_ROLES = [
    (mode, role)
    for mode in sorted(NEW_CANONICAL_MODES)
    for role in (["leader", "teammate"] if mode.startswith("team.") else ["agent"])
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,role", _MODE_ROLES)
async def test_host_modes_mount_pcs_and_allow_search(mode, role, tmp_path, monkeypatch):
    """Verify host wiring and policy independently of the installed Core version."""
    register_swarm_providers()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path / ".jiuwenswarm")
    home = tmp_path / ".jiuwenswarm" / ".personal_context"
    # The host pins Core separately. Mock its Rail contract here; Core tests
    # own the actual search, file reading and Agent-use switch behavior.
    pcs_rail = MagicMock()
    rail_factory = MagicMock(return_value=pcs_rail)
    monkeypatch.setattr(interface_deep, "PersonalContextRail", rail_factory)
    monkeypatch.setattr(member_rails, "PersonalContextRail", rail_factory)
    search_tool = LocalFunction(card=ToolCard(name="pcs_search"), func=AsyncMock())
    pcs_rail.init.side_effect = lambda owner: owner.ability_manager.add_ability(search_tool.card, search_tool)
    agent = MagicMock()
    agent.ability_manager = AbilityManager(owner_id=f"pcs-host-mode-{uuid4().hex}")
    agent.prompt_attachment_manager = PromptAttachmentManager()
    agent.system_prompt_builder.language = "en"
    agent.deep_config = SimpleNamespace(subagents=[])
    agent.load_state.return_value = SimpleNamespace(
        plan_mode=SimpleNamespace(mode="plan" if mode.endswith(".plan") else "normal", plan_slug="pcs"),
    )
    mode_rail = None
    sanitizer = None
    if role == "agent":
        is_code = ".code." in mode
        adapter_type = JiuwenSwarmCodeAdapter if is_code else JiuWenSwarmDeepAdapter
        adapter = object.__new__(adapter_type)
        adapter._is_code_agent = is_code
        adapter._instance = agent
        adapter._personal_context_rail = None
        adapter._personal_context_rail_lock = asyncio.Lock()
        adapter._personal_context_runtime_enabled = True
        adapter._resolve_runtime_language = lambda: "en"

        async def mount(rail):
            rail.init(agent)

        agent.register_rail = AsyncMock(side_effect=mount)
        await adapter._sync_personal_context_rail(mode)
        pcs_rail = adapter._personal_context_rail
        mode_rail = adapter._build_agent_mode_rail({}) if is_code else adapter._build_work_agent_mode_rail()
        assert mode_rail is not None
    else:
        specs, _ = build_member_capability_specs({}, mode, role)
        context = SwarmBuildContext(mode=mode, role=role, config={})
        pcs_spec = next(spec for spec in specs if spec.type == "swarm.personal_context")
        pcs_rail = pcs_spec.build(language="en", context=context)
        pcs_rail.init(agent)
        for spec in specs:
            if spec.type == "swarm.code_agent_mode":
                mode_rail = spec.build(language="en", context=context)
                assert mode_rail is not None
            elif spec.type == "swarm.designer_surface_sanitizer":
                sanitizer = spec.build(language="en", context=context)

    assert pcs_rail is not None
    rail_factory.assert_called_once_with(home)
    pcs_rail.init.assert_called_once_with(agent)
    if mode.endswith(".plan") and role != "teammate":
        assert mode_rail is not None
    if mode == "team.design" and role == "leader":
        assert sanitizer is not None
    try:
        if mode_rail is not None:
            mode_rail.init(agent)
        if sanitizer is not None:
            sanitizer.init(agent)
        card = agent.ability_manager.get("pcs_search")
        assert card is not None
        ctx = SimpleNamespace(
            agent=agent,
            session=SimpleNamespace(session_id="host-pcs-mode"),
            inputs=SimpleNamespace(
                messages=[AssistantMessage(content="Find the project notes")],
                tools=[card, ToolCard(name="send_file_to_user")],
                tool_name="pcs_search", tool_args={"query": "pcsmodecheck"},
                tool_call=SimpleNamespace(id="pcs-mode-call"),
            ),
            extra={},
        )
        if mode_rail is not None:
            await mode_rail.before_model_call(ctx)
            await mode_rail.before_tool_call(ctx)
            assert not ctx.extra.get("_skip_tool", False)
            if mode.endswith(".plan") and role != "teammate":
                assert [item.name for item in ctx.inputs.tools] == ["pcs_search"]
        if sanitizer is not None:
            await sanitizer.before_model_call(ctx)
        assert agent.ability_manager.get("pcs_search") is card
    finally:
        if mode_rail is not None:
            mode_rail.uninit(agent)
        if sanitizer is not None:
            sanitizer.uninit(agent)
        pcs_rail.uninit(agent)
        for card in list(agent.ability_manager.list()):
            agent.ability_manager.remove_ability(card.name)


@pytest.mark.asyncio
async def test_core_default_plan_policy_does_not_implicitly_allow_host_search():
    rail = AgentModeRail(plan_mode_system_note="Read-only planning")
    rail._agent = MagicMock()
    rail._agent.load_state.return_value = SimpleNamespace(plan_mode=SimpleNamespace(mode="plan"))
    rail.system_prompt_builder = MagicMock(language="en")
    ctx = SimpleNamespace(
        session=SimpleNamespace(),
        inputs=SimpleNamespace(tool_name="pcs_search", tool_args={"query": "notes"}, tool_call=None),
        extra={},
    )
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True
