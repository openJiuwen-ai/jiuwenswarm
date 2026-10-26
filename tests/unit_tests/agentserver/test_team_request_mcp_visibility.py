# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registration, member assembly, enumeration and real tool execution together."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import ProgressiveToolRail
from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm, registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common import mcp_config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.test_request_scoped_office_claw_mcp import (
    _bare_session_adapter, _ResourceManager,
)


@pytest.fixture(autouse=True)
def _clear_registrations():
    mcp_config._clear_live_office_claw_allowlists_for_tests()
    yield
    mcp_config._clear_live_office_claw_allowlists_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
@pytest.mark.parametrize("source", ["request_mcp_servers", "mcp_server_list"])
@pytest.mark.parametrize("session_id", ["session", None, "", "  "])
async def test_request_tools_are_visible_to_all_llm_members(mode, source, session_id, monkeypatch):
    resource_manager = _ResourceManager()
    resource_manager.get_tool = lambda tool_id, **kwargs: resource_manager.tools.get(tool_id)
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    definitions = [{"name": "connector_lookup", "description": "Lookup", "input_params": {"type": "object"}}]
    connection = {"url": "http://localhost:12345/mcp", "_mcp_client_type": "streamable-http"}
    monkeypatch.setattr(interface_deep, "list_request_mcp_server_tools", AsyncMock(return_value=(definitions, connection)))
    monkeypatch.setattr(interface_deep, "get_mcp_server_registry", lambda: SimpleNamespace(
        snapshot_for_chat=AsyncMock(return_value=[("connector", definitions, connection)]),
    ))
    roles = ["leader", "teammate", "analyst"]
    spec = TeamAgentSpec(
        team_name="mcp_team", leader=LeaderSpec(member_name="team_leader"),
        agents={role: DeepAgentSpec() for role in roles},
        predefined_members=[TeamMemberSpec(
            member_name="analyst", display_name="Analyst", role_type=TeamRole.TEAMMATE,
        )],
    )
    session_key = interface_deep.JiuWenSwarmDeepAdapter._session_adapter_key(session_id)
    enrich_team_spec_for_swarm(spec, session_id=session_key, mode=mode, channel_id="web", config_base={})
    agents = {role: DeepAgent(AgentCard(id="test-" + role, name=role)) for role in ["entry", *roles]}
    rails = {
        role: next(rail for rail in spec.agents[role].rails if rail.type == registry.REQUEST_SCOPED_MCP_TOOLS)
        .build(language="cn", context=SwarmBuildContext(session_id=session_key))
        for role in roles
    }
    contexts = {role: AgentCallbackContext(agent=agents[role]) for role in roles}
    original_execute = {role: agents[role].ability_manager.execute for role in roles}
    adapter = _bare_session_adapter()
    adapter._instance = agents["entry"]
    params = {"query": "lookup"}
    params[source] = ["connector"] if source == "mcp_server_list" else {"mcpServers": {"connector": connection}}
    request = AgentRequest(request_id="request", session_id=session_id, channel_id="web", params=params)
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    try:
        assert registration.tool_names == ("connector_lookup",)
        assert registration.session_id == session_key
        assert mcp_config.get_request_scoped_mcp_registration(session_key) is registration

        async def call_tool(name, *, arguments):
            assert mcp_config.get_active_office_claw_mcp_tool_ids() == frozenset(registration.tool_ids)
            await asyncio.sleep(0)  # Interleave calls sharing ctx.extra.
            if arguments.get("fail"):
                raise RuntimeError("transport failed")
            if arguments.get("cancel"):
                raise asyncio.CancelledError
            return SimpleNamespace(content=[SimpleNamespace(text="found")])

        transport = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(registration.tool_instances[0], "_acquire_mcp_session", AsyncMock(
            return_value=SimpleNamespace(call_tool=transport),
        ))
        for role in roles:
            await rails[role].before_invoke(contexts[role])
            await agents[role].agent_callback_manager.register_rail(rails[role], agents[role])
        discovery = ProgressiveToolRail()
        with mcp_config.bind_active_office_claw_mcp_tools(registration.tool_ids):
            visible = {role: [tool.name for tool in await discovery._get_all_tool_infos(agent)]
                       for role, agent in agents.items()}
        assert visible == {role: ["connector_lookup"] for role in ["entry", *roles]}
        for role, inherited in zip(roles, [("office-claw-request-old.connector_lookup",), (), None]):
            expected = frozenset(inherited) if inherited is not None else None
            async def execute():
                for parallel in (False, True):
                    calls = [ToolCall(id=str(i), type="function", name="connector_lookup", arguments=args)
                             for i, args in enumerate(['{}', '{"fail":true}', '{}'])]
                    results = await agents[role].ability_manager.execute(
                        contexts[role], calls, None, parallel_tool_calls=parallel,
                    )
                    assert results[0][0] == results[2][0] == {"result": "found"}
                    assert "transport failed" in str(results[1])
                    assert mcp_config.get_active_office_claw_mcp_tool_ids() == expected
                cancelled = await agents[role].ability_manager.execute(contexts[role], [ToolCall(
                    id="cancel", type="function", name="connector_lookup", arguments='{"cancel":true}',
                )], None)
                assert "cancelled" in str(cancelled)
                assert mcp_config.get_active_office_claw_mcp_tool_ids() == expected
                card = agents[role].ability_manager.get("connector_lookup")
                card.parallel_safe = False
                try:
                    cancelled = await agents[role].ability_manager.execute(contexts[role], [ToolCall(
                        id="cancel-serial", type="function", name="connector_lookup", arguments='{"cancel":true}',
                    )], None)
                    assert "cancelled" in str(cancelled)
                    assert mcp_config.get_active_office_claw_mcp_tool_ids() == expected
                finally:
                    card.parallel_safe = True

            with mcp_config.bind_active_office_claw_mcp_tools(inherited) if inherited is not None else nullcontext():
                await asyncio.create_task(execute())
        assert transport.await_count == 30  # Three roles: batches with retry and two cancellation paths.
        for role in roles:
            await rails[role].after_invoke(contexts[role])
            assert agents[role].ability_manager.get("connector_lookup") is None
            assert agents[role].ability_manager.execute == original_execute[role]
        assert agents["entry"].ability_manager.get("connector_lookup") is not None
        assert len(resource_manager.tools) == 1
    finally:
        for role in roles:
            await agents[role].agent_callback_manager.unregister_rail(rails[role], agents[role])
        await adapter.cleanup_request_scoped_office_claw_mcp(registration)
    assert resource_manager.tools == {}
    assert mcp_config.get_request_scoped_mcp_registration(session_key) is None


@pytest.mark.asyncio
async def test_failure_after_publication_rolls_back_session_registration(monkeypatch):
    resource_manager = _ResourceManager()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", resource_manager)
    monkeypatch.setattr(interface_deep, "list_request_mcp_server_tools", AsyncMock(return_value=(
        [{"name": "lookup", "description": "Lookup", "input_params": {"type": "object"}}],
        {"url": "http://localhost:12345/mcp", "_mcp_client_type": "streamable-http"},
    )))

    def fail_publication(*args):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(interface_deep, "publish_live_office_claw_allowlist", fail_publication)
    adapter = _bare_session_adapter()
    request = AgentRequest(request_id="failed", session_id="session", channel_id="web", params={
        "query": "lookup", "request_mcp_servers": {"mcpServers": {
            "connector": {"url": "http://localhost:12345/mcp"},
        }},
    })
    registration = await adapter.register_request_scoped_office_claw_mcp(request)
    assert registration.tool_ids == ()
    assert mcp_config.get_request_scoped_mcp_registration("session") is registration
    assert adapter._instance.ability_manager.get("lookup") is None
    assert resource_manager.tools == {}
    await adapter.cleanup_request_scoped_office_claw_mcp(registration)
