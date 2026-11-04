# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent expert-Team model changes are isolated, validated and applied per call."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig, ToolMessage
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.prompts import SystemPromptBuilder

from jiuwenswarm.agents.harness.team.rails.organization_model_rail import OrganizationModelRail
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_errors import ModelSelectionError
from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver
from jiuwenswarm.server.runtime import organization_team_models as models


@pytest.fixture
def model_environment(tmp_path, monkeypatch):
    entries = []
    for model_id, name in (("glm", "GLM-5.3"), ("deepseek", "deepseek-flash")):
        entries.append({
            "model_id": model_id, "alias": name,
            "model_client_config": {
                "model_name": name, "client_provider": "OpenAI",
                "api_base": "https://example.invalid/v1", "api_key": "private-test-key",
            },
            "model_config_obj": {},
        })
    catalog = ModelCatalog({"models": {"defaults": entries, "groups": [], "agentos": []}})
    monkeypatch.setattr(models, "ModelCatalog", lambda: catalog)
    monkeypatch.setattr(models, "ModelSelectionResolver", lambda: ModelSelectionResolver(catalog))
    monkeypatch.setattr(models, "team_home", lambda name: tmp_path / name)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_packages.team_package_availability",
        lambda team_id, group="": {"available": True} if team_id in {"expert", "peer"} else {},
    )
    organization = SimpleNamespace(
        owner_team_id="owner", organization_id="org",
        leaders=[
            SimpleNamespace(team_id=name, leader_id="team-leader", capabilities=[])
            for name in ("owner", "expert", "peer", "summary")
        ],
    )
    rows = [SimpleNamespace(member_name="team-leader", options=None, role="leader")]
    db = SimpleNamespace(member=SimpleNamespace(get_team_members=AsyncMock(return_value=rows)))
    teams = {}
    for name in ("owner", "expert", "peer", "summary"):
        teams[name] = SimpleNamespace(
            current_session_id="session",
            agent=SimpleNamespace(
                spec=SimpleNamespace(metadata={"expert_team": name in {"expert", "peer"}}),
                team_backend=SimpleNamespace(is_leader=True, db=db),
            ),
        )

    async def get_team(team_id):
        return teams.get(team_id)

    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=get_team),
        organization_runtime_manager=SimpleNamespace(get_organization=AsyncMock(return_value=organization)),
    )
    return SimpleNamespace(root=tmp_path, catalog=catalog, runtime=runtime, teams=teams, rows=rows, org=organization)


async def _set(env, *, caller="owner", target="expert", model="deepseek-flash"):
    return await models.set_organization_team_model(
        runtime=env.runtime, caller_team_id=caller, session_id="session",
        organization_id="org", target_team_id=target, model_name=model,
    )


def _agent(name):
    agent = ReActAgent(AgentCard(id=name, name=name))
    agent.set_llm(Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_base="https://example.invalid/v1", api_key="test",
        ),
        model_config=ModelRequestConfig(model="GLM-5.3"),
    ))
    return agent


def _model_name(agent):
    return agent.get_llm().model_config.model_name


@pytest.mark.asyncio
async def test_owner_persists_only_a_credential_free_child_selection(model_environment):
    env = model_environment
    setting = await _set(env)

    assert setting.model_name == "deepseek-flash"
    assert models.load_team_model("expert", "session").selection.id == "deepseek"
    assert models.load_team_model("owner", "session") is None
    assert models.load_team_model("peer", "session") is None
    assert models.load_team_model("expert", "another-session") is None
    raw = models._selection_path("expert").read_text(encoding="utf-8")
    assert "private-test-key" not in raw
    assert "api_key" not in raw


@pytest.mark.asyncio
async def test_leader_existing_new_and_recovered_members_keep_model_until_next_change(model_environment):
    env = model_environment
    await _set(env)
    agents = [_agent(name) for name in ("child-leader", "existing-member", "new-member", "recovered-member")]
    rails = [OrganizationModelRail("expert", "session", "leader") for _ in agents]
    peer = _agent("peer-leader")
    for rail, agent in zip(rails, agents):
        await rail.before_model_call(AgentCallbackContext(agent=agent))
        assert _model_name(agent) == "deepseek-flash"
    await OrganizationModelRail("peer", "session", "leader").before_model_call(AgentCallbackContext(agent=peer))
    assert _model_name(peer) == "GLM-5.3"

    # A fresh rail simulates a cold build; no in-memory selection is needed.
    await OrganizationModelRail("expert", "session", "teammate").before_model_call(
        AgentCallbackContext(agent=agents[-1]),
    )
    assert _model_name(agents[-1]) == "deepseek-flash"
    await _set(env, model="GLM-5.3")
    for rail, agent in zip(rails, agents):
        await rail.before_model_call(AgentCallbackContext(agent=agent))
        assert _model_name(agent) == "GLM-5.3"


@pytest.mark.asyncio
async def test_change_does_not_modify_an_inflight_model_reference(model_environment):
    env = model_environment
    agent = _agent("busy-member")
    inflight = agent.get_llm()
    rail = OrganizationModelRail("expert", "session", "teammate")
    await _set(env)

    assert agent.get_llm() is inflight
    await rail.before_model_call(AgentCallbackContext(agent=agent))
    assert inflight.model_config.model_name == "GLM-5.3"
    assert _model_name(agent) == "deepseek-flash"
    current = agent.get_llm()
    await rail.before_model_call(AgentCallbackContext(agent=agent))
    assert agent.get_llm() is current


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,target", [("peer", "expert"), ("owner", "owner"), ("owner", "unknown"),
                                          ("owner", "summary")])
async def test_switch_rejects_sibling_owner_unknown_and_summary_targets(model_environment, caller, target):
    with pytest.raises(ValueError):
        await _set(model_environment, caller=caller, target=target)
    assert not list(model_environment.root.rglob("organization-model.json"))


@pytest.mark.asyncio
async def test_child_leader_can_change_only_itself(model_environment):
    await _set(model_environment, caller="expert")
    assert models.load_team_model("expert", "session").model_name == "deepseek-flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cross_session", "non_leader", "not_member", "external_cli", "bridge"])
async def test_invalid_execution_boundaries_leave_existing_setting_intact(model_environment, case):
    env = model_environment
    await _set(env)
    if case == "cross_session":
        env.teams["expert"].current_session_id = "other"
    elif case == "non_leader":
        env.teams["owner"].agent.team_backend.is_leader = False
    elif case == "not_member":
        env.org.leaders = [SimpleNamespace(team_id="owner")]
    elif case == "external_cli":
        env.rows.append(SimpleNamespace(member_name="cli", options=json.dumps({"cli_agent": "claudecode"})))
    else:
        env.teams["expert"].agent.team_backend.is_bridge_agent = lambda name: True
    with pytest.raises(ValueError):
        await _set(env, model="GLM-5.3")
    assert models.load_team_model("expert", "session").selection.id == "deepseek"


@pytest.mark.asyncio
async def test_invalid_model_or_write_failure_preserves_previous_selection(model_environment, monkeypatch):
    env = model_environment
    await _set(env)
    with pytest.raises(ValueError):
        await _set(env, model="not-configured")

    def fail_replace(*args):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(models.os, "replace", fail_replace)
    with pytest.raises(OSError):
        await _set(env, model="GLM-5.3")
    assert models.load_team_model("expert", "session").selection.id == "deepseek"
    assert len(list(models._selection_path("expert").parent.iterdir())) == 1


@pytest.mark.asyncio
async def test_duplicate_model_name_is_rejected_but_stable_id_disambiguates(model_environment, monkeypatch):
    env = model_environment
    await _set(env, model="GLM-5.3")
    public = env.catalog.list_public_models()
    duplicate = {**public[1], "model_id": "second-deepseek"}
    monkeypatch.setattr(env.catalog, "list_public_models", lambda: [*public, duplicate])

    with pytest.raises(ValueError, match="exactly one"):
        await _set(env)
    assert models.load_team_model("expert", "session").selection.id == "glm"
    setting = await _set(env, model="deepseek")
    assert setting.selection.id == "deepseek"


@pytest.mark.asyncio
async def test_deleted_selected_model_fails_instead_of_using_default(model_environment):
    env = model_environment
    await _set(env)
    env.catalog.snapshot["by_id"].pop("deepseek")
    agent = _agent("member")
    with pytest.raises(ModelSelectionError):
        await OrganizationModelRail("expert", "session", "teammate").before_model_call(
            AgentCallbackContext(agent=agent),
        )


@pytest.mark.asyncio
async def test_model_rail_supports_setter_only_and_uninitialized_agents(model_environment):
    await _set(model_environment)
    captured = []
    agent = SimpleNamespace(set_llm=captured.append)
    rail = OrganizationModelRail("expert", "session", "teammate")
    await rail.before_model_call(AgentCallbackContext(agent=agent))
    await rail.before_model_call(AgentCallbackContext(agent=agent))
    assert all(model.model_config.model_name == "deepseek-flash" for model in captured)

    def uninitialized():
        raise ValueError("model not initialized")

    agent.get_llm = uninitialized
    await rail.before_model_call(AgentCallbackContext(agent=agent))
    assert len(captured) == 3
    with pytest.raises(ValueError, match="callable set_llm"):
        await rail.before_model_call(AgentCallbackContext(agent=SimpleNamespace(set_llm=None)))


@pytest.mark.asyncio
async def test_saved_model_remains_successful_when_notification_fails(model_environment, monkeypatch):
    from jiuwenswarm.agents.harness.team.expert_org import model_tool

    monkeypatch.setattr(model_tool, "publish_team_model", AsyncMock(side_effect=OSError("offline")))
    output = await model_tool.OrgSetTeamModelTool("owner", "session", model_environment.runtime).invoke({
        "organization_id": "org", "team_id": "expert", "model_name": "deepseek-flash",
    })
    assert output.success
    assert output.data["model_name"] == "deepseek-flash"
    assert models.load_team_model("expert", "session").selection.id == "deepseek"


@pytest.mark.asyncio
async def test_fresh_members_cannot_bypass_team_model_with_cli_spawn(model_environment):
    await _set(model_environment)
    rail = OrganizationModelRail("expert", "session", "leader")
    inputs = ToolCallInputs(tool_name="spawn_external_cli", tool_call=SimpleNamespace(id="call"), tool_args={})
    ctx = AgentCallbackContext(agent=_agent("leader"), inputs=inputs)
    await rail.before_tool_call(ctx)
    assert inputs.tool_result["success"] is False


def test_model_policy_and_capabilities_are_leader_scoped_and_feature_gated():
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
    from jiuwenswarm.agents.swarm.providers.runtime_tools import build_organization_model_tools

    enabled = {"experimental": {"team_organization_ui_enabled": True}}
    for role in ("leader", "teammate"):
        rails, tools = build_member_capability_specs(enabled, "team", role)
        assert any(rail.type == "swarm.organization_model" for rail in rails)
        assert any(tool.type == "swarm.organization_model_tools" for tool in tools) == (role == "leader")
        assert any(rail.type == "swarm.organization_owner_inbox" for rail in rails) == (role == "leader")
        assert any(tool.type == "swarm.organization_owner_inbox_tools" for tool in tools) == (role == "leader")
    rails, tools = build_member_capability_specs({}, "team", "leader")
    assert not any(rail.type == "swarm.organization_model" for rail in rails)
    assert not any(tool.type == "swarm.organization_model_tools" for tool in tools)
    assert not any(rail.type == "swarm.organization_owner_inbox" for rail in rails)
    assert not any(tool.type == "swarm.organization_owner_inbox_tools" for tool in tools)
    assert build_organization_model_tools({}, SimpleNamespace(role="teammate")) == []

    builder = SystemPromptBuilder(language="cn")
    rail = OrganizationModelRail("owner", "session", "leader")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    assert "org_set_team_model" in builder.build()
    rail.uninit(None)
    assert "org_set_team_model" not in builder.build()


@pytest.mark.asyncio
async def test_tool_returns_effective_scope_and_publishes_only_after_success(model_environment, monkeypatch):
    from jiuwenswarm.agents.harness.team.expert_org import model_tool

    publish = AsyncMock()
    monkeypatch.setattr(model_tool, "publish_team_model", publish)
    tool = model_tool.OrgSetTeamModelTool("owner", "session", model_environment.runtime)
    result = await tool.invoke({"organization_id": "org", "team_id": "expert", "model_name": "deepseek-flash"})

    assert result.success
    assert result.data["persistent"] is True
    assert result.data["effective"] == "next_model_call"
    assert result.data["applies_to"] == "leader_and_all_native_members"
    publish.assert_awaited_once()
    result = await tool.invoke({"organization_id": "org", "team_id": "owner", "model_name": "deepseek-flash"})
    assert result.success is False
    assert publish.await_count == 1


@pytest.mark.asyncio
async def test_model_change_event_has_gateway_event_type_and_no_credentials(model_environment, monkeypatch):
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    setting = await _set(model_environment)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(WebSocketGatewayPushTransport, "send_push", push)
    await models.publish_team_model(setting)

    message = push.call_args.args[0]
    assert message["payload"]["event_type"] == "team.model_changed"
    assert message["payload"]["team_id"] == "expert"
    assert message["session_id"] == "session"
    assert "private-test-key" not in json.dumps(build_server_push_wire(message))


@pytest.mark.asyncio
async def test_team_list_recovers_child_model_without_overwriting_owner(model_environment, monkeypatch):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.server import agent_ws_server

    env = model_environment
    await _set(env)
    server = object.__new__(agent_ws_server.AgentWebSocketServer)
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", None, raising=False)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr("jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True)
    manager = SimpleNamespace(
        get_runtime_team_snapshot=lambda: {}, get_active_team_name=lambda _: None, _pending_team_names={},
    )
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _: manager)
    db = SimpleNamespace(team=SimpleNamespace(get_team=AsyncMock(return_value=None)))
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(return_value=(db, env.org)))

    teams = {entry["team_id"]: entry for entry in await server._team_list_entries("session", "web")}

    assert teams["expert"]["execution_model_name"] == "deepseek-flash"
    assert teams["expert"]["execution_model_selection"] == {"type": "model", "id": "deepseek"}
    assert "execution_model_name" not in teams["owner"]
    assert "execution_model_name" not in teams["peer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_shape", ["dict", "tool_output"])
async def test_organization_view_exposes_owner_and_saved_child_model_for_switch_decisions(
    model_environment, result_shape,
):
    from openjiuwen.harness.tools.base_tool import ToolOutput

    await _set(model_environment)
    data = {
        "owner_team_id": "owner",
        "leaders": [{"team_id": name} for name in ("owner", "expert", "peer")],
        "next_action": "existing organization guidance",
    }
    result = {"success": True, "data": data}
    if result_shape == "tool_output":
        result = ToolOutput(success=True, data=data)
    message = ToolMessage(content="old result", tool_call_id="view")
    inputs = ToolCallInputs(tool_name="org_view_organization", tool_result=result, tool_msg=message)
    rail = OrganizationModelRail("owner", "session", "leader")

    await rail.after_tool_call(AgentCallbackContext(agent=_agent("owner"), inputs=inputs))

    rendered = json.loads(message.content)["data"]
    owner, expert, peer = rendered["leaders"]
    assert owner["is_owner"] is True
    assert "execution_model_selection" not in owner
    assert expert["is_owner"] is False
    assert expert["execution_model_selection"] == {"type": "model", "id": "deepseek"}
    assert expert["execution_model_name"] == "deepseek-flash"
    assert "execution_model_selection" not in peer
    assert rendered["next_action"] == "existing organization guidance"
    assert "Never call org_set_team_model for owner_team_id" in rendered["model_selection_guidance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
async def test_child_leader_receives_existing_persistent_selection_before_deciding_to_switch(
    model_environment, language,
):
    await _set(model_environment)
    builder = SystemPromptBuilder(language=language)
    rail = OrganizationModelRail("expert", "session", "leader")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    agent = _agent("child-leader")
    ctx = AgentCallbackContext(agent=agent)

    await rail.before_model_call(ctx)
    await rail.before_model_call(ctx)

    assert _model_name(agent) == "deepseek-flash"
    assert '"persistent_team_model": "deepseek-flash"' in builder.build()
    assert '"id": "deepseek"' in builder.build()
    assert "Reuse it unless a different model is requested" in builder.build()
    rail.uninit(None)
    assert "persistent_team_model" not in builder.build()


@pytest.mark.asyncio
async def test_owner_matching_model_is_rejected_with_guidance_without_touching_child(model_environment):
    env = model_environment
    await _set(env)

    with pytest.raises(ValueError, match="Owner Team.*proceed without switching"):
        await _set(env, target="owner", model="GLM-5.3")

    assert models.load_team_model("owner", "session") is None
    assert models.load_team_model("expert", "session").model_name == "deepseek-flash"
