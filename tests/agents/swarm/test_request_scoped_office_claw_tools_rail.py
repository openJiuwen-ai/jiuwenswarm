# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped OfficeClaw tools on in-process Team members."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import member_rails
from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.mcp_config import OfficeClawMcpRegistration


class _AbilityManager:
    def __init__(self) -> None:
        self.cards: dict[str, ToolCard] = {}

    def get(self, name: str) -> ToolCard | None:
        return self.cards.get(name)

    def add(self, card: ToolCard) -> SimpleNamespace:
        existing = self.cards.get(card.name)
        if existing is not None and existing.id != card.id:
            return SimpleNamespace(added=False, reason="duplicate_tool")
        self.cards[card.name] = card
        return SimpleNamespace(added=True, reason="added_tool")

    def remove(self, name: str) -> ToolCard | None:
        return self.cards.pop(name, None)


def _registration(request_id: str, session_id: str) -> OfficeClawMcpRegistration:
    card = ToolCard(
        id=f"office-claw-request-{request_id}.cos.cos_get_object",
        name="cos_get_object",
        description="Read an object from COS.",
        input_params={"type": "object"},
    )
    tool = SimpleNamespace(card=card)
    return OfficeClawMcpRegistration(
        request_id=request_id,
        tool_ids=(card.id,),
        tool_names=(card.name,),
        tool_instances=(tool,),
        session_id=session_id,
    )


@pytest.fixture(autouse=True)
def _clear_live_registrations() -> None:
    mcp_config._clear_live_office_claw_allowlists_for_tests()
    yield
    mcp_config._clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
async def test_team_member_mounts_request_tools_for_one_invoke() -> None:
    registration = _registration("req-1", "session-1")
    mcp_config.publish_request_scoped_mcp_registration(registration)
    rail = member_rails._build_request_scoped_mcp_tools_rail(
        {}, SwarmBuildContext(session_id="session-1")
    )
    ability_manager = _AbilityManager()
    agent = SimpleNamespace(ability_manager=ability_manager)
    ctx = AgentCallbackContext(agent=agent)

    await rail.before_invoke(ctx)

    assert ability_manager.get("cos_get_object") is registration.tool_instances[0].card
    assert getattr(ability_manager, mcp_config._OFFICE_CLAW_TOOL_IDS_ATTR) == frozenset(
        registration.tool_ids
    )

    await rail.after_invoke(ctx)

    assert ability_manager.get("cos_get_object") is None
    assert not hasattr(ability_manager, mcp_config._OFFICE_CLAW_TOOL_IDS_ATTR)


@pytest.mark.asyncio
async def test_team_member_resume_replaces_stale_request_tool() -> None:
    old_registration = _registration("req-old", "session-1")
    new_registration = _registration("req-new", "session-1")
    rail = member_rails._build_request_scoped_mcp_tools_rail(
        {}, SwarmBuildContext(session_id="session-1")
    )
    ability_manager = _AbilityManager()
    agent = SimpleNamespace(ability_manager=ability_manager)

    mcp_config.publish_request_scoped_mcp_registration(old_registration)
    old_ctx = AgentCallbackContext(agent=agent)
    await rail.before_invoke(old_ctx)
    assert ability_manager.get("cos_get_object").id == old_registration.tool_ids[0]

    mcp_config.publish_request_scoped_mcp_registration(new_registration)
    mcp_config.revoke_request_scoped_mcp_registration(old_registration)
    new_ctx = AgentCallbackContext(agent=agent)
    await rail.before_invoke(new_ctx)

    assert (
        mcp_config.get_request_scoped_mcp_registration("session-1") is new_registration
    )
    assert ability_manager.get("cos_get_object").id == new_registration.tool_ids[0]

    await rail.after_invoke(old_ctx)
    assert ability_manager.get("cos_get_object").id == new_registration.tool_ids[0]

    await rail.after_invoke(new_ctx)
    assert ability_manager.get("cos_get_object") is None


@pytest.mark.asyncio
async def test_team_member_keeps_conflicting_static_tool() -> None:
    registration = _registration("req-1", "session-1")
    mcp_config.publish_request_scoped_mcp_registration(registration)
    rail = member_rails._build_request_scoped_mcp_tools_rail(
        {}, SwarmBuildContext(session_id="session-1")
    )
    static_card = ToolCard(
        id="builtin.cos_get_object",
        name="cos_get_object",
        description="Built-in object reader.",
        input_params={"type": "object"},
    )
    ability_manager = _AbilityManager()
    ability_manager.add(static_card)
    agent = SimpleNamespace(ability_manager=ability_manager)
    ctx = AgentCallbackContext(agent=agent)

    await rail.before_invoke(ctx)

    assert ability_manager.get("cos_get_object") is static_card
    assert not hasattr(ability_manager, mcp_config._OFFICE_CLAW_TOOL_IDS_ATTR)

    await rail.after_invoke(ctx)

    assert ability_manager.get("cos_get_object") is static_card


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
def test_every_team_member_profile_mounts_request_scoped_tools(
    mode: str, role: str
) -> None:
    rails, _ = build_member_capability_specs({}, mode, role)

    assert registry.REQUEST_SCOPED_MCP_TOOLS in {rail.type for rail in rails}
