# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expert routing must not execute or cancel the owner's foreground turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server import agent_ws_server


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


def test_regular_stream_key_is_unchanged():
    assert AgentWebSocketServer._organization_stream_key(request(team="")) == "session"
    assert (
        AgentWebSocketServer._organization_stream_key(request())
        == "session::organization::expert"
    )


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
@pytest.mark.parametrize("description", [None, "", "short description", "x" * 400])
async def test_org_snapshot_handles_empty_description(monkeypatch, description):
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
    monkeypatch.setattr(pool, "get_process_org_manager", lambda **kwargs: manager)
    snapshot = await server._build_org_snapshot(
        session_id="session",
        team_id="owner",
        organization_id="org",
        backend=SimpleNamespace(db=object()),
    )
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
