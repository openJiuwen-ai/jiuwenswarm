# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real SQLite -> closed connection -> fresh server historical RPC queries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlmodel import select

from openjiuwen.agent_teams.organization import pool as org_pool
from openjiuwen.agent_teams.organization.manager import TeamOrganizationManager
from openjiuwen.agent_teams.organization.schema import OrgTaskCreator, OrgTaskRecord
from openjiuwen.agent_teams.spawn import shared_resources
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime import team_organization_history as history
from jiuwenswarm.server.runtime import team_organization_identity_store as identity_store
from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def restarted_server(monkeypatch, tmp_path):
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": True}})
    monkeypatch.setattr(shared_resources, "_db_instances", {})
    monkeypatch.setattr(org_pool, "_MANAGERS", {})
    db_path = tmp_path / "saved-team.db"
    config = DatabaseConfig(connection_string=str(db_path))
    db = TeamDatabase(config)
    await db.initialize()
    manager = TeamOrganizationManager(organization_id="org", db=db, session_id="session")
    await manager.initialize(metadata={"owner_team_id": "owner", "owner_leader_id": "leader-owner"})
    names = ("owner", "tech", "architecture", "org-summary-org")
    displays = {"tech": "技术洞察专家团", "architecture": "代码架构设计专家团"}
    try:
        for name in names:
            await db.team.create_team(name, displays.get(name, f"Display {name}"), f"leader-{name}")
            await manager.register_leader(team_id=name, leader_id=f"leader-{name}", capabilities=["analysis"])
        creator = OrgTaskCreator(creator_type="client", creator_id="client", organization_id="org")
        for name in names:
            result = await manager.task_pool.create_task(
                task_id=f"task-{name}", title=f"Result {name}", description="Brief analysis",
                created_by=creator, required_capabilities=["analysis"],
            )
            assert result.ok
        # Seed terminal records: this integration exercises history queries,
        # not LLM scheduling or Summary Team execution.
        sessions = await manager.db_context.initialize()
        async with sessions.write() as session:
            for row in (await session.execute(select(OrgTaskRecord))).scalars().all():
                row.status = "COMPLETED"
                row.output_context_json = json.dumps({"description": f"Saved output {row.task_id}"})
                row.output_abstract = "Accepted result"
            await session.commit()
        foreign = TeamOrganizationManager(organization_id="foreign-org", db=db, session_id="another-session")
        await foreign.initialize(metadata={"owner_team_id": "foreign"})
        await foreign.register_leader(team_id="foreign", leader_id="foreign-leader", capabilities=["analysis"])
    finally:
        await db.close()

    entities = TeamEntityStore(tmp_path / "teams")
    monkeypatch.setattr(identity_store, "team_home", lambda name: tmp_path / "teams" / name)
    identity_store.save_organization_team_identity(
        "tech", agent_group_name="technology-insight", display_name="技术洞察专家团"
    )
    entities.write(
        team_name="owner", template_id="business",
        template_snapshot={"storage": {"type": "sqlite", "params": {"connection_string": str(db_path)}}},
    )
    metadata = {"team_name": "owner"}
    monkeypatch.setattr(history, "get_team_entity_store", lambda: entities)
    monkeypatch.setattr(history, "get_session_metadata", lambda _: metadata)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: metadata)
    # No live runtimes survive the restart. Any accidental activation fails.
    runtime = SimpleNamespace(
        pool=SimpleNamespace(_teams={}, teams_for_session=AsyncMock(return_value=[]), get=AsyncMock(return_value=None)),
        activate=AsyncMock(side_effect=AssertionError("history query activated a Team")),
    )
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER._team_runtime_manager", runtime)
    team_manager = SimpleNamespace(
        get_runtime_team_snapshot=lambda: {}, get_active_team_name=lambda _: None, _pending_team_names={},
    )
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _: team_manager)
    monkeypatch.setattr(agent_ws_server, "encode_agent_response_for_wire", lambda response, **_: response)
    sent = []

    async def send(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
    try:
        yield object.__new__(AgentWebSocketServer), sent, runtime, metadata
    finally:
        for reopened in shared_resources._db_instances.values():
            await reopened.close()


def query(method, team="owner", **extra):
    return AgentRequest(
        request_id="history", channel_id="web", session_id="session", req_method=method,
        params={"session_id": "session", "team_id": team, **extra},
    )


async def test_restart_lists_all_teams_and_reads_completed_tasks(restarted_server):
    server, sent, runtime, _ = restarted_server
    await server._handle_team_list(None, query("team.list"), asyncio.Lock())
    teams = {team["team_id"]: team for team in sent[-1].payload["teams"]}
    assert set(teams) == {"owner", "tech", "architecture", "org-summary-org"}
    assert sent[-1].payload["default_team_id"] == "owner"
    assert all(team["organization_id"] == "org" for team in teams.values())
    assert all(team["state"] == "configured" for team in teams.values())
    assert teams["tech"]["display_name"] == "技术洞察专家团"
    assert teams["tech"]["agent_group_name"] == "technology-insight"
    # Legacy teams have no identity sidecar; their saved readable name remains available.
    assert teams["architecture"]["display_name"] == "代码架构设计专家团"
    assert teams["architecture"]["agent_group_name"] is None
    for team_id in teams:
        await server._handle_org_snapshot(None, query("org.snapshot", team_id), asyncio.Lock())
        snapshot = sent[-1].payload
        assert snapshot["organization"]["organization_id"] == "org"
        assert {leader["team_id"] for leader in snapshot["organization"]["leaders"]} == set(teams)
        assert snapshot["stats"]["by_status"] == {"COMPLETED": 4}
        assert len(snapshot["tasks"]) == 4
        assert {task["title"] for task in snapshot["tasks"]} == {
            f"Result {name}" for name in teams
        }
    db, _ = await server._load_persisted_organization("session")
    reader = TeamOrganizationManager(organization_id="org", db=db, session_id="session")
    saved_tasks = await reader.task_pool.list_tasks()
    assert {task.output_abstract for task in saved_tasks} == {"Accepted result"}
    runtime.activate.assert_not_awaited()
    assert runtime.pool._teams == {}


async def test_restart_snapshot_does_not_populate_runtime_manager_cache(restarted_server):
    server, sent, runtime, _ = restarted_server
    assert org_pool._MANAGERS == {}
    for team_id in ("owner", "tech", "architecture"):
        await server._handle_org_snapshot(None, query("org.snapshot", team_id), asyncio.Lock())
        assert sent[-1].payload["organization"]["organization_id"] == "org"
        assert org_pool._MANAGERS == {}
    runtime.activate.assert_not_awaited()


@pytest.mark.parametrize("first_team", ["tech", "architecture"])
async def test_snapshot_then_restore_teams_delivers_messages_and_task_events(
    restarted_server, monkeypatch, tmp_path, first_team
):
    from openjiuwen.agent_teams.messager.base import MessagerTransportConfig
    from openjiuwen.agent_teams.messager.inprocess import InProcessMessager
    from openjiuwen.agent_teams.organization.runtime import OrganizationRuntimeManager
    from openjiuwen.agent_teams.organization import workspace

    server, sent, runtime, _ = restarted_server
    await server._handle_org_snapshot(None, query("org.snapshot", first_team), asyncio.Lock())
    assert sent[-1].payload["stats"]["by_status"] == {"COMPLETED": 4}
    db, _ = await server._load_persisted_organization("session", first_team)
    workspaces = workspace.OrganizationWorkspaceManager(
        organization_id="org", session_id="session",
        config=workspace.OrganizationWorkspaceConfig(root_path=str(tmp_path / "organization"), version_control=False),
    )
    monkeypatch.setattr(workspace, "get_organization_workspace_manager", lambda *args: workspaces)
    org_runtime = OrganizationRuntimeManager(runtime)
    # Substitute LLM scheduling only; binding, subscriptions, SQLite and transport are real.
    claim_turn = Mock()
    message_turn = Mock()
    monkeypatch.setattr(org_runtime, "_schedule_claim_turn", claim_turn)
    monkeypatch.setattr(org_runtime, "_schedule_leader_message_turn", message_turn)
    second_team = "architecture" if first_team == "tech" else "tech"
    agents = {}
    try:
        for team_id in (first_team, second_team):
            backend = SimpleNamespace(
                db=db, team_name=team_id, member_name=f"leader-{team_id}",
                leader_member_name=f"leader-{team_id}", is_leader=True,
                messager=InProcessMessager(config=MessagerTransportConfig(node_id=f"leader-{team_id}")),
            )
            tools = []
            agent = SimpleNamespace(
                team_backend=backend, member_name=backend.member_name,
                spec=SimpleNamespace(enable_organization=True, metadata={"capabilities": ["analysis"]}),
                harness=SimpleNamespace(add_tool=tools.append),
            )
            agents[team_id] = agent
            runtime.pool._teams[team_id] = SimpleNamespace(agent=agent, current_session_id="session")
            assert await org_runtime.ensure_team_binding(team_id=team_id, session_id="session", agent=agent)
            assert tools
            assert backend.org_task_manager.messager is not None
            assert backend.org_message_service.messager is not None
            # Reading live and dormant teams must not disconnect the restored transport.
            await server._handle_org_snapshot(None, query("org.snapshot", second_team), asyncio.Lock())
        claim_turn.assert_not_called()
        message_turn.assert_not_called()
        for sender, recipient in ((first_team, second_team), (second_team, first_team)):
            result = await agents[sender].team_backend.org_message_service.send_leader_message(
                from_team_id=sender, from_leader_id=f"leader-{sender}", to_team_id=recipient,
                content=f"Restored {sender} is ready",
            )
            assert result.ok
            assert result.data["delivered_to"] == [recipient]
            message_turn.assert_called_with(
                team_id=recipient, session_id="session", message_id=result.data["message_id"],
                from_team_id=sender, organization_id="org",
            )
        claim_turn.reset_mock()
        result = await agents[first_team].team_backend.org_task_manager.create_task(
            task_id="task-after-restart", title="New collaboration", description="Brief analysis",
            created_by=OrgTaskCreator(
                creator_type="leader", creator_id=f"leader-{first_team}",
                organization_id="org", team_id=first_team,
            ),
            required_capabilities=["analysis"],
        )
        assert result.ok
        claim_turn.assert_called_once_with(
            team_id=second_team, session_id="session", task_id="task-after-restart", organization_id="org"
        )
    finally:
        await org_runtime.close()


@pytest.mark.parametrize("team,organization_id", [("foreign", "org"), ("owner", "foreign"), ("", "org")])
async def test_restart_snapshot_rejects_unbound_target(restarted_server, team, organization_id):
    server, sent, runtime, _ = restarted_server
    await server._handle_org_snapshot(
        None, query("org.snapshot", team, organization_id=organization_id), asyncio.Lock()
    )
    assert sent[-1].payload["organization"] is None
    assert sent[-1].payload["tasks"] == []
    runtime.activate.assert_not_awaited()


async def test_dissolved_organization_does_not_reappear(restarted_server):
    server, sent, _, _ = restarted_server
    db, organization = await server._load_persisted_organization("session")
    manager = TeamOrganizationManager(organization_id=organization.organization_id, db=db, session_id="session")
    await manager.dissolve_organization()
    await server._handle_team_list(None, query("team.list"), asyncio.Lock())
    assert [team["team_id"] for team in sent[-1].payload["teams"]] == ["owner"]
    await server._handle_org_snapshot(None, query("org.snapshot"), asyncio.Lock())
    assert sent[-1].payload["organization"] is None


async def test_restart_member_detail_failure_keeps_complete_rpc_list(restarted_server, monkeypatch):
    server, sent, _, _ = restarted_server
    db, _ = await server._load_persisted_organization("session")
    original_get = db.team.get_team

    async def read(team_id):
        if team_id == "architecture":
            raise OSError("simulated SQLite read failure")
        return await original_get(team_id)

    monkeypatch.setattr(db.team, "get_team", read)
    await server._handle_team_list(None, query("team.list"), asyncio.Lock())
    assert sent[-1].ok is True
    teams = {team["team_id"]: team for team in sent[-1].payload["teams"]}
    assert set(teams) == {"owner", "tech", "architecture", "org-summary-org"}
    assert teams["architecture"]["organization_id"] == "org"
    assert teams["architecture"]["leader_id"] == "leader-architecture"
    assert teams["tech"]["display_name"] == "技术洞察专家团"


async def test_disabled_switch_hides_existing_organization_without_querying_db(restarted_server, monkeypatch):
    server, sent, runtime, _ = restarted_server
    # The fixture has a real database with organization members and terminal tasks.
    assert await server._load_persisted_organization("session") is not None
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": False}})
    lookup = AsyncMock(side_effect=AssertionError("disabled history lookup"))
    monkeypatch.setattr(server, "_load_persisted_organization", lookup)
    await server._handle_team_list(None, query("team.list"), asyncio.Lock())
    assert [team["team_id"] for team in sent[-1].payload["teams"]] == ["owner"]
    await server._handle_org_snapshot(None, query("org.snapshot"), asyncio.Lock())
    assert sent[-1].payload["organization"] is None
    assert sent[-1].payload["tasks"] == []
    lookup.assert_not_awaited()
    runtime.activate.assert_not_awaited()


async def test_disabled_switch_hides_live_organization_snapshot(restarted_server, monkeypatch):
    server, sent, _, _ = restarted_server
    db, _ = await server._load_persisted_organization("session")
    live = SimpleNamespace(db=db, org_task_manager=SimpleNamespace(organization_id="org"))
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *args: live)
    monkeypatch.setattr(history, "get_config", lambda: {"experimental": {"team_organization_ui_enabled": False}})
    build = AsyncMock(side_effect=AssertionError("disabled snapshot query"))
    monkeypatch.setattr(server, "_build_org_snapshot", build)
    await server._handle_org_snapshot(None, query("org.snapshot"), asyncio.Lock())
    assert sent[-1].payload["organization"] is None
    build.assert_not_awaited()


async def test_restart_direct_expert_chat_restores_backend_and_reuses_membership(restarted_server, monkeypatch):
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    server, _, runtime, _ = restarted_server
    db, organization = await server._load_persisted_organization("session", "tech")
    original_ids = {leader.team_id for leader in organization.leaders}
    original_tasks = {task.task_id for task in await TeamOrganizationManager(
        organization_id="org", db=db, session_id="session"
    ).task_pool.list_tasks()}

    async def activate(spec, session_id):
        # Model/runtime construction is substituted; membership, database and RPC are real.
        binding = TeamOrganizationManager(organization_id="org", db=db, session_id=session_id)
        backend = SimpleNamespace(db=db, org_task_manager=binding)
        agent = SimpleNamespace(team_backend=backend)
        runtime.pool._teams[spec.team_name] = SimpleNamespace(agent=agent, current_session_id=session_id)
        return SimpleNamespace(agent=agent)

    runtime.activate = AsyncMock(side_effect=activate)
    runtime.pause = AsyncMock(return_value=True)
    runtime.stop_team = AsyncMock()

    async def get(team_id):
        return runtime.pool._teams.get(team_id)

    runtime.pool.get = get
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "_get_runtime", lambda _: runtime)
    monkeypatch.setattr(
        JiuwenExpertTeamLauncher, "_build_enriched_spec", AsyncMock(return_value=SimpleNamespace(team_name="tech"))
    )
    monkeypatch.setattr("jiuwenswarm.server.runtime.extension_package_manager.resolve_agent_group_dir", lambda name: name)
    monkeypatch.setattr("jiuwenswarm.agents.swarm.agent_group.load_agent_group_package_bundle", lambda path: object())
    records = []
    monkeypatch.setattr(agent_ws_server, "append_history_record", lambda **kwargs: records.append(kwargs))

    async def run(self, team_id, session_id, inputs, **kwargs):
        assert (team_id, session_id) == ("tech", "session")
        assert inputs["query"] == "What work did you do?"
        assert runtime.pool._teams[team_id].agent.team_backend.db is db
        return True

    monkeypatch.setattr(JiuwenExpertTeamLauncher, "run_organization_turn", run)
    chunks = [chunk async for chunk in server._targeted_expert_response_stream(
        query("chat.send", "tech", target_team_id="tech", query="What work did you do?")
    )]
    assert chunks[-1].payload["success"] is True
    assert set(runtime.pool._teams) == {"tech"}
    runtime.activate.assert_awaited_once()
    assert records[0]["extra"]["team_id"] == "tech"
    assert {leader.team_id for leader in (await server._load_persisted_organization("session"))[1].leaders} == original_ids
    remaining = await TeamOrganizationManager(organization_id="org", db=db, session_id="session").task_pool.list_tasks()
    assert {task.task_id for task in remaining} == original_tasks
