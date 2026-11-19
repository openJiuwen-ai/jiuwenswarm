"""A consumed group message owns its tool calls independently of older work."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.tool import ToolOutput

from jiuwenswarm.agents.swarm import group_chat
from jiuwenswarm.agents.swarm import group_org_bootstrap as bootstrap
from jiuwenswarm.agents.swarm import group_turn
from jiuwenswarm.agents.swarm import group_execution as policy
from jiuwenswarm.server.runtime import group_service
from tests.agents.swarm.test_group_org_bootstrap import _MemoryGroup, SESSION


@pytest.fixture
def bound_tool(monkeypatch):
    turn = SimpleNamespace(session_id=SESSION, member_name="business", client_message_id="new", route="organization")
    monkeypatch.setattr(group_turn, "current_group_turn", lambda *_: turn)
    old = dict(plan_id="old", status="running", members=["business", "insight"],
               owner_member="business", created_at=bootstrap._now_ms(), body="Review")
    new = {**old, "plan_id": "new", "created_at": old["created_at"] + 1, "body": "Add three suggestions", "status": "pending"}
    group = _MemoryGroup(monkeypatch, organization_plans=[old, new])
    tool = group_chat.RunTeam(SimpleNamespace(member_name="business", team_name="outer", conversation_mode="group_chat"),
                              SimpleNamespace(session_id=SESSION), {})
    bootstrap.bind_member_turn(SESSION, "business", "old")
    bootstrap.bind_member_turn(SESSION, "business", "new")
    yield tool, turn, group
    bootstrap._member_turns.clear()
    bootstrap._accepted_plans.clear()
    bootstrap._proxy_backends.pop((SESSION, "business"), None)
    bootstrap._proxy_tools.pop((SESSION, "business"), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "continue", "update"])
async def test_bound_org_queues_itself_even_with_old_target_and_missing_run_id(bound_tool, monkeypatch, action):
    tool, _, group = bound_tool
    queued = Mock(side_effect=lambda **kw: ToolOutput(success=True, data={"status": "queued", "plan_id": kw["plan"]["plan_id"]}))
    launched = AsyncMock()
    adjusted = AsyncMock()
    monkeypatch.setattr(bootstrap, "queue_organization_plan", queued)
    monkeypatch.setattr(bootstrap, "run_organization_member", launched)
    monkeypatch.setattr(tool, "_adjust", adjusted)
    for _ in range(2):
        result = await tool.invoke({"action": action, "execution_id": "old-card", "content": "Change old task"})
        assert result.success and result.data["plan_id"] == "new"
        assert result.data["execution_id"] == bootstrap.organization_execution_id(SESSION, "new")
    assert queued.call_args.kwargs["plan"]["body"] == "Add three suggestions"
    assert bootstrap.peek_member_turn(SESSION, "business") == "old"
    assert group.state["organization_plans"][0]["body"] == "Review"
    launched.assert_not_awaited()
    adjusted.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_org_launch_failure_keeps_binding_for_retry(bound_tool, monkeypatch):
    tool, _, group = bound_tool
    group.state["organization_plans"][0]["status"] = "done"
    launched = AsyncMock(side_effect=[ToolOutput(success=False, error="launch failed"), ToolOutput(success=True, data={"accepted": True})])
    monkeypatch.setattr(bootstrap, "run_organization_member", launched)
    assert not (await tool.invoke({"action": "update"})).success
    assert "new" in bootstrap._member_turns[(SESSION, "business")]
    assert not bootstrap.organization_plan_accepted(SESSION, "new", "business")
    assert (await tool.invoke({"action": "update"})).success
    assert "new" not in bootstrap._member_turns[(SESSION, "business")]
    assert launched.call_args.kwargs["plan"]["plan_id"] == "new"


@pytest.mark.asyncio
async def test_bound_finished_org_never_restarts_or_falls_back(bound_tool, monkeypatch):
    tool, _, group = bound_tool
    group.state["organization_plans"][1]["status"] = "done"
    launched = AsyncMock()
    monkeypatch.setattr(bootstrap, "run_organization_member", launched)
    result = await tool.invoke({"action": "update", "execution_id": "old-card"})
    assert result.data["status"] == "done" and result.data["plan_id"] == "new"
    launched.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_cancel_is_not_converted_into_a_launch(bound_tool, monkeypatch):
    tool, _, _ = bound_tool
    launched = AsyncMock()
    monkeypatch.setattr(bootstrap, "run_organization_member", launched)
    assert not (await tool.invoke({"action": "cancel", "execution_id": "old-card"})).success
    launched.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "cancel"])
async def test_direct_message_preserves_adjustment_even_with_older_org_pending(bound_tool, monkeypatch, action):
    tool, turn, _ = bound_tool
    turn.route = "direct"
    turn.client_message_id = "direct"
    adjusted = AsyncMock(return_value=ToolOutput(success=True, data={"unchanged": True}))
    monkeypatch.setattr(tool, "_adjust", adjusted)
    inputs = {"action": action, "execution_id": "selected-card", "content": "Follow up"}
    assert (await tool.invoke(inputs)).data["unchanged"]
    adjusted.assert_awaited_once_with(inputs)


@pytest.mark.asyncio
async def test_direct_start_cannot_be_stolen_by_unaccepted_older_org(bound_tool, monkeypatch):
    tool, turn, _ = bound_tool
    turn.route = "direct"
    turn.client_message_id = "single-or-mixed"
    # Use the unmanaged private runner to observe the unchanged direct branch.
    monkeypatch.setattr(group_service, "read_group", lambda _: None)
    monkeypatch.setattr(bootstrap, "pending_organization_plan", lambda *_: {"plan_id": "old"})
    private = AsyncMock(return_value=ToolOutput(success=True, data={"private": True}))
    organization = AsyncMock()
    monkeypatch.setattr(tool, "_run", private)
    monkeypatch.setattr(bootstrap, "run_organization_member", organization)
    assert (await tool.invoke({"run_id": "direct", "content": "Single-team work"})).data["private"]
    organization.assert_not_awaited()
    assert private.call_args.args[1] == "Single-team work"


@pytest.mark.asyncio
async def test_unknown_message_cannot_adjust_an_arbitrary_old_org(bound_tool, monkeypatch):
    tool, turn, _ = bound_tool
    turn.route = "unknown"
    adjusted = AsyncMock()
    monkeypatch.setattr(tool, "_adjust", adjusted)
    assert not (await tool.invoke({"action": "update", "execution_id": "old-card"})).success
    adjusted.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_batch_does_not_resume_a_previous_organization_instance(bound_tool, monkeypatch):
    tool, turn, _ = bound_tool
    turn.route = "direct"
    turn.client_message_id = None
    turn.mixed_mentions = True
    monkeypatch.setattr(group_service, "read_group", lambda _: None)
    monkeypatch.setattr(bootstrap, "pending_organization_plan", lambda *_: None)
    saved = AsyncMock(return_value={"kind": "organization"})
    private = AsyncMock(return_value=ToolOutput(success=True, data={"private": True}))
    monkeypatch.setattr(bootstrap, "saved_organization_instance", saved)
    monkeypatch.setattr(tool, "_run", private)
    assert (await tool.invoke({"run_id": "mixed", "content": "Mixed work"})).data["private"]
    saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_background_does_not_inherit_proxy_turn(monkeypatch):
    plan = dict(plan_id="isolated", status="pending", members=["business", "insight"],
                owner_member="business", created_at=bootstrap._now_ms(), body="Task")
    _MemoryGroup(monkeypatch, organization_plans=[plan])
    tool = group_chat.RunTeam(SimpleNamespace(member_name="business", team_name="outer", conversation_mode="group_chat"),
                              SimpleNamespace(session_id=SESSION), {})
    children = []

    async def background():
        await asyncio.sleep(0)
        return group_turn.current_group_turn(SESSION, "business")

    async def launch(**kwargs):
        assert kwargs["plan"]["plan_id"] == "isolated"
        children.append(asyncio.create_task(background()))
        return ToolOutput(success=True, data={"accepted": True})

    monkeypatch.setattr(bootstrap, "run_organization_member", launch)
    turn = group_turn.GroupTurn(SESSION, "business", "isolated", "organization")
    token = group_turn.bind_group_turn(turn)
    try:
        assert (await tool.invoke({"action": "update", "execution_id": "wrong"})).success
        assert group_turn.current_group_turn(SESSION, "business") is turn
        assert await children[0] is None
    finally:
        group_turn.reset_group_turn(token)
        bootstrap.release_organization_plan_claim(SESSION, "isolated", "business")
        bootstrap._proxy_backends.pop((SESSION, "business"), None)
        bootstrap._proxy_tools.pop((SESSION, "business"), None)


@pytest.mark.asyncio
async def test_consumed_core_notice_routes_real_tool_through_rail(tmp_path, monkeypatch):
    from openjiuwen.agent_teams.i18n import STRINGS
    from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, UserMessageInputs, ToolCallInputs

    plan = dict(plan_id="source-b", status="pending", members=["business", "insight"],
                owner_member="business", created_at=bootstrap._now_ms(), body="B requirements")
    group = _MemoryGroup(monkeypatch, organization_plans=[plan])
    monkeypatch.setattr(policy, "read_group", lambda _: group.state)
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({"message_id": "message-b", "client_message_id": "source-b",
                               "sender": "user", "mentions": ["business", "insight"]}))
    backend = SimpleNamespace(member_name="business", team_name="outer", conversation_mode="group_chat",
                              group_conversation=AsyncMock(return_value=SimpleNamespace(history_path=path)))
    monkeypatch.setattr(policy, "get_team_backend", lambda _: backend)
    context = SimpleNamespace(session_id=SESSION, member_name="business")
    rail = policy.GroupTaskPolicyRail(context)
    notification = STRINGS["cn"]["conversation.context"].format(
        from_timestamp=0, to_timestamp=1, trigger_message_id="message-b", path=str(path), excerpts="")
    ctx = AgentCallbackContext(agent=None, inputs=UserMessageInputs(parts=[notification]))
    await rail.on_user_message(ctx)
    call_ctx = AgentCallbackContext(agent=None, inputs=ToolCallInputs(tool_name="run_team"), extra=dict(ctx.extra))
    launched = AsyncMock(return_value=ToolOutput(success=True, data={"accepted": True}))
    monkeypatch.setattr(bootstrap, "run_organization_member", launched)
    tool = group_chat.RunTeam(backend, context, {})
    try:
        await rail.before_tool_call(call_ctx)
        output = await tool.invoke({"action": "update", "execution_id": "old-org-id"})
        assert output.data["plan_id"] == "source-b"
        assert launched.call_args.kwargs["plan"]["body"] == "B requirements"
    finally:
        await rail.after_tool_call(call_ctx)
        bootstrap.release_organization_plan_claim(SESSION, "source-b", "business")
        bootstrap._proxy_backends.pop((SESSION, "business"), None)
        bootstrap._proxy_tools.pop((SESSION, "business"), None)
    assert group_turn.current_group_turn(SESSION, "business") is None
