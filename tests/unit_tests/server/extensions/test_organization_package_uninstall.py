# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Uninstall disables organization instances without deleting membership or history."""

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams.runtime import TeamRuntimeManager
from openjiuwen.agent_teams.runtime.dispatch import RunActionKind
from openjiuwen.agent_teams.runtime.pool import ActiveTeam, RuntimeState
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.runner.runner import GLOBAL_RUNNER
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.agents.harness.team.rails.organization_availability_rail import OrganizationAvailabilityRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime import extension_package_manager as packages
from jiuwenswarm.server.runtime import team_organization_identity_store as identities
from jiuwenswarm.server.runtime.organization_packages import team_package_availability, uninstall_organization_agent_group


@pytest.fixture
def package_workspace(extension_workspace, monkeypatch, tmp_path):
    monkeypatch.setattr(identities, "team_home", lambda name: tmp_path / "teams" / name)
    for group in ("group-a", "group-b"):
        package = extension_workspace.parent.parent / ".agent_teams" / "agent_groups" / "local" / group
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(json.dumps({
            "name": group, "package_type": "agent_group", "agents": ["leader"],
        }), encoding="utf-8")
        packages.upsert_agent_group_marketplace_entry(group, installed=True, source="local")
    for team, group in (("expert-a", "group-a"), ("expert-a2", "group-a"), ("expert-b", "group-b")):
        identities.save_organization_team_identity(team, agent_group_name=group, display_name=group)
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_identity", [False, True])
async def test_uninstall_stops_matching_instances_across_sessions_and_preserves_history(
    package_workspace, monkeypatch, metadata_identity,
):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    runtime = TeamRuntimeManager()
    organization = runtime.organization_runtime_manager
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    runtime.finalize = AsyncMock()
    agents = {}
    started = {}
    tasks = []

    async def activate(spec, session_id, inputs):
        return SimpleNamespace(agent=agents[spec.name], action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
                               session=SimpleNamespace(post_run=AsyncMock()))

    runtime.activate = activate
    for name, group, session_id in (("expert-a", "group-a", "s1"), ("expert-a2", "group-a", "s2"), ("expert-b", "group-b", "s1")):
        started[name] = asyncio.Event()

        async def stream(inputs, session, team=name):
            started[team].set()
            await asyncio.Event().wait()
            yield None

        metadata = {"expert_team": True, "channel_id": "web"}
        if metadata_identity:
            metadata["agent_group_name"] = group
        agent = SimpleNamespace(
            spec=SimpleNamespace(name=name, metadata=metadata),
            team_backend=SimpleNamespace(org_task_manager=SimpleNamespace(organization_id="org")),
            stream=stream, stop_coordination=AsyncMock(),
        )
        agents[name] = agent
        await runtime.pool.add(ActiveTeam(name, agent, session_id, RuntimeState.PAUSED))
        organization._team_organizations[(session_id, name)] = "org"
        organization._leader_turn_queues[(session_id, name)] = deque([{"query": "queued"}])
        launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime, push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)))
        task = asyncio.create_task(launcher.run_organization_turn(name, session_id, {}, request_id=name))
        tasks.append(task)
        organization._leader_turn_workers[(session_id, name)] = task
    await asyncio.gather(*(event.wait() for event in started.values()))
    history = package_workspace / "history.jsonl"
    history.write_text('{"content":"saved answer"}\n', encoding="utf-8")
    membership = package_workspace / "organization.json"
    membership.write_text('{"leaders":["expert-a","expert-b"]}', encoding="utf-8")
    try:
        request = AgentRequest(request_id="uninstall", channel_id="web", req_method=ReqMethod.AGENT_GROUPS_UNINSTALL, params={"id": "group-a"})
        response = await JiuWenSwarm._handle_package_catalog_request(None, request)
        assert response.ok
        assert not packages.is_agent_group_installed("group-a")
        assert packages.is_agent_group_installed("group-b")
        assert tasks[0].cancelled() and tasks[1].cancelled()
        assert not tasks[2].done()
        for name, session_id in (("expert-a", "s1"), ("expert-a2", "s2")):
            assert await runtime.pool.get(name) is None
            assert (session_id, name) not in organization._leader_turn_queues
            agents[name].stop_coordination.assert_awaited_once()
        agents["expert-b"].stop_coordination.assert_not_awaited()
        assert history.read_text(encoding="utf-8") == '{"content":"saved answer"}\n'
        assert json.loads(membership.read_text(encoding="utf-8"))["leaders"] == ["expert-a", "expert-b"]
        assert identities.load_organization_team_identity("expert-a")["agent_group_name"] == "group-a"
        assert team_package_availability("expert-a")["available"] is False
        assert team_package_availability("expert-b")["available"] is True

        server = object.__new__(AgentWebSocketServer)
        monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
        monkeypatch.setattr("jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True)
        recover = AsyncMock()
        monkeypatch.setattr(server, "_load_persisted_organization", recover)
        chunks = [chunk async for chunk in server._targeted_expert_response_stream(AgentRequest(
            request_id="chat", channel_id="web", session_id="s1", req_method=ReqMethod.CHAT_SEND,
            params={"target_team_id": "expert-a", "mode": "team", "query": "continue"},
        ))]
        assert chunks[0].payload["code"] == "AGENT_GROUP_NOT_INSTALLED"
        assert chunks[-1].payload["is_processing"] is False
        recover.assert_not_awaited()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await organization.close()


@pytest.mark.asyncio
async def test_failed_uninstall_does_not_stop_live_teams(package_workspace, monkeypatch):
    runtime = SimpleNamespace(pool=SimpleNamespace(list_all_info=AsyncMock()), stop_team=AsyncMock())
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    monkeypatch.setattr(packages, "uninstall_agent_group", Mock(side_effect=PermissionError("locked")))
    with pytest.raises(PermissionError, match="locked"):
        await uninstall_organization_agent_group({"id": "group-a"})
    runtime.stop_team.assert_not_awaited()
    runtime.pool.list_all_info.assert_not_awaited()
    assert team_package_availability("expert-a")["available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dictionary", [False, True])
async def test_roster_model_prompt_and_delegation_agree_after_uninstall(package_workspace, monkeypatch, dictionary):
    packages.uninstall_agent_group({"id": "group-a"})
    rail = OrganizationAvailabilityRail("owner", "session")
    data = {"leaders": [{"team_id": "expert-a"}, {"team_id": "expert-b"}], "next_action": "leaders can claim tasks"}
    result = {"success": True, "data": data} if dictionary else ToolOutput(success=True, data=data)
    inputs = ToolCallInputs(tool_name="org_view_organization", tool_result=result, tool_msg=ToolMessage(content="old", tool_call_id="call"))
    await rail.after_tool_call(SimpleNamespace(inputs=inputs, exception=None))
    assert data["leaders"][0]["available"] is False
    assert data["leaders"][1]["available"] is True
    assert "available=false" in data["next_action"]
    assert json.loads(inputs.tool_msg.content)["data"] == data

    builder = SimpleNamespace(remove_section=Mock(), add_section=Mock())
    manager = SimpleNamespace(get_organization=AsyncMock(return_value=SimpleNamespace(leaders=[SimpleNamespace(team_id="expert-a")])))
    entry = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(team_backend=SimpleNamespace(org_task_manager=manager)))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=entry))), raising=False)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    await rail.before_model_call(SimpleNamespace())
    assert "expert-a" in builder.add_section.call_args.args[0].content["en"]
    assert "unavailable" in builder.add_section.call_args.args[0].content["en"]

    inputs = ToolCallInputs(tool_name="org_delegate_task", tool_args={"to_team_id": "expert-a"}, tool_call=SimpleNamespace(id="delegate"))
    ctx = SimpleNamespace(inputs=inputs, extra={})
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True
    assert inputs.tool_result["success"] is False
    assert inputs.tool_msg.tool_call_id == "delegate"
    # Reinstallation changes availability; no permanent tombstone blocks use.
    package = package_workspace / "reinstalled"
    package.mkdir(exist_ok=True)
    monkeypatch.setattr(packages, "resolve_agent_group_dir", lambda _: package)
    assert team_package_availability("expert-a")["available"] is True
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_name="org_delegate_task", tool_args={"to_team_id": "expert-a"}), extra={})
    await rail.before_tool_call(ctx)
    assert "_skip_tool" not in ctx.extra
    builder.add_section.reset_mock()
    await rail.before_model_call(SimpleNamespace())
    notice = builder.add_section.call_args.args[0].content["en"]
    assert '"available": true' in notice
    assert '"available": false' not in notice
    rail.uninit(None)


def test_unknown_package_identity_does_not_disable_ordinary_teams(package_workspace):
    assert team_package_availability("ordinary-team") == {}


@pytest.mark.asyncio
async def test_uninstall_during_activation_prevents_the_model_stream_from_starting(package_workspace):
    started = Mock()

    async def stream(inputs, session):
        started()
        yield None

    agent = SimpleNamespace(spec=SimpleNamespace(metadata={"expert_team": True, "agent_group_name": "group-a", "channel_id": "web"}), stream=stream)
    entry = SimpleNamespace(agent=agent, current_session_id="session", state=RuntimeState.PAUSED)

    async def activate(spec, session_id, inputs):
        packages.uninstall_agent_group({"id": "group-a"})
        return SimpleNamespace(agent=agent, action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE), session=SimpleNamespace(post_run=AsyncMock()))

    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=entry)), activate=activate,
                              stop_team=AsyncMock(), finalize=AsyncMock())
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime, push_transport=SimpleNamespace(send_push=AsyncMock(return_value=True)))
    assert not await launcher.run_organization_turn("expert-a", "session", {})
    started.assert_not_called()
    runtime.stop_team.assert_awaited_once_with(team_name="expert-a", session_id="session")


@pytest.mark.asyncio
async def test_historical_roster_retains_removed_expert_but_marks_it_unavailable(package_workspace, monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", None, raising=False)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr("jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True)
    manager = SimpleNamespace(get_runtime_team_snapshot=lambda: {}, get_active_team_name=lambda _: None, _pending_team_names={})
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _: manager)
    leaders = [SimpleNamespace(team_id=team, leader_id="leader", capabilities=[]) for team in ("expert-a", "expert-b")]
    organization = SimpleNamespace(leaders=leaders, organization_id="org")
    db = SimpleNamespace(team=SimpleNamespace(get_team=AsyncMock(return_value=SimpleNamespace(display_name="saved expert"))))
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(return_value=(db, organization)))
    packages.uninstall_agent_group({"id": "group-a"})
    teams = {team["team_id"]: team for team in await server._team_list_entries("session", "web")}
    assert teams["expert-a"]["available"] is False
    assert teams["expert-a"]["unavailable_reason"] == "AGENT_GROUP_NOT_INSTALLED"
    assert teams["expert-b"]["available"] is True
    assert teams["expert-a"]["organization_id"] == "org"
    assert teams["expert-a"]["display_name"] == "saved expert"


@pytest.mark.parametrize("enabled,role", [(True, "leader"), (False, "leader"), (True, "teammate")])
def test_availability_rail_is_built_only_for_organization_leaders(enabled, role):
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs
    from jiuwenswarm.agents.swarm.providers.member_rails import _build_organization_availability_rail

    rails, _ = build_member_capability_specs({"experimental": {"team_organization_ui_enabled": enabled}}, "team", role)
    matching = [rail for rail in rails if rail.type == "swarm.organization_availability"]
    assert bool(matching) is (enabled and role == "leader")
    if matching:
        rail = _build_organization_availability_rail({}, SimpleNamespace(team_id="owner", session_id="session"))
        assert isinstance(rail, OrganizationAvailabilityRail)
        assert (rail.team_id, rail.session_id) == ("owner", "session")


@pytest.mark.parametrize("error,reason", [(RuntimeError("manifest damaged"), "AGENT_GROUP_UNAVAILABLE"),
                                          (ValueError("missing"), "AGENT_GROUP_UNINSTALLED")])
def test_availability_probe_contains_resolver_failures(monkeypatch, error, reason):
    warning = Mock()
    monkeypatch.setattr("jiuwenswarm.server.runtime.organization_packages.logger.warning", warning)
    monkeypatch.setattr(packages, "resolve_agent_group_dir", Mock(side_effect=error))
    error.code = "AGENT_GROUP_UNINSTALLED"
    result = team_package_availability("expert", "group")
    assert result["available"] is False
    assert result["unavailable_reason"] == reason
    if isinstance(error, RuntimeError):
        warning.assert_called_once()


@pytest.mark.asyncio
async def test_roster_accepts_null_leaders():
    data = {"leaders": None}
    inputs = ToolCallInputs(tool_name="org_view_organization", tool_result={"success": True, "data": data})
    await OrganizationAvailabilityRail("owner", "session").after_tool_call(SimpleNamespace(inputs=inputs, exception=None))
    assert "available=false" in data["next_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("identity_source", ["persisted", "runtime"])
async def test_model_json_delegation_uses_real_ability_callbacks_and_database(
    package_workspace, monkeypatch, available, identity_source,
):
    from openjiuwen.agent_teams.organization.schema import OrgTaskCreator
    from openjiuwen.agent_teams.organization.task_pool import OrgTaskManager
    from openjiuwen.agent_teams.organization.tools import OrgDelegateTaskTool
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

    if not available:
        packages.uninstall_agent_group({"id": "group-a"})
    if identity_source == "runtime":
        monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {})
        entry = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(
            spec=SimpleNamespace(metadata={"expert_team": True, "agent_group_name": "group-a"}),
        ))
        runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=entry)))
        monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=":memory:"))
    manager = OrgTaskManager(db=db, organization_id="org", session_id="session")
    ability = AbilityManager(owner_id="availability-test")
    callbacks = AgentCallbackManager("availability-test")
    agent = SimpleNamespace(agent_callback_manager=callbacks, ability_manager=ability)
    rail = OrganizationAvailabilityRail("owner", "session")
    await callbacks.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, rail.before_tool_call)
    try:
        for team in ("owner", "expert-a"):
            await manager.register_leader(team_id=team, leader_id=f"leader-{team}", capabilities=["analysis"])
        client = OrgTaskCreator(creator_type="client", creator_id="client", organization_id="org")
        leader = OrgTaskCreator(
            creator_type="team_leader", creator_id="leader-owner", organization_id="org", team_id="owner",
        )
        parent = await manager.create_task(
            task_id="root", title="root", description="root", required_capabilities=["analysis"], created_by=client,
        )
        assert parent.ok
        assert (await manager.claim_task(task_id="root", team_id="owner")).ok
        assert (await manager.set_root_aggregation_mode(
            task_id="root", team_id="owner", leader_id="leader-owner", aggregation_mode="HIERARCHICAL",
        )).ok
        assert (await manager.start_task(task_id="root", team_id="owner")).ok
        child = await manager.create_task(
            task_id="child", parent_task_id="root", title="child", description="child",
            required_capabilities=["analysis"], created_by=leader,
        )
        assert child.ok
        assert (await manager.claim_task(task_id="child", team_id="owner")).ok
        before = await manager.get_task("child")
        tool = OrgDelegateTaskTool(manager, "owner", "leader-owner")
        ability.add_ability(tool.card, tool)
        call = ToolCall(
            id="delegate", type="function", name="org_delegate_task",
            arguments=json.dumps({"task_id": "child", "to_team_id": "expert-a"}),
        )
        session = SimpleNamespace(get_session_id=lambda: "session")
        results = await ability.execute(
            AgentCallbackContext(agent=agent, session=session, extra={}), call, session,
            parallel_tool_calls=False,
        )
        result, message = results[0]
        success = result.get("success") if isinstance(result, dict) else result.success
        assert success is available
        after = await manager.get_task("child")
        if available:
            assert after.assignment.team_id == "expert-a"
        else:
            assert "unavailable" in message.content
            assert after == before
    finally:
        ability.remove_ability("org_delegate_task")
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{", "[]", "null", "42", [], None])
async def test_availability_rail_leaves_invalid_arguments_for_tool_validation(arguments):
    rail = OrganizationAvailabilityRail("owner", "session")
    ctx = SimpleNamespace(inputs=ToolCallInputs(tool_name="org_delegate_task", tool_args=arguments), extra={})
    await rail.before_tool_call(ctx)
    assert "_skip_tool" not in ctx.extra


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,blocked", [
    ({}, False), ({"summary_team": True}, False), ({"expert_team": True}, True),
    ({"expert_team": True, "agent_group_name": "group-a"}, True),
])
async def test_package_identity_has_one_policy_for_prompt_roster_and_dispatch(
    package_workspace, monkeypatch, metadata, blocked,
):
    packages.uninstall_agent_group({"id": "group-a"})
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {})
    manager = SimpleNamespace(get_organization=AsyncMock(return_value=SimpleNamespace(
        leaders=[SimpleNamespace(team_id="target")],
    )))
    entry = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(
        spec=SimpleNamespace(metadata=metadata),
        team_backend=SimpleNamespace(org_task_manager=manager),
    ))
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=entry)))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    rail = OrganizationAvailabilityRail("owner", "session")
    builder = SimpleNamespace(remove_section=Mock(), add_section=Mock())
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    await rail.before_model_call(SimpleNamespace())
    assert builder.add_section.called is blocked
    if blocked:
        notice = builder.add_section.call_args.args[0].content["en"]
        assert '"available": false' in notice
        if not metadata.get("agent_group_name"):
            assert "AGENT_GROUP_IDENTITY_UNKNOWN" in notice
    data = {"leaders": [{"team_id": "target"}]}
    await rail.after_tool_call(SimpleNamespace(exception=None, inputs=ToolCallInputs(
        tool_name="org_view_organization", tool_result={"success": True, "data": data},
    )))
    leader = data["leaders"][0]
    assert ("available" in leader) is blocked
    for tool_name, field in [
        ("org_delegate_task", "to_team_id"), ("org_create_task", "delegated_to_team_id"),
        ("org_send_leader_message", "to_team_id"), ("org_invite_team", "team_id"),
        ("org_activate_and_invite_team", "team_id"),
    ]:
        ctx = SimpleNamespace(extra={}, inputs=ToolCallInputs(
            tool_name=tool_name, tool_args=json.dumps({field: "target"}),
        ))
        await rail.before_tool_call(ctx)
        assert bool(ctx.extra.get("_skip_tool")) is blocked
        if blocked:
            assert ctx.inputs.tool_result["success"] is False
            assert leader["available"] is False


@pytest.mark.asyncio
async def test_roster_package_name_resolves_availability_without_persisted_identity(package_workspace, monkeypatch):
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {})
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", None, raising=False)
    packages.uninstall_agent_group({"id": "group-a"})
    rail = OrganizationAvailabilityRail("owner", "session")
    data = {"leaders": [{"team_id": "target", "agent_group_name": "group-a"}]}
    await rail.after_tool_call(SimpleNamespace(exception=None, inputs=ToolCallInputs(
        tool_name="org_view_organization", tool_result={"success": True, "data": data},
    )))
    assert data["leaders"][0]["available"] is False
    ctx = SimpleNamespace(extra={}, inputs=ToolCallInputs(
        tool_name="org_delegate_task", tool_args=json.dumps({"to_team_id": "target"}),
    ))
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True
    # Reinstallation must be observed without keeping a stale availability snapshot.
    restored = package_workspace / "restored-group"
    restored.mkdir()
    monkeypatch.setattr(packages, "resolve_agent_group_dir", lambda _: restored)
    ctx.extra.clear()
    await rail.before_tool_call(ctx)
    assert "_skip_tool" not in ctx.extra


@pytest.mark.asyncio
async def test_uninstall_teardown_failure_does_not_fail_rpc_or_skip_other_teams(package_workspace, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.organization_execution import is_stopping

    warning = Mock()
    monkeypatch.setattr("jiuwenswarm.server.runtime.organization_packages.logger.warning", warning)
    entries = {}
    for name, session in [("first", "s1"), ("second", "s1"), ("third", "s2")]:
        entries[name] = SimpleNamespace(current_session_id=session, agent=SimpleNamespace(
            spec=SimpleNamespace(metadata={"expert_team": True, "agent_group_name": "group-a"}),
        ))
    attempted = []

    async def stop(*, team_name, session_id):
        assert is_stopping(session_id, team_name)
        attempted.append((session_id, team_name))
        if len(attempted) == 1:
            raise RuntimeError("teardown failed")

    runtime = SimpleNamespace(
        pool=SimpleNamespace(
            list_all_info=AsyncMock(return_value=[SimpleNamespace(team_name=name) for name in entries]),
            get=AsyncMock(side_effect=lambda name: entries.get(name)),
        ),
        stop_team=AsyncMock(side_effect=stop),
    )
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    request = AgentRequest(
        request_id="uninstall", channel_id="web", req_method=ReqMethod.AGENT_GROUPS_UNINSTALL,
        params={"id": "group-a"},
    )
    response = await JiuWenSwarm._handle_package_catalog_request(None, request)
    assert response.ok
    assert not packages.is_agent_group_installed("group-a")
    assert set(attempted) == {("s1", "first"), ("s1", "second"), ("s2", "third")}
    warning.assert_called_once()
    assert str(warning.call_args.args[-1]) == "teardown failed"
    for session_id, team_name in attempted:
        assert not is_stopping(session_id, team_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["namespace", "dataclass", "broken_dump"])
async def test_roster_serialization_accepts_duck_results_and_preserves_message_on_failure(monkeypatch, kind):
    from dataclasses import dataclass

    @dataclass
    class Result:
        success: bool
        data: dict

    data = {"leaders": [{"team_id": "expert"}]}
    result = Result(True, data) if kind == "dataclass" else SimpleNamespace(success=True, data=data)
    if kind == "broken_dump":
        result.model_dump = Mock(side_effect=ValueError("cannot dump"))
    rail = OrganizationAvailabilityRail("owner", "session")
    monkeypatch.setattr(rail, "_team_availability", AsyncMock(return_value={"available": False}))
    warning = Mock()
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.rails.organization_availability_rail.logger.warning", warning)
    message = ToolMessage(content="original tool message", tool_call_id="roster")
    await rail.after_tool_call(SimpleNamespace(exception=None, inputs=ToolCallInputs(
        tool_name="org_view_organization", tool_result=result, tool_msg=message,
    )))
    assert data["leaders"][0]["available"] is False
    if kind == "broken_dump":
        assert message.content == "original tool message"
        warning.assert_called_once()
    else:
        assert json.loads(message.content)["data"] == data
        warning.assert_not_called()
