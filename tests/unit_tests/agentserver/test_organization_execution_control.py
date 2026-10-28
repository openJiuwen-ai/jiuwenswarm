# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Organization stop must terminate actual runners and pending organization wakes."""

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.runtime import TeamRuntimeManager
from openjiuwen.agent_teams.runtime.dispatch import RunActionKind
from openjiuwen.agent_teams.runtime.pool import ActiveTeam, RuntimeState
from openjiuwen.core.runner.runner import GLOBAL_RUNNER

from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.organization_execution import active_turns, is_stopping, stopping_teams


@pytest.fixture(autouse=True)
def isolate_owner_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.server.runtime.organization_owner_inbox.get_agent_root_dir", lambda: tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_owner,cancel_raises,owner_only", [
    (False, False, False), (True, False, False), (True, True, False),
    (True, False, True), (True, True, True),
])
@pytest.mark.parametrize("intent", ["cancel", None, "missing"])
async def test_stop_cancels_running_turns_and_real_core_queued_wakes(
    monkeypatch, stop_owner, cancel_raises, owner_only, intent,
):
    runtime = TeamRuntimeManager()
    stopped_owners = set()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_execution.set_owner_stopped",
        lambda sid, stopped: stopped_owners.add(sid) if stopped else stopped_owners.discard(sid),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_execution.owner_is_stopped",
        lambda sid, team="": sid in stopped_owners and team == "owner",
    )
    organization = runtime.organization_runtime_manager
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True,
    )
    runtime.finalize = AsyncMock()
    starts = {}
    agents = {}
    transports = {}
    tasks = []

    async def activate(spec, session_id, inputs):
        return SimpleNamespace(
            agent=agents[spec.name], action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
            session=SimpleNamespace(post_run=AsyncMock()),
        )

    runtime.activate = activate
    for name, org_id, session_id in [
        ("owner", "org", "session"), ("expert", "org", "session"), ("summary", "org", "session"),
        ("foreign", "different", "session"), ("other-session", "org", "other-session"),
    ]:
        starts[name] = asyncio.Event()

        async def stream(inputs, session, team=name):
            starts[team].set()
            await asyncio.Event().wait()
            yield None

        backend = SimpleNamespace(org_task_manager=SimpleNamespace(organization_id=org_id), is_leader=True)
        agent = SimpleNamespace(
            spec=SimpleNamespace(name=name, metadata={"expert_team": True, "channel_id": "web"}),
            stream=stream, team_backend=backend, stop_coordination=AsyncMock(),
        )
        agents[name] = agent
        await runtime.pool.add(ActiveTeam(name, agent, session_id, RuntimeState.PAUSED))
        organization._team_organizations[(session_id, name)] = org_id
        message_key = (session_id, name, "queued-message")
        organization._scheduled_leader_messages.add(message_key)
        organization._leader_turn_queues[(session_id, name)] = deque([{"_org_message_key": message_key}])
        transport = SimpleNamespace(send_push=AsyncMock(return_value=True))
        transports[name] = transport
        launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime, push_transport=transport)
        task = asyncio.create_task(launcher.run_organization_turn(
            name, session_id, {}, source="org_root_background" if name == "owner" else "org_expert_background",
            request_id=f"round-{name}",
        ))
        tasks.append(task)
        organization._leader_turn_workers[(session_id, name)] = task
    await asyncio.gather(*(event.wait() for event in starts.values()))
    assert len(active_turns("session")) == 4

    server = object.__new__(AgentWebSocketServer)
    server._session_stream_tasks = {}
    monkeypatch.setattr(server, "_resolve_team_backend", lambda sid, team: agents.get(team).team_backend if team in agents else None)
    async def cancel_owner(*args, **kwargs):
        for name in ("owner", "expert", "summary"):
            assert is_stopping("session", name) is (not owner_only or name == "owner")
        if cancel_raises:
            raise RuntimeError("owner cancel failed")
        return AgentResponse(request_id="stop", channel_id="web", ok=True)

    execution = SimpleNamespace(cancel_request=AsyncMock(side_effect=cancel_owner))
    monkeypatch.setattr(server, "_execution_runtime", lambda: execution)
    request = AgentRequest(
        request_id="stop", channel_id="web", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
        params={"mode": "team", "intent": "cancel", "target_team_id": "owner" if stop_owner else "expert",
                **({"organization_stop": True} if stop_owner else {}),
                **({"organization_owner_stop": True} if owner_only else {})},
    )
    if intent == "missing":
        request.params.pop("intent")
    else:
        request.params["intent"] = intent
    try:
        response = await (server._cancel_organization(request) if stop_owner else server._cancel_targeted_expert(request))
        assert response.ok is not cancel_raises
        assert response.payload["success"] is not cancel_raises
        if cancel_raises:
            assert response.payload["event_type"] == "chat.interrupt_result"
            assert response.payload["error"] == "owner cancel failed"
        stopped = {"owner", "expert", "summary"} if stop_owner else {"expert"}
        if owner_only:
            from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox

            enabled_at = owner_inbox().enabled_at("session", "owner")
            assert (enabled_at is not None) is not cancel_raises
            stopped = {"owner"}
            assert response.payload.get("organization_owner_stopped", False) is not cancel_raises
            assert ("session" in stopped_owners) is not cancel_raises
            assert response.payload["source"] == "org_owner_stop"
        for index, name in enumerate(agents):
            if name in stopped:
                assert tasks[index].cancelled()
                assert not is_stopping("session", name)
                assert await runtime.pool.get(name) is None
                assert ("session", name) not in organization._leader_turn_queues
                assert ("session", name) not in organization._leader_turn_workers
                assert ("session", name, "queued-message") not in organization._scheduled_leader_messages
                agents[name].stop_coordination.assert_awaited_once()
                pushes = transports[name].send_push.call_args_list
                assert [call.args[0]["payload"]["is_processing"] for call in pushes] == [True, False]
                assert pushes[0].args[0]["payload"]["request_id"] == pushes[-1].args[0]["payload"]["request_id"]
                # A queued notification cannot reactivate a torn-down team.
                assert not await JiuwenExpertTeamLauncher(runtime_manager=runtime).run_organization_turn(name, "session", {})
            else:
                assert not tasks[index].done()
                agents[name].stop_coordination.assert_not_awaited()
        expected_active = {"owner", "expert", "summary", "foreign"} - stopped
        assert {item["team_id"] for item in active_turns("session")} == expected_active
        if stop_owner:
            owner_request = execution.cancel_request.call_args.args[0]
            assert "target_team_id" not in owner_request.params
            assert owner_request.params["intent"] == "cancel"
        else:
            execution.cancel_request.assert_not_awaited()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await organization.close()
    assert not active_turns("session")
    assert not active_turns("other-session")


@pytest.mark.asyncio
async def test_stop_gate_rejects_a_new_background_turn_before_activation():
    runtime = SimpleNamespace(activate=AsyncMock())
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    async with stopping_teams("session", {"expert"}):
        assert is_stopping("session", "expert")
        assert not await launcher.run_organization_turn("expert", "session", {})
    assert not is_stopping("session", "expert")
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["resume", "supplement"])
@pytest.mark.parametrize("success", [True, False])
async def test_explicit_owner_resume_clears_stop_before_execution_and_restores_on_failure(
    monkeypatch, intent, success,
):
    from jiuwenswarm.server.runtime import organization_execution
    from jiuwenswarm.server.runtime.session import session_metadata

    metadata = {"team_name": "owner", "organization_owner_stopped": True}
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **kwargs: metadata.update(kwargs))
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(server, "_is_targeted_expert_request", lambda request: False)

    async def resume(request, **kwargs):
        assert not organization_execution.owner_is_stopped("session")
        return AgentResponse(request_id="resume", channel_id="web", ok=True, payload={"success": success})

    monkeypatch.setattr(server, "_execution_runtime", lambda: SimpleNamespace(cancel_request=resume))
    request = AgentRequest(request_id="resume", session_id="session", params={"mode": "team", "intent": intent})
    await server._handle_cancel(None, request, asyncio.Lock(), send_response=False)
    assert organization_execution.owner_is_stopped("session") is not success


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["org_root_background", "org_root_delivery"])
async def test_stopped_owner_ignores_followup_before_runtime_activation(monkeypatch, source):
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda _: {
        "team_name": "owner", "organization_owner_stopped": True,
    })
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock()), activate=AsyncMock())
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    assert await launcher.run_organization_turn("owner", "session", {"_org_relay_source": source})
    runtime.pool.get.assert_not_awaited()
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("target,attach,expected", [
    ("", False, True), ("expert", False, False), ("", True, False),
])
async def test_only_fresh_owner_input_clears_stopped_owner(monkeypatch, target, attach, expected):
    from jiuwenswarm.server.runtime import organization_execution
    from jiuwenswarm.server.runtime.organization_owner_inbox import owner_inbox
    from jiuwenswarm.server.runtime.session import session_metadata

    metadata = {"mode": "team", "team_name": "owner", "organization_owner_stopped": True}
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **kwargs: metadata.update(kwargs))
    assert owner_inbox().enabled_at("session", "owner") is None
    changed = []
    real_set_stopped = organization_execution.set_owner_stopped

    def record_stopped(sid, stopped):
        changed.append((sid, stopped))
        real_set_stopped(sid, stopped)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_execution.set_owner_stopped",
        record_stopped,
    )
    server = object.__new__(AgentWebSocketServer)
    request = AgentRequest(
        request_id="next", session_id="session", channel_id="web", req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team", "content": "continue", "target_team_id": target, "attach_only": attach},
    )
    await server._ensure_auto_team_binding_for_chat(request)
    assert changed == ([("session", False)] if expected else [])
    assert metadata["organization_owner_stopped"] is not expected
    if expected:
        assert owner_inbox().enabled_at("session", "owner") == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,target,intent", [("agent", "owner", "cancel"), ("team", "expert", "cancel"), ("team", "owner", "pause")])
async def test_organization_stop_rejects_wrong_mode_target_or_intent(monkeypatch, mode, target, intent):
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr("jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True)
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(server, "_resolve_team_backend", lambda sid, team: SimpleNamespace(org_task_manager=SimpleNamespace(organization_id="org")))
    stop = AsyncMock()
    monkeypatch.setattr(server, "_stop_organization_team", stop)
    response = await server._cancel_organization(AgentRequest(
        request_id="invalid", session_id="session", params={"mode": mode, "target_team_id": target, "intent": intent},
    ))
    assert not response.ok
    stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_owner,persisted_owner,persisted_target,allowed", [
    ("org", "org", "foreign", False),
    ("org", "org", None, False),
    (None, "org", "foreign", False),
    (None, "org", "org", True),
])
async def test_cold_expert_stop_compares_independent_bindings(monkeypatch, live_owner, persisted_owner, persisted_target, allowed):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda sid, team: SimpleNamespace(
        org_task_manager=SimpleNamespace(organization_id=live_owner)) if team == "owner" else None)
    async def lookup(sid, team):
        org = persisted_owner if team == "owner" else persisted_target
        return (object(), SimpleNamespace(organization_id=org)) if org else None
    monkeypatch.setattr(server, "_load_persisted_organization", lookup)
    stop = AsyncMock()
    monkeypatch.setattr(server, "_stop_organization_team", stop)
    request = AgentRequest(request_id="stop", session_id="session", req_method=ReqMethod.CHAT_CANCEL,
                           params={"mode": "team", "target_team_id": "expert"})
    response = await server._cancel_targeted_expert(request)
    assert response.ok is allowed
    assert stop.await_count == int(allowed)


@pytest.mark.asyncio
async def test_owner_stop_reports_membership_lookup_failure(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *_: None)
    monkeypatch.setattr(server, "_load_persisted_organization", AsyncMock(side_effect=OSError("database unavailable")))
    stop = AsyncMock()
    monkeypatch.setattr(server, "_stop_organization_team", stop)
    response = await server._cancel_organization(AgentRequest(
        request_id="stop", session_id="session", channel_id="web",
        params={"mode": "team", "target_team_id": "owner", "intent": "cancel"},
    ))
    assert response.ok is False
    assert response.payload["event_type"] == "chat.interrupt_result"
    assert response.payload["success"] is False
    assert response.payload["error"] == "database unavailable"
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_stop_does_not_report_success_when_foreground_cancel_fails(monkeypatch):
    server = object.__new__(AgentWebSocketServer)
    monkeypatch.setattr(agent_ws_server, "get_session_metadata", lambda _: {"team_name": "owner"})
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_organization_history.organization_enabled", lambda: True,
    )
    backend = SimpleNamespace(org_task_manager=SimpleNamespace(organization_id="org"))
    monkeypatch.setattr(server, "_resolve_team_backend", lambda *_: backend)
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", None, raising=False)
    cancel = AsyncMock(return_value=AgentResponse(request_id="stop", channel_id="web", ok=False))
    monkeypatch.setattr(server, "_execution_runtime", lambda: SimpleNamespace(cancel_request=cancel))
    stop = AsyncMock()
    monkeypatch.setattr(server, "_stop_organization_team", stop)
    response = await server._cancel_organization(AgentRequest(
        request_id="stop", session_id="session", channel_id="web",
        params={"mode": "team", "target_team_id": "owner", "intent": "cancel"},
    ))
    assert response.ok is False
    assert response.payload["success"] is False
    assert "Owner" in response.payload["error"]
    stop.assert_awaited_once_with("session", "owner")
