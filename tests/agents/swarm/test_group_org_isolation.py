"""Disjoint organization rounds must survive interleaved proxy launches and cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.swarm import group_chat, group_turn
from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from jiuwenswarm.server.runtime import group_service
from tests.agents.swarm.test_group_org_bootstrap import _MemoryGroup, _Pool, SESSION


def test_legacy_registration_is_migrated_without_mixing_organizations(monkeypatch):
    group = _MemoryGroup(monkeypatch, organization_id="A", organization_teams=[
        dict(member_name="a", team_id="ta", role="owner"),
    ])
    group_service.upsert_group_organization_team(SESSION, "B", dict(member_name="b", team_id="tb", role="owner"))
    group_service.upsert_group_organization_team(SESSION, "A", dict(member_name="a", team_id="ta", role="owner"))
    assert len(group_service.group_organization_teams(SESSION)) == 2
    assert group_service.group_organization_owner_team_ids(SESSION) == {"ta", "tb"}
    group_service.clear_group_organization_binding(SESSION, "B")
    assert group.state["organization_id"] == "A"
    assert [row["team_id"] for row in group_service.group_organization_teams(SESSION, "A")] == ["ta"]
    group_service.clear_group_organization_binding(SESSION, "B")
    assert len(group_service.group_organization_teams(SESSION)) == 1
    group_service.mark_group_organization(SESSION, "B", [dict(member_name="b", team_id="tb", role="owner")])
    assert len(group_service.group_organization_teams(SESSION)) == 2
    group_service.clear_group_organization_binding(SESSION, "A")
    assert [row["team_id"] for row in group_service.group_organization_teams(SESSION)] == ["tb"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["A", "B"])
@pytest.mark.parametrize("fail_first", [False, True])
async def test_disjoint_proxy_runs_bind_execute_and_clean_up_independently(monkeypatch, first, fail_first):
    group = _MemoryGroup(monkeypatch)
    group.state["members"] = [dict(kind="agent_group", id=name, member_name=name)
                               for name in ["business", "insight", "architecture", "contract"]]
    plans = {key: bootstrap.record_organization_plan(SESSION, mentions=members, body=key, plan_id=key)
             for key, members in [("A", ["business", "insight"]), ("B", ["architecture", "contract"])]}
    pool = _Pool()
    runtime = SimpleNamespace(pool=pool)
    launched, paused, outputs, errors, cards = [], [], [], [], []
    entered = {key: asyncio.Event() for key in plans}
    finish = {key: asyncio.Event() for key in plans}

    async def ready(**kwargs):
        member = kwargs["member"]["member_name"]
        launched.append(member)
        team_id = bootstrap.instance_team_id(SESSION, member)
        pool.add(team_id)
        return team_id

    async def create(**kwargs):
        pool.entries[kwargs["owner_team_id"]].agent.team_backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs["organization_id"])

    async def invite(**kwargs):
        pool.entries[kwargs["target_team_id"]].agent.team_backend.org_task_manager = SimpleNamespace(
            organization_id=kwargs["organization_id"])

    async def release(**kwargs):
        for entry in pool.entries.values():
            if bootstrap._bound_organization(entry.agent.team_backend) == kwargs["organization_id"]:
                entry.agent.team_backend.org_task_manager = None

    org_runtime = SimpleNamespace(release_membership=release)

    async def result(_future, _pool, plan_id, **kwargs):
        entered[plan_id].set()
        await finish[plan_id].wait()
        if fail_first and plan_id == first:
            raise RuntimeError("simulated task failure")
        return "result-" + plan_id, []

    async def post(member, content, **kwargs):
        outputs.append((member, content))
        return SimpleNamespace(message=SimpleNamespace(message_id=kwargs["client_message_id"]))

    async def pause(**kwargs):
        paused.append(set(kwargs["team_ids"]))

    async def publish(**kwargs):
        cards.append((kwargs["plan"]["plan_id"], kwargs.get("status", "running")))

    async def error(_backend, member, message, _identity):
        errors.append((member, message))

    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (runtime, org_runtime, None))
    monkeypatch.setattr(bootstrap, "ensure_member_instance", ready)
    monkeypatch.setattr(bootstrap, "_owner_creates_organization", create)
    monkeypatch.setattr(bootstrap, "_owner_invites_team", invite)
    for name in ["_record_team_run", "_set_member_instance_silence", "_block_template_expert_creation",
                 "_owner_primes_root_task", "_queue_owner_start_reminder"]:
        monkeypatch.setattr(bootstrap, name, AsyncMock())
    monkeypatch.setattr(bootstrap, "_await_completed_result", result)
    monkeypatch.setattr(bootstrap, "pause_organization_instances", pause)
    monkeypatch.setattr(bootstrap, "publish_organization_card", publish)
    monkeypatch.setattr("jiuwenswarm.server.runtime.group_execution.post_member_error", error)
    monkeypatch.setattr(bootstrap, "_JOIN_POLL_SECONDS", 0.001)

    async def run(member, key):
        backend = SimpleNamespace(member_name=member, team_name="outer-team", conversation_mode="group_chat",
                                  append_group_message=post)
        tool = group_chat.RunTeam(backend, SimpleNamespace(session_id=SESSION), {})
        token = group_turn.bind_group_turn(group_turn.GroupTurn(SESSION, member, key, "organization"))
        try:
            response = await tool.invoke({"action": "start"})
            assert response.success, response.error
            assert response.data["plan_id"] == key
        finally:
            group_turn.reset_group_turn(token)

    tasks = []
    try:
        # Reproduce chat-19: A's participant registers before its Owner; B intervenes.
        await run("insight", "A")
        await run("architecture", "B")
        await run("contract", "B")
        await run("business", "A")
        tasks = [bootstrap._owner_runs[(SESSION, key)] for key in plans]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        assert launched == ["insight", "architecture", "contract", "business"]
        for key, plan in plans.items():
            rows = group_service.group_organization_teams(SESSION, plan["organization_id"])
            assert {row["member_name"] for row in rows} == set(plan["members"])
            assert await bootstrap._wait_for_members(runtime, session_id=SESSION,
                organization_id=plan["organization_id"], members=plan["members"]) == []
        first_task = tasks[list(plans).index(first)]
        finish[first].set()
        await asyncio.wait_for(asyncio.shield(first_task), 2)
        other = "B" if first == "A" else "A"
        expected = {bootstrap.instance_team_id(SESSION, member) for member in plans[first]["members"]}
        assert paused == [expected]
        assert not group_service.group_organization_teams(SESSION, plans[first]["organization_id"])
        assert len(group_service.group_organization_teams(SESSION, plans[other]["organization_id"])) == 2
        assert await bootstrap._wait_for_members(runtime, session_id=SESSION,
            organization_id=plans[other]["organization_id"], members=plans[other]["members"]) == []
        finish[other].set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert not group_service.group_organization_teams(SESSION)
        assert (other, "completed") in cards
        assert (first, "failed" if fail_first else "completed") in cards
        assert (plans[other]["owner_member"], "result-" + other) in outputs
        if fail_first:
            assert errors == [(plans[first]["owner_member"], "simulated task failure")]
    finally:
        for event in finish.values():
            event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bootstrap._member_turns.clear()
        bootstrap._accepted_plans.clear()
        bootstrap._proxy_backends.clear()
        bootstrap._proxy_tools.clear()

@pytest.mark.asyncio
async def test_group_deletion_dissolves_all_orgs_without_a_global_current_org(monkeypatch):
    group = _MemoryGroup(monkeypatch)
    group.state["members"] = [dict(kind="agent_group", id=name, member_name=name) for name in ["a", "b", "c", "d"]]
    for org, members in [("A", ["a", "b"]), ("B", ["c", "d"])]:
        for index, member in enumerate(members):
            group_service.upsert_group_organization_team(SESSION, org, dict(
                member_name=member, team_id=bootstrap.instance_team_id(SESSION, member),
                role="owner" if index == 0 else "member"))
    group.state.pop("organization_id")
    runtime = SimpleNamespace(dissolve_organization=AsyncMock())
    monkeypatch.setattr(bootstrap, "_organization_runtime", lambda: (object(), runtime, object()))
    ready = AsyncMock()
    sync = AsyncMock()
    monkeypatch.setattr(bootstrap, "ensure_member_instance", ready)
    monkeypatch.setattr(bootstrap, "sync_organization_membership", sync)
    await bootstrap.dissolve_group_organization(SESSION)
    assert [call.kwargs["organization_id"] for call in runtime.dissolve_organization.await_args_list] == ["A", "B"]
    assert [call.kwargs["organization_id"] for call in ready.await_args_list] == ["A", "A", "B", "B"]
    assert [{row["member_name"] for row in call.kwargs["teams"]} for call in sync.await_args_list] == [{"a", "b"}, {"c", "d"}]


def test_unknown_legacy_members_are_not_assigned_to_a_later_organization(monkeypatch):
    _MemoryGroup(monkeypatch, organization_teams=[dict(member_name="unknown", team_id="old")])
    group_service.upsert_group_organization_team(SESSION, "A", dict(member_name="a", team_id="ta"))
    group_service.upsert_group_organization_team(SESSION, "B", dict(member_name="b", team_id="tb"))
    assert [row["team_id"] for row in group_service.group_organization_teams(SESSION, "A")] == ["ta"]
    assert [row["team_id"] for row in group_service.group_organization_teams(SESSION, "B")] == ["tb"]
