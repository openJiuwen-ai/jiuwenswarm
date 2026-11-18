# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime import expert_team_batch as batch


@pytest.mark.parametrize("value", [None, [], "a", [""], [1], ["a"] * 33])
def test_reject_invalid_selection(value):
    with pytest.raises(ValueError):
        batch.validate_group_names(value)


def test_normalize_selection_without_reordering():
    assert batch.validate_group_names([" b ", "a", "b"]) == ["b", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "", "new", "../outside", " s "])
async def test_reject_invalid_session_before_runtime_initialization(session_id):
    with pytest.raises(ValueError, match="real session_id"):
        await batch.add_expert_teams(SimpleNamespace(session_id=session_id))


@pytest.mark.asyncio
async def test_partial_failure_retry_reuses_durable_membership(monkeypatch):
    from jiuwenswarm.server.runtime import team_organization_identity_store as identities

    saved = {"old-team": {"agent_group_name": "old"}}
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda team: saved.get(team, {}))
    validate = AsyncMock()
    monkeypatch.setattr(batch, "_validate_package", validate)
    organization = SimpleNamespace(
        organization_id="org", owner_team_id="owner",
        leaders=[SimpleNamespace(team_id="owner"), SimpleNamespace(team_id="old-team")],
    )
    team_runtime = SimpleNamespace(pool=SimpleNamespace(teams_for_session=AsyncMock(return_value=[])))
    failed_once = True

    async def create(**kwargs):
        nonlocal failed_once
        name = kwargs["agent_group_name"]
        if name == "retry" and failed_once:
            failed_once = False
            raise ValueError("temporary error")
        team_id = name + "-team"
        saved[team_id] = {"agent_group_name": name}
        organization.leaders.append(SimpleNamespace(team_id=team_id))
        return {"team_id": team_id}

    runtime = SimpleNamespace(create_and_invite_expert_team=AsyncMock(side_effect=create))
    args = dict(runtime=runtime, team_runtime=team_runtime, organization=organization, session_id="s")
    first = await batch._invite_groups(names=["old", "new", "retry"], **args)
    assert [item["status"] for item in first] == ["existing", "created", "failed"]
    second = await batch._invite_groups(names=["old", "new", "retry"], **args)
    assert [item["status"] for item in second] == ["existing", "existing", "created"]
    assert [call.kwargs["agent_group_name"] for call in runtime.create_and_invite_expert_team.call_args_list] == [
        "new", "retry", "retry",
    ]
    assert second[1]["team_id"] == first[1]["team_id"]


@pytest.mark.asyncio
async def test_validation_failure_does_not_launch_and_does_not_block_next_group(monkeypatch):
    from jiuwenswarm.server.runtime import team_organization_identity_store as identities

    monkeypatch.setattr(identities, "load_organization_team_identity", lambda _: {})
    monkeypatch.setattr(batch, "_validate_package", AsyncMock(side_effect=[ValueError("disabled"), None]))
    runtime = SimpleNamespace(create_and_invite_expert_team=AsyncMock(return_value={"team_id": "ok-team"}))
    result = await batch._invite_groups(
        names=["disabled", "ok"], runtime=runtime,
        team_runtime=SimpleNamespace(pool=SimpleNamespace(teams_for_session=AsyncMock(return_value=[]))),
        organization=SimpleNamespace(organization_id="org", owner_team_id="owner", leaders=[]),
        session_id="s",
    )
    assert [item["status"] for item in result] == ["failed", "created"]
    runtime.create_and_invite_expert_team.assert_awaited_once_with(
        organization_id="org", owner_team_id="owner", agent_group_name="ok", session_id="s",
    )


@pytest.mark.asyncio
async def test_existing_owner_is_reused_without_activation():
    backend = object()
    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            current_session_id="s", agent=SimpleNamespace(team_backend=backend),
        ))),
        activate=AsyncMock(),
    )
    assert await batch._prepare_owner(SimpleNamespace(session_id="s"), None, runtime, {"team_name": "owner"}) is backend
    runtime.activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_bound_to_another_session_is_rejected():
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(current_session_id="other"))))
    with pytest.raises(ValueError, match="another session"):
        await batch._prepare_owner(SimpleNamespace(session_id="s"), None, runtime, {"team_name": "owner"})


@pytest.mark.asyncio
async def test_new_owner_is_created_and_paused_without_starting_a_chat(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    for name in ("persist_session_team_dispatch_mode", "persist_session_team_bootstrap_mode", "persist_session_swarmflow_config"):
        monkeypatch.setattr(team_helpers, name, Mock())
    monkeypatch.setattr(team_helpers, "_resolve_session_team_dispatch_mode", lambda *a, **k: "scheduled")
    monkeypatch.setattr(team_helpers, "_resolve_session_team_bootstrap_mode", lambda *a, **k: "leader")
    monkeypatch.setattr(team_helpers, "_resolve_session_swarmflow_config", lambda *a, **k: {"enable_swarmflow": False})
    monkeypatch.setattr(batch, "update_session_metadata", Mock())
    backend = SimpleNamespace(team_name="owner", build_team=AsyncMock())
    agent = SimpleNamespace(team_backend=backend, stream=Mock(side_effect=AssertionError("no LLM turn")))
    spec = SimpleNamespace(team_name="owner", enable_organization=False, dispatch_mode="scheduled",
                           bootstrap_mode="leader", enable_swarmflow=False, swarmflow_budget=None)
    manager = SimpleNamespace(get_swarm_enriched_team_spec=AsyncMock(return_value=spec),
                              prepare_runtime_activation=AsyncMock(), commit_runtime_ready=Mock(),
                              clear_pending_runtime=Mock(), clear_active_runtime=Mock(),
                              clear_session_initialized=Mock())
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=None)),
                              activate=AsyncMock(return_value=SimpleNamespace(agent=agent)), pause=AsyncMock())
    request = SimpleNamespace(session_id="s", request_id="r", user_id="u", channel_id="web", metadata={}, params={})
    assert await batch._prepare_owner(request, manager, runtime, {"mode": "team"}) is backend
    runtime.activate.assert_awaited_once_with(spec, "s")
    runtime.pause.assert_awaited_once_with(team_name="owner", session_id="s")
    backend.build_team.assert_awaited_once()
    manager.commit_runtime_ready.assert_not_called()
    manager.clear_pending_runtime.assert_called_once_with("s")
    manager.clear_active_runtime.assert_called_once_with("s")
    manager.clear_session_initialized.assert_called_once_with("s")
    assert spec.enable_organization
    agent.stream.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_requests_share_session_lock_and_reuse_success(monkeypatch):
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenswarm.agents.harness import team
    from jiuwenswarm.agents.harness.team.expert_org import wiring
    from jiuwenswarm.server.runtime import team_organization_history as history
    from jiuwenswarm.server.runtime import team_organization_identity_store as identities

    organization = SimpleNamespace(organization_id="org", owner_team_id="owner", leaders=[])
    backend = SimpleNamespace(team_name="owner", db=object(), org_task_manager=None)
    saved = {}
    async def create(**kwargs):
        from openjiuwen.agent_teams.context import get_session_id

        assert get_session_id() == "s"
        await asyncio.sleep(0.01)
        saved["expert"] = {"agent_group_name": kwargs["agent_group_name"]}
        organization.leaders.append(SimpleNamespace(team_id="expert"))
        return {"team_id": "expert"}
    org = SimpleNamespace(create_organization=AsyncMock(return_value=organization),
                          create_and_invite_expert_team=AsyncMock(side_effect=create))
    runtime = SimpleNamespace(organization_runtime_manager=org,
                              pool=SimpleNamespace(teams_for_session=AsyncMock(return_value=[])))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    lock = asyncio.Lock()
    monkeypatch.setattr(team, "get_team_manager", lambda _: SimpleNamespace(get_startup_lock=lambda _: lock))
    monkeypatch.setattr(wiring, "register_expert_adapter_installer", lambda: None)
    monkeypatch.setattr(history, "organization_enabled", lambda: True)
    monkeypatch.setattr(history, "load_persisted_organization", AsyncMock(return_value=None))
    monkeypatch.setattr(identities, "load_organization_team_identity", lambda name: saved.get(name, {}))
    monkeypatch.setattr(batch, "_prepare_owner", AsyncMock(return_value=backend))
    monkeypatch.setattr(batch, "get_session_metadata", lambda *a, **k: {"mode": "team"})
    monkeypatch.setattr(batch, "_validate_package", AsyncMock())
    request = SimpleNamespace(session_id="s", channel_id="web", params={"agent_group_names": ["a"]})
    results = await asyncio.gather(batch.add_expert_teams(request), batch.add_expert_teams(request))
    assert [result["results"][0]["status"] for result in results] == ["created", "existing"]
    org.create_and_invite_expert_team.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_registers_authenticated_proxy(monkeypatch):
    from jiuwenswarm.extensions.expert_teams import extension
    from jiuwenswarm.common.schema.message import ReqMethod

    proxy = AsyncMock()
    monkeypatch.setattr(extension, "proxy_unary_request", proxy)
    channel = SimpleNamespace(register_method=Mock(), connection_user_id=Mock(return_value="alice"))
    client = object()
    extension.ExpertTeamsExtension().bind_web_channel(channel, SimpleNamespace(require_agent_client=lambda: client))
    name, handler = channel.register_method.call_args.args
    assert name == "org.expert_teams.add"
    assert channel.register_method.call_args.kwargs == {"local_only": True}
    ws = object()
    await handler(ws, "r", {"agent_group_names": ["a"]}, "s")
    assert proxy.call_args.kwargs["user_id"] == "alice"
    assert proxy.call_args.kwargs["req_method"] == ReqMethod.ORG_EXPERT_TEAMS_ADD
    assert proxy.call_args.kwargs["session_id"] == "s"
