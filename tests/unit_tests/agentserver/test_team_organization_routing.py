# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expert routing must not execute or cancel the owner's foreground turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
    HOST_USER_ORIGIN_INTERNAL,
)


@pytest.fixture(autouse=True)
def isolate_historical_database(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_organization_history.get_config",
        lambda: {"experimental": {"team_organization_ui_enabled": True}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_organization_identity_store.load_organization_team_identity",
        lambda _: {},
    )
    monkeypatch.setattr(
        AgentWebSocketServer, "_load_persisted_organization", AsyncMock(return_value=None)
    )


def request(method=ReqMethod.CHAT_SEND, team="expert"):
    return AgentRequest(
        request_id="request",
        channel_id="web",
        session_id="session",
        req_method=method,
        params={"mode": "team", "target_team_id": team},
    )


def backend(org):
    return SimpleNamespace(org_task_manager=SimpleNamespace(organization_id=org))


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,params,metadata,external", [
    ("web", {}, None, True),
    ("feishu", {}, None, True),
    ("cron", {}, None, False),
    ("heartbeat", {}, None, False),
    ("web", {"source": "background"}, None, False),
    ("web", {"log_as_user": False}, None, False),
    ("web", {"automation": {"kind": "heartbeat"}}, None, False),
    ("web", {}, {"automation": {"kind": "heartbeat"}}, False),
    ("web", {"metadata": {"automation": {"kind": "heartbeat"}}}, None, False),
    ("web", {"origin_kind": HOST_USER_ORIGIN_EXTERNAL}, {"automation": {"kind": "heartbeat"}}, False),
    ("web", {}, {SESSION_MESSAGE_INTERNAL_KEY: {}}, False),
])
async def test_expert_input_origin_is_classified_at_host_ingress(monkeypatch, channel, params, metadata, external):
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *args: backend("org"))
    monkeypatch.setattr(agent_ws_server, "append_history_record", lambda **kwargs: None)
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "run_organization_turn", run)
    req = request()
    req.channel_id = channel
    req.params.update(params)
    req.params["content"] = "Use the supplied materials."
    req.metadata = metadata
    chunks = [chunk async for chunk in server._targeted_expert_response_stream(req)]
    run.assert_awaited_once()
    assert run.await_args.kwargs["origin_kind"] == (
        HOST_USER_ORIGIN_EXTERNAL if external else HOST_USER_ORIGIN_INTERNAL
    )
    assert chunks[-1].payload["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "organization,allowed", [("org", True), ("foreign", False), (None, False)]
)
async def test_cancel_isolated_from_owner(monkeypatch, organization, allowed):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"}
    )
    monkeypatch.setattr(
        server,
        "_resolve_team_backend",
        lambda sid, team: backend("org" if team == "owner" else organization),
    )
    started = asyncio.Event()

    async def running():
        started.set()
        await asyncio.Event().wait()

    expert = asyncio.create_task(running())
    owner = asyncio.create_task(running())
    await started.wait()
    server._session_stream_tasks = {
        "session": {owner: asyncio.Event()},
        "session::organization::expert": {expert: asyncio.Event()},
    }
    try:
        response = await server._cancel_targeted_expert(request(ReqMethod.CHAT_CANCEL))
        assert response.ok is allowed
        assert expert.cancelled() is allowed
        assert not owner.done()
    finally:
        owner.cancel()
        expert.cancel()
        await asyncio.gather(owner, expert, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_org,allowed", [("org", True), ("foreign", False), (None, False)])
async def test_cancel_recovered_expert_without_owner_runtime(monkeypatch, persisted_org, allowed):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda sid, team: backend("org") if team == "expert" else None)
    lookup = AsyncMock(return_value=(object(), SimpleNamespace(organization_id=persisted_org)) if persisted_org else None)
    monkeypatch.setattr(server, "_load_persisted_organization", lookup)
    started = asyncio.Event()

    async def running():
        started.set()
        await asyncio.Event().wait()

    expert = asyncio.create_task(running())
    server._session_stream_tasks = {"session::organization::expert": {expert: asyncio.Event()}}
    try:
        await started.wait()
        response = await server._cancel_targeted_expert(request(ReqMethod.CHAT_CANCEL))
        assert response.ok is allowed
        assert expert.cancelled() is allowed
        lookup.assert_awaited_once_with("session", "expert")
    finally:
        expert.cancel()
        await asyncio.gather(expert, return_exceptions=True)


@pytest.mark.asyncio
async def test_expert_stream_uses_runtime_event_adapter(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    closed = []

    async def stream(req):
        try:
            yield AgentResponseChunk(
                request_id=req.request_id,
                channel_id="web",
                payload={"event_type": "chat.final", "team_id": "expert"},
                is_complete=True,
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(server, "_targeted_expert_response_stream", stream)
    events = [
        event async for event in server._targeted_expert_runtime_stream(request())
    ]
    assert len(events) == 1
    assert events[0].session_id == "session"
    assert events[0].payload["team_id"] == "expert"
    assert events[0].is_complete
    assert closed == [True]


def test_regular_stream_key_is_unchanged(monkeypatch):
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    assert AgentWebSocketServer._organization_stream_key(request(team="")) == "session"
    assert AgentWebSocketServer._organization_stream_key(request(ReqMethod.TEAM_LIST)) == "session"
    assert (
        AgentWebSocketServer._organization_stream_key(request())
        == "session::organization::expert"
    )


@pytest.mark.asyncio
async def test_real_expert_stream_is_registered_cancelled_and_explicitly_settled(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    server._session_stream_tasks = {}
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *_: backend("org"))
    monkeypatch.setattr(agent_ws_server.AgentRuntime, "uses_session_runtime", lambda _: True)
    monkeypatch.setattr(server, "_finalize_session_message_resume", AsyncMock())
    monkeypatch.setattr(server, "_send_runtime_event", AsyncMock(return_value=True))
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(server, "send_push", push)
    started = asyncio.Event()
    closed = asyncio.Event()

    async def stream(req):
        try:
            started.set()
            await asyncio.Event().wait()
            if False:
                yield req
        finally:
            closed.set()

    monkeypatch.setattr(server, "_targeted_expert_runtime_stream", stream)
    active_request = request()
    active_request.metadata = {"ws_id": "old-connection"}
    expert = asyncio.create_task(server._handle_stream_impl(object(), active_request, asyncio.Lock()))
    owner = asyncio.create_task(asyncio.Event().wait())
    server._session_stream_tasks["session"] = {owner: asyncio.Event()}
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert server._expert_request_snapshot("session") == [{"team_id": "expert", "request_id": "request"}]
        monkeypatch.setattr(server, "_team_list_entries", AsyncMock(return_value=[]))
        monkeypatch.setattr(agent_ws_server, "encode_agent_response_for_wire", lambda response, **kwargs: response)
        send = AsyncMock()
        monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
        await server._handle_team_list(None, request(ReqMethod.TEAM_LIST), asyncio.Lock())
        assert send.await_args.args[1].payload["expert_requests"] == [{"team_id": "expert", "request_id": "request"}]
        cancel_request = request(ReqMethod.CHAT_CANCEL)
        cancel_request.metadata = {"ws_id": "new-connection"}
        response = await server._cancel_targeted_expert(cancel_request)
        assert response.ok is True
        assert response.payload["cancelled_request_ids"] == ["request"]
        assert expert.cancelled() and closed.is_set()
        assert not owner.done()
        assert server._expert_request_snapshot("session") == []
        assert "session::organization::expert" not in server._session_stream_tasks
        payload = push.await_args.args[0]["payload"]
        assert payload["team_id"] == "expert"
        assert payload["request_id"] == "request"
        assert payload["is_processing"] is False
        assert push.await_args.args[0]["metadata"] == {"ws_id": "new-connection"}
    finally:
        owner.cancel()
        expert.cancel()
        await asyncio.gather(owner, expert, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_lookup_error_returns_failed_interrupt_response(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda sid, team: backend("org") if team == "expert" else None)
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(side_effect=RuntimeError("db unavailable")))
    response = await server._cancel_targeted_expert(request(ReqMethod.CHAT_CANCEL))
    assert response.ok is False
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["success"] is False
    assert "db unavailable" in response.payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,team", [("agent", "expert"), ("team", "owner"), ("team", "")])
@pytest.mark.parametrize("intent", ["cancel", "pause", "resume", "supplement"])
async def test_non_expert_interrupt_uses_session_runtime(monkeypatch, mode, team, intent):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    cancel = AsyncMock(return_value=SimpleNamespace(ok=True))
    targeted = AsyncMock()
    monkeypatch.setattr(server, "_cancel_targeted_expert", targeted)
    monkeypatch.setattr(server, "_execution_runtime", lambda: SimpleNamespace(cancel_request=cancel))
    req = request(ReqMethod.CHAT_CANCEL, team)
    req.params["mode"] = mode
    req.params["intent"] = intent
    await server._handle_cancel(None, req, asyncio.Lock(), send_response=False)
    cancel.assert_awaited_once_with(req, allow_create=False)
    targeted.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_list_defaults_to_owner_even_when_expert_is_running(monkeypatch):
    from jiuwenswarm.server import agent_ws_server

    server = object.__new__(AgentWebSocketServer)
    sent = []

    async def entries(session_id, channel_id):
        assert (session_id, channel_id) == ("session", "web")
        return [
            {"team_id": "expert", "state": "running", "is_owner": False},
            {"team_id": "owner", "state": "paused", "is_owner": True},
        ]

    async def send(ws, payload):
        sent.append(payload)

    monkeypatch.setattr(server, "_team_list_entries", entries)
    monkeypatch.setattr(
        agent_ws_server,
        "encode_agent_response_for_wire",
        lambda response, **kwargs: response,
    )
    monkeypatch.setattr(agent_ws_server, "send_wire_payload", send)
    await server._handle_team_list(object(), request(), asyncio.Lock())
    assert sent[0].payload["default_team_id"] == "owner"


def team_manager_marker(snapshot=None, active=None, pending=None):
    return SimpleNamespace(
        get_runtime_team_snapshot=lambda: snapshot or {},
        get_active_team_name=lambda session_id: active,
        _pending_team_names=pending or {},
    )


@pytest.mark.asyncio
async def test_team_list_marks_assembling_team_owner_before_metadata_persisted(monkeypatch):
    """First-turn window: the team is listed via TeamManager markers while
    ``team_name`` has not yet been persisted to session metadata. The Web
    conversation view must not flip to the empty team panel, so the live
    binding has to answer is_owner."""
    server = object.__new__(AgentWebSocketServer)
    manager = team_manager_marker(
        snapshot={"session": {"team_name": "biz-plan", "state": "pending"}},
        pending={"session": "biz-plan"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda channel_id: manager
    )
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {})
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", None)

    teams = await server._team_list_entries("session", "web")

    assert [(team["team_id"], team["is_owner"]) for team in teams] == [
        ("biz-plan", True)
    ]


@pytest.mark.asyncio
async def test_team_list_metadata_owner_wins_over_live_marker(monkeypatch):
    """Persisted metadata stays authoritative once written, even when the
    live TeamManager marker points at another team."""
    server = object.__new__(AgentWebSocketServer)
    manager = team_manager_marker(
        snapshot={"session": {"team_name": "rebound", "state": "active"}},
        active="rebound",
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda channel_id: manager
    )
    monkeypatch.setattr(
        agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner-team"}
    )
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", None)

    teams = await server._team_list_entries("session", "web")

    assert {team["team_id"]: team["is_owner"] for team in teams} == {
        "rebound": False,
        "owner-team": True,
    }


@pytest.mark.asyncio
async def test_team_list_without_owner_binding_marks_no_owner(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id: team_manager_marker(),
    )
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {})
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", None)

    async def teams_for_session(session_id):
        return [
            SimpleNamespace(
                team_name="expert",
                state=SimpleNamespace(value="running"),
                agent=SimpleNamespace(
                    team_backend=SimpleNamespace(
                        is_leader=True, leader_member_name="leader"
                    ),
                    card=SimpleNamespace(name="Expert"),
                    spec=SimpleNamespace(metadata={}),
                ),
                current_session_id=session_id,
            )
        ]

    runtime_manager = SimpleNamespace(pool=SimpleNamespace(teams_for_session=teams_for_session))
    monkeypatch.setattr(
        "openjiuwen.core.runner.runner.GLOBAL_RUNNER", SimpleNamespace(
            _team_runtime_manager=runtime_manager
        )
    )

    teams = await server._team_list_entries("session", "web")

    assert teams and all(team["is_owner"] is False for team in teams)


@pytest.mark.asyncio
async def test_team_list_returns_expert_snapshot_from_spec_without_package(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    identity = {"id": "asset", "name": "expert-group", "display_name": "专家团"}
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _: team_manager_marker(),
    )
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    entry = SimpleNamespace(
        team_name="expert", state=SimpleNamespace(value="paused"),
        agent=SimpleNamespace(
            team_backend=SimpleNamespace(is_leader=True),
            spec=SimpleNamespace(metadata={
                "agent_group_name": "expert-group", "team_group_identity": identity,
            }),
        ),
    )
    pool = SimpleNamespace(teams_for_session=AsyncMock(return_value=[entry]))
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", SimpleNamespace(
        _team_runtime_manager=SimpleNamespace(pool=pool),
    ))

    teams = await server._team_list_entries("session", "web")

    expert = next(team for team in teams if team["team_id"] == "expert")
    assert expert["team_group_identity"] == identity
    assert expert["is_owner"] is False
    assert "team_group_identity" not in next(team for team in teams if team["is_owner"])



@pytest.mark.asyncio
async def test_team_list_merges_history_without_downgrading_live_team(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda _: team_manager_marker()
    )
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    live = SimpleNamespace(
        team_name="expert", state=SimpleNamespace(value="running"),
        agent=SimpleNamespace(
            team_backend=SimpleNamespace(is_leader=True, leader_member_name="live-leader"),
            card=SimpleNamespace(name="Live expert"), spec=SimpleNamespace(metadata={}),
        ),
    )
    pool = SimpleNamespace(teams_for_session=AsyncMock(return_value=[live]))
    monkeypatch.setattr(
        "openjiuwen.core.runner.runner.GLOBAL_RUNNER",
        SimpleNamespace(_team_runtime_manager=SimpleNamespace(pool=pool)),
    )
    organization = SimpleNamespace(
        organization_id="org",
        leaders=[SimpleNamespace(team_id=name, leader_id=f"leader-{name}", capabilities=["analysis"])
                 for name in ("owner", "expert", "summary")],
    )
    db = SimpleNamespace(team=SimpleNamespace(get_team=AsyncMock(return_value=None)))
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(return_value=(db, organization)))
    teams = {team["team_id"]: team for team in await server._team_list_entries("session", "web")}
    assert set(teams) == {"owner", "expert", "summary"}
    assert teams["owner"]["is_owner"] is True
    assert teams["summary"]["state"] == "configured"
    assert teams["expert"]["state"] == "running"
    assert teams["expert"]["display_name"] == "Live expert"
    assert teams["expert"]["source"] == "pool"
    assert teams["expert"]["organization_id"] == "org"


@pytest.mark.asyncio
async def test_history_failure_keeps_live_team_list(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda _: team_manager_marker()
    )
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", None)
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(side_effect=OSError("unavailable")))
    teams = await server._team_list_entries("session", "web")
    assert [team["team_id"] for team in teams] == ["owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,broken", [
    (stage, name) for stage in ("database", "identity") for name in ("owner", "expert", "summary")
] + [("import", "")])
async def test_optional_history_failure_preserves_all_members(monkeypatch, stage, broken):
    import builtins

    warnings = Mock(wraps=agent_ws_server.logger.warning)
    monkeypatch.setattr(agent_ws_server.logger, "warning", warnings)
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr("jiuwenswarm.agents.harness.team.get_team_manager", lambda _: team_manager_marker())
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", None)
    names = ("owner", "expert", "summary")
    organization = SimpleNamespace(
        organization_id="org", leaders=[
            SimpleNamespace(team_id=name, leader_id=f"leader-{name}", capabilities=["analysis"])
            for name in names
        ],
    )
    reads = []

    async def get_team(name):
        reads.append(name)
        if stage == "database" and name == broken:
            raise OSError("database read failed")
        return SimpleNamespace(display_name=f"Display {name}")

    def identity(name):
        if stage == "identity" and name == broken:
            raise RuntimeError("unexpected identity failure")
        return {}

    module = "jiuwenswarm.server.runtime.team_organization_identity_store"
    monkeypatch.setattr(module + ".load_organization_team_identity", identity)
    if stage == "import":
        original_import = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == module:
                raise ImportError("identity loader is unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", unavailable)
    db = SimpleNamespace(team=SimpleNamespace(get_team=get_team))
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(return_value=(db, organization)))
    teams = {team["team_id"]: team for team in await server._team_list_entries("session", "web")}
    assert set(teams) == set(names)
    assert reads == list(names)
    assert all(team["organization_id"] == "org" for team in teams.values())
    assert all(team["capabilities"] == ["analysis"] for team in teams.values())
    for name in names:
        if stage != "database" or name != broken:
            assert teams[name]["display_name"] == f"Display {name}"
    assert any("unavailable" in call.args[0] for call in warnings.call_args_list)


@pytest.mark.asyncio
async def test_cold_expert_request_recovers_target_without_running_owner(monkeypatch):
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    server = object.__new__(AgentWebSocketServer)
    organization = SimpleNamespace(organization_id="org")
    db = object()
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *args: None)
    history_lookup = AsyncMock(return_value=(db, organization))
    monkeypatch.setattr(server, "_load_persisted_organization", history_lookup)
    restore = AsyncMock(return_value=backend("org"))
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "restore", restore)
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "run_organization_turn", run)
    monkeypatch.setattr(agent_ws_server, "append_history_record", lambda **kwargs: None)
    chunks = [chunk async for chunk in server._targeted_expert_response_stream(request())]
    history_lookup.assert_awaited_once_with("session", "expert")
    restore.assert_awaited_once_with(
        team_id="expert", session_id="session", organization=organization, db=db, channel_id="web"
    )
    assert run.await_args.args[:2] == ("expert", "session")
    assert chunks[-1].payload["success"] is True


@pytest.mark.asyncio
async def test_cold_expert_recovery_error_returns_terminal_status(monkeypatch):
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *args: None)
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(return_value=(object(), SimpleNamespace(organization_id="org"))))
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "restore", AsyncMock(side_effect=ValueError("package unavailable")))
    run = AsyncMock()
    monkeypatch.setattr(JiuwenExpertTeamLauncher, "run_organization_turn", run)
    chunks = [chunk async for chunk in server._targeted_expert_response_stream(request())]
    assert "package unavailable" in chunks[0].payload["error"]
    assert chunks[-1].payload["is_processing"] is False
    assert chunks[-1].payload["success"] is False
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [None, "", "short description", "x" * 400])
async def test_org_snapshot_handles_empty_description(monkeypatch, description):
    from openjiuwen.agent_teams.organization import manager as org_manager
    from openjiuwen.agent_teams.organization import pool

    server = object.__new__(AgentWebSocketServer)
    task = SimpleNamespace(
        description=description,
        unclaimed=None,
        status="OPEN",
        brief=lambda: {"task_id": "task"},
    )
    organization = SimpleNamespace(
        organization_id="org",
        display_name="Organization",
        description="",
        owner_team_id="owner",
        owner_leader_id="leader",
        leaders=[],
        metadata={},
        unclaimed_task_policy=SimpleNamespace(model_dump=lambda: {}),
    )
    manager = SimpleNamespace(
        get_organization=AsyncMock(return_value=organization),
        task_pool=SimpleNamespace(
            list_tasks=AsyncMock(return_value=[task]),
            list_pending_reviews=AsyncMock(return_value=[]),
        ),
    )
    reader = Mock(return_value=manager)
    monkeypatch.setattr(org_manager, "TeamOrganizationManager", reader)
    monkeypatch.setattr(
        pool, "get_process_org_manager", Mock(side_effect=AssertionError("snapshot accessed runtime registry"))
    )
    db = object()
    snapshot = await server._build_org_snapshot(
        session_id="session",
        team_id="owner",
        organization_id="org",
        backend=SimpleNamespace(db=db, messager=object()),
    )
    reader.assert_called_once_with(organization_id="org", db=db, session_id="session")
    assert snapshot["tasks"] == [
        {"task_id": "task", "description": (description or "")[:280]}
    ]
    assert snapshot["stats"]["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [False, True])
async def test_direct_expert_failure_persists_error_once(monkeypatch, succeeded):
    from jiuwenswarm.agents.harness.team.expert_org.launcher import (
        JiuwenExpertTeamLauncher,
    )
    server = object.__new__(AgentWebSocketServer)
    history = []
    monkeypatch.setattr(
        agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"}
    )
    monkeypatch.setattr(
        server, "_resolve_team_backend", lambda sid, team: backend("org")
    )
    monkeypatch.setattr(
        agent_ws_server,
        "append_history_record",
        lambda **kwargs: history.append(kwargs),
    )

    async def run_turn(self, *args, **kwargs):
        return succeeded

    monkeypatch.setattr(JiuwenExpertTeamLauncher, "run_organization_turn", run_turn)
    req = request()
    req.params["content"] = "Explain your finding"
    chunks = [chunk async for chunk in server._targeted_expert_response_stream(req)]
    errors = [
        chunk.payload for chunk in chunks if chunk.payload["event_type"] == "chat.error"
    ]
    if succeeded:
        assert [record["role"] for record in history] == ["user"]
        assert errors == []
    else:
        assert [record["role"] for record in history] == ["user", "assistant"]
        error_record = history[1]
        assert len(errors) == 1
        assert error_record["event_type"] == "chat.error"
        assert error_record["content"] == errors[0]["error"]
        assert error_record["extra"] == errors[0]
        assert error_record["extra"]["team_id"] == "expert"
        assert error_record["request_id"] == req.request_id
    assert chunks[-1].is_complete
    assert chunks[-1].payload["success"] is succeeded


@pytest.mark.asyncio
async def test_denied_expert_request_does_not_write_history(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    history = []
    monkeypatch.setattr(
        agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"}
    )
    monkeypatch.setattr(
        server, "_resolve_team_backend", lambda sid, team: backend(team)
    )
    monkeypatch.setattr(
        agent_ws_server,
        "append_history_record",
        lambda **kwargs: history.append(kwargs),
    )
    chunks = [
        chunk async for chunk in server._targeted_expert_response_stream(request())
    ]
    assert history == []
    assert chunks[0].payload["event_type"] == "chat.error"
