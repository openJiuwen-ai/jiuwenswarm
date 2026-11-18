# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Hot-reload observers must not retain a retired generation's real context."""
import gc
import weakref

import pytest
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm import UserMessage
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard

from jiuwenswarm.agents.harness.team.team_manager import (
    MemberInfo, RuntimeInfo, TeamManager, TeamRailMountContext, TeamWorkspaceInfo,
)


class Member:
    pass


class Rail:
    def __init__(self, agent):
        self.agent = agent


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["leader", "teammate"])
async def test_recycled_member_contexts_are_not_owned_by_hot_reload_indexes(role):
    manager = TeamManager()
    references = []
    for generation in range(8):
        session = create_agent_session(session_id="memory-session", card=AgentCard(id="member", name="member"))
        agent = Member()
        agent.context_engine = ContextEngine()
        context = await agent.context_engine.create_context(session=session, history_messages=[
            UserMessage(content=str(generation) + "x" * (256 * 1024))])
        rail = Rail(agent)
        agent.rails = [rail]
        mount = TeamRailMountContext(agent=agent, member_info=MemberInfo(role=role),
            runtime=RuntimeInfo(channel="web"), team_workspace=TeamWorkspaceInfo(team_id="memory-team"))
        manager.register_team_live_rail("memory-session", agent, rail)
        if role == "leader":
            manager.register_team_rail_context("memory-session", mount)
            manager.register_team_skill_rail("memory-session", rail)
            manager.register_team_skill_create_rail("memory-session", rail)
        else:
            manager.register_team_member_rail_context("memory-session", mount)
            manager.register_team_member_skill_evolution_rail("memory-session", rail)
        references.extend([weakref.ref(agent), weakref.ref(rail), weakref.ref(context)])
        # Model a retired harness dropping its ownership, including cycles
        # between its rails and native agent. Core context objects are real.
        del agent, rail, context, session, mount
        gc.collect()
        assert all(reference() is None for reference in references)
    assert not list(manager._team_live_rails["memory-session"])
    assert manager.get_team_rail_context("memory-session") is None


@pytest.mark.asyncio
async def test_live_harness_ownership_keeps_rail_and_context_available():
    manager = TeamManager()
    owner = Member()
    owner.context_engine = ContextEngine()
    session = create_agent_session(session_id="live-memory", card=AgentCard(id="member", name="member"))
    context = await owner.context_engine.create_context(session=session, history_messages=[UserMessage(content="live")])
    rail = Rail(owner)
    owner.rails = [rail]
    manager.register_team_live_rail("live-memory", owner, rail)
    manager.register_team_member_skill_evolution_rail("live-memory", rail)
    reference = weakref.ref(context)
    del context, rail, session
    gc.collect()
    assert reference() is not None
    assert list(manager._team_live_rails["live-memory"]) == [(owner, owner.rails[0])]
    assert list(manager._team_member_skill_evolution_rails["live-memory"]) == owner.rails
    del owner
    gc.collect()
    assert reference() is None
    assert not list(manager._team_live_rails["live-memory"])
