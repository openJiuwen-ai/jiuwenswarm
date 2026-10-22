"""Private-team background ownership, interaction, continuation and provenance."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.swarm import group_chat as gc, assembly
from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail
from jiuwenswarm.server.runtime.group_execution import GroupExecutions
from jiuwenswarm.server.runtime import group_service as gs
from tests.unit_tests.server.test_group_service import service, create


@pytest.fixture
async def private_run(service, tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness import team
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    monkeypatch.setattr(team, "get_team_manager", lambda *args: manager)
    monkeypatch.setattr(assembly, "enrich_team_spec_for_swarm", lambda *args, **kwargs: None)
    monkeypatch.setattr(gc.Runner, "pause_agent_team", AsyncMock(return_value=True))
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    pushes = AsyncMock(return_value=True)
    monkeypatch.setattr(WebSocketGatewayPushTransport, "send_push", pushes)
    group = await create(service, members=[{"kind": "agent_group", "id": "group", "member_name": "advisor"}])
    raw = gs.read_group(group["group_id"])
    tool = next(t for t in raw["compiled"]["advisor"]["template"]["tools"] if t["type"] == gc.RUN_TEAM)
    backend = SimpleNamespace(
        member_name="advisor", team_name=group["team_name"], conversation_mode="group_chat",
        group_conversation=AsyncMock(return_value=SimpleNamespace(history_path=tmp_path / "history.jsonl")),
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="result"))),
    )
    context = SimpleNamespace(
        session_id=group["group_id"], channel_id="web", mode="team", user_id="owner",
        trusted_dirs=None, project_dir=None, member_name="advisor",
    )
    store = GroupExecutions(group["group_id"])
    child_spec = dict(tool["params"]["child_spec"])
    child_spec["workspace"] = {"root_path": str(tmp_path / "private-workspace")}
    proxy = gc.RunTeam(backend, context, child_spec)
    value = SimpleNamespace(proxy=proxy, backend=backend, context=context, store=store, manager=manager, pushes=pushes)
    yield value
    for record in store.read().values():
        worker = manager.get_stream_task(record.get("team_session_id"))
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def private_session(run, output):
    return run.store.get(output.data["execution_id"])["team_session_id"]


async def wait_worker(run, accepted):
    worker = run.manager.get_stream_task(private_session(run, accepted))
    await asyncio.wait_for(asyncio.shield(worker), 2)
    return run.store.get(accepted.data["execution_id"])


async def submit_private(child, *, task=False):
    key = next(t.params["key"] for t in child.agents["leader"].tools if t.type == gc.COMPLETE_RUN)
    execution_id = gc._private_runs[key]["execution_id"]
    if task:
        started = await gc.BeginTeamTask(key).invoke({"execution_id": execution_id})
        assert started.success
    output = await gc.CompleteTeamRun(key).invoke({"result": "Final", "execution_id": execution_id})
    assert output.success


@pytest.mark.asyncio
async def test_background_proxy_remains_available_and_adjusts_same_execution(private_run, monkeypatch):
    run = private_run
    started = asyncio.Event()
    release = asyncio.Event()

    async def stream(child, inputs, *, session):
        started.set()
        await release.wait()
        await submit_private(child)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await asyncio.wait_for(run.proxy.invoke({"run_id": "one", "task": "Research"}), 1)
    assert accepted.success and accepted.data["accepted"]
    await started.wait()
    key = accepted.data["execution_id"]
    await GroupTaskPolicyRail(run.context).after_invoke(None)
    assert run.store.get(key)["status"] == "running"
    # A subsequent model invocation can use the adjustment tool while the private worker waits.
    delivered = AsyncMock(return_value=SimpleNamespace(ok=True, reason=None))
    monkeypatch.setattr(gc.Runner, "interact_agent_team", delivered)
    adjusted = await run.proxy.invoke({
        "execution_id": key, "action": "update", "content": "Focus on compatibility",
    })
    assert adjusted.success and adjusted.data["instruction_delivered"]
    assert delivered.await_args.kwargs["session_id"] == private_session(run, accepted)
    assert delivered.await_args.args[0].body == "Focus on compatibility"
    repeat = await run.proxy.invoke({"run_id": "one", "task": "Research"})
    assert repeat.data["execution_id"] == key and len(run.store.read()) == 1
    worker = run.manager.get_stream_task(private_session(run, accepted))
    release.set()
    await asyncio.wait_for(asyncio.shield(worker), 2)
    assert run.store.get(key)["status"] == "completed"
    assert run.store.get(key)["presentation"] == "message"


@pytest.mark.asyncio
async def test_continuation_reuses_team_session_but_not_card_or_observation(private_run, monkeypatch):
    run = private_run
    calls = []

    async def stream(child, inputs, *, session):
        calls.append((child.team_name, session, child.workspace.root_path, inputs))
        await submit_private(child, task=len(calls) == 1)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    first = await run.proxy.invoke({"run_id": "one", "task": "Research"})
    original = await wait_worker(run, first)
    second = await run.proxy.invoke({
        "run_id": "two", "task": "Answer one follow-up question",
        "continue_from_execution_id": original["execution_id"],
    })
    followup = await wait_worker(run, second)
    assert first.data["execution_id"] != second.data["execution_id"]
    assert calls[0][:3] == calls[1][:3]
    assert calls[1][3]["query"] == "Answer one follow-up question"
    assert original["presentation"] == "card"
    assert followup["presentation"] == "message"
    assert followup["previous_execution_id"] == original["execution_id"]
    assert run.store.get(original["execution_id"])["status"] == "completed"
    assert len(run.store.read()) == 2
    run.backend.append_group_message.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_waits_for_private_stream_cleanup_and_preserves_card(private_run, monkeypatch):
    run = private_run
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stream(child, inputs, *, session):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "Research"})
    await started.wait()
    result = await run.proxy.invoke({
        "execution_id": accepted.data["execution_id"], "action": "cancel", "reason": "No longer needed",
    })
    assert result.success and result.data["status"] == "cancelled"
    assert stopped.is_set()
    assert run.store.get(accepted.data["execution_id"])["cancellation_reason"] == "No longer needed"
    assert run.manager.get_stream_task(private_session(run, accepted)) is None
    run.backend.append_group_message.assert_not_awaited()
    assert not gc._results and not gc._private_runs


@pytest.mark.asyncio
async def test_adjustment_rejects_delivery_failure_and_wrong_member(private_run, monkeypatch):
    run = private_run
    started = asyncio.Event()

    async def stream(child, inputs, *, session):
        started.set()
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "Research"})
    await started.wait()
    key = accepted.data["execution_id"]
    delivered = AsyncMock(return_value=SimpleNamespace(ok=False, reason="not_active"))
    monkeypatch.setattr(gc.Runner, "interact_agent_team", delivered)
    tool = run.proxy
    result = await tool.invoke({"execution_id": key, "action": "update", "content": "Change"})
    assert not result.success and "not_active" in result.error
    assert not run.store.get(key).get("adjustments")
    outsider = gc.RunTeam(SimpleNamespace(
        member_name="other", conversation_mode="group_chat",
    ), run.context, run.proxy.child_spec)
    denied = await outsider.invoke({"execution_id": key, "action": "cancel"})
    assert not denied.success
    assert run.store.get(key)["status"] == "running"


@pytest.mark.asyncio
async def test_unknown_continuation_does_not_create_orphan_card(private_run):
    run = private_run
    result = await run.proxy.invoke({
        "run_id": "one", "task": "Research", "continue_from_execution_id": "missing",
    })
    assert not result.success and run.store.read() == {}


@pytest.mark.asyncio
async def test_stale_result_cannot_complete_a_followup(private_run, monkeypatch):
    run = private_run
    count = 0
    followup_started = asyncio.Event()
    release = asyncio.Event()

    async def stream(child, inputs, *, session):
        nonlocal count
        count += 1
        key = next(t.params["key"] for t in child.agents["leader"].tools if t.type == gc.COMPLETE_RUN)
        if count == 2:
            followup_started.set()
            await release.wait()
            rejected = await gc.CompleteTeamRun(key).invoke({
                "result": "Stale", "execution_id": first.data["execution_id"],
            })
            assert not rejected.success
            assert not gc._results[key].done()
        await submit_private(child)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    first = await run.proxy.invoke({"run_id": "one", "task": "First"})
    await wait_worker(run, first)
    second = await run.proxy.invoke({
        "run_id": "two", "task": "Second", "continue_from_execution_id": first.data["execution_id"],
    })
    await asyncio.wait_for(followup_started.wait(), 2)
    repeat = await run.proxy.invoke({
        "run_id": "two", "task": "Second", "continue_from_execution_id": first.data["execution_id"],
    })
    assert repeat.success and repeat.data["execution_id"] == second.data["execution_id"]
    release.set()
    result = await wait_worker(run, second)
    assert result["status"] == "completed" and result["result"] == "Final"


@pytest.mark.asyncio
async def test_background_failure_and_pause_failure_are_not_success(private_run, monkeypatch):
    run = private_run

    async def stream(child, inputs, *, session):
        yield SimpleNamespace(payload={"event_type": "team.error"})

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "First"})
    assert (await wait_worker(run, accepted))["status"] == "failed"
    run.backend.append_group_message.assert_not_awaited()

    async def success(child, inputs, *, session):
        await submit_private(child)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", success)
    monkeypatch.setattr(gc.Runner, "pause_agent_team", AsyncMock(return_value=False))
    second = await run.proxy.invoke({"run_id": "two", "task": "Second"})
    assert (await wait_worker(run, second))["status"] == "failed"
    run.backend.append_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_member_does_not_create_another_card(private_run, monkeypatch):
    run = private_run

    async def stream(child, inputs, *, session):
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "First"})
    result = await run.proxy.invoke({"run_id": "two", "task": "Different"})
    assert not result.success
    assert len(run.store.read()) == 1
    cancelled = await run.proxy.invoke({
        "execution_id": accepted.data["execution_id"], "action": "cancel",
    })
    assert cancelled.success and cancelled.data["status"] == "cancelled"


@pytest.mark.asyncio
async def test_group_delete_stops_worker_even_before_run_ticket_is_written(private_run, service, monkeypatch):
    run = private_run

    async def not_started(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(run.proxy, "_run", not_started)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "First"})
    worker = run.manager.get_stream_task(private_session(run, accepted))
    service.manager_factory = lambda _: run.manager
    result = await service.delete(run.context.session_id, user_id="owner", channel_id="web")
    assert result["deleted"]
    assert worker.done()
    assert gs.read_group(run.context.session_id) is None


@pytest.mark.asyncio
async def test_two_proxies_keep_interleaved_task_events_on_their_own_cards(private_run, service, monkeypatch):
    run = private_run
    await service.add_member(
        run.context.session_id,
        {"member": {"kind": "agent_group", "id": "group", "member_name": "other"}},
        user_id="owner", channel_id="web", request_id="add-other",
    )
    other_backend = SimpleNamespace(**{**vars(run.backend), "member_name": "other"})
    other_context = SimpleNamespace(**{**vars(run.context), "member_name": "other"})
    other = gc.RunTeam(other_backend, other_context, run.proxy.child_spec)
    ready = asyncio.Event()
    release = asyncio.Event()
    started = []

    async def stream(child, inputs, *, session):
        started.append(session)
        yield SimpleNamespace(payload={"event_type": "team.task", "event": {
            "task_id": "same-local-id", "title": inputs["query"], "status": "in_progress",
            "team_id": child.team_name,
        }})
        if len(started) == 2:
            ready.set()
        await release.wait()
        await submit_private(child)
        await asyncio.Event().wait()

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    first = await run.proxy.invoke({"run_id": "one", "task": "First task"})
    second = await other.invoke({"run_id": "one", "task": "Second task"})
    workers = [run.manager.get_stream_task(private_session(run, result)) for result in (first, second)]
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert private_session(run, first) != private_session(run, second)
        records = [run.store.get(result.data["execution_id"]) for result in (first, second)]
        assert records[0]["task_details"]["same-local-id"]["title"] == "First task"
        assert records[1]["task_details"]["same-local-id"]["title"] == "Second task"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*workers), 2)
    assert all(run.store.get(result.data["execution_id"])["status"] == "completed" for result in (first, second))


@pytest.mark.asyncio
async def test_core_member_execution_counts_participation_without_native_worker_rail(private_run, monkeypatch):
    run = private_run

    async def stream(child, inputs, *, session):
        yield SimpleNamespace(payload={"event_type": "team.member", "event": {
            "type": "team.member.execution_changed", "member_id": "cli-worker", "new_status": "running",
        }})
        await submit_private(child)
        await asyncio.Event().wait()

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"run_id": "one", "task": "Research"})
    result = await wait_worker(run, accepted)
    assert result["presentation"] == "message"

@pytest.mark.asyncio
async def test_pushes_and_queries_hide_internal_fields_without_losing_runtime_state(service, monkeypatch):
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(WebSocketGatewayPushTransport, "send_push", sender)
    group = await create(service, members=[{"kind": "agent_group", "id": "group", "member_name": "advisor"}])
    store = GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Research", "one"))[0]
    key = record["execution_id"]
    internal = {
        "participants": ["worker"], "build_team_called": True,
        "previous_execution_id": "old", "team_session_id": "private-session", "background": True,
        "private_team_key": "private-key",
    }
    await store.update(key, **internal)
    snapshot = await service.execution_get(
        group["group_id"], {"execution_id": key}, user_id="owner", channel_id="web",
    )
    page = await service.execution_list(group["group_id"], {}, user_id="owner", channel_id="web")
    assert not internal.keys() & snapshot.keys()
    assert not internal.keys() & page["executions"][0].keys()
    assert "result_source" not in snapshot
    backend = SimpleNamespace(
        member_name="advisor",
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="final"))),
    )
    await store.finish(key, backend, "Done", [])
    for call in sender.await_args_list:
        assert not internal.keys() & call.args[0]["payload"].keys()
    assert store.get(key)["team_session_id"] == "private-session"
    assert store.get(key)["background"] is True
    assert store.get(key)["previous_execution_id"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("substantive", [False, True])
async def test_unified_start_creates_card_only_for_substantive_work(private_run, service, monkeypatch, substantive):
    run = private_run
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(child, inputs, *, session):
        entered.set()
        await release.wait()
        await submit_private(child, task=substantive)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"action": "start", "run_id": "one", "content": "Review this"})
    assert accepted.success
    await entered.wait()
    assert run.pushes.await_count == 0
    page = await service.execution_list(
        run.context.session_id, {}, user_id="owner", channel_id="web",
    )
    assert page["total"] == 0
    release.set()
    result = await wait_worker(run, accepted)
    assert result["status"] == "completed"
    events = [call.args[0]["payload"]["event_type"] for call in run.pushes.await_args_list]
    if substantive:
        assert events[0] == "group.execution.created"
        assert events[-1] == "group.execution.updated"
        assert events.count("group.execution.created") == 1
    else:
        assert events == []
    run.backend.append_group_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unified_continue_reuses_direct_answer_context(private_run, monkeypatch):
    run = private_run
    sessions = []

    async def stream(child, inputs, *, session):
        sessions.append(session)
        await submit_private(child, task=len(sessions) == 2)
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    first = await run.proxy.invoke({"action": "start", "run_id": "one", "content": "Explain"})
    original = await wait_worker(run, first)
    second = await run.proxy.invoke({
        "action": "continue", "run_id": "two", "content": "Implement it",
        "execution_id": original["execution_id"],
    })
    assert second.success
    result = await wait_worker(run, second)
    assert sessions[0] == sessions[1]
    assert original["execution_id"] != result["execution_id"]
    assert original["presentation"] == "message"
    assert result["presentation"] == "card"


@pytest.mark.asyncio
async def test_private_work_gate_and_stale_start(private_run, monkeypatch):
    run = private_run

    async def stream(child, inputs, *, session):
        key = next(t.params["key"] for t in child.agents["leader"].tools if t.type == gc.COMPLETE_RUN)
        execution_id = gc._private_runs[key]["execution_id"]
        policy = gc.PrivateTaskPolicy(None, key)
        ctx = SimpleNamespace(
            inputs=SimpleNamespace(tool_name="build_team", tool_call=None), extra={},
        )
        await policy.before_tool_call(ctx)
        assert ctx.extra["_skip_tool"]
        stale = await gc.BeginTeamTask(key).invoke({"execution_id": "old"})
        assert not stale.success
        started = await gc.BeginTeamTask(key).invoke({"execution_id": execution_id})
        assert started.success
        repeated = await gc.BeginTeamTask(key).invoke({"execution_id": execution_id})
        assert repeated.success
        ctx.extra.clear()
        await policy.before_tool_call(ctx)
        assert not ctx.extra
        await submit_private(child)
        late = await gc.BeginTeamTask(key).invoke({"execution_id": execution_id})
        assert not late.success
        await asyncio.Event().wait()
        yield None

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"action": "start", "run_id": "one", "content": "Work"})
    result = await wait_worker(run, accepted)
    assert result["status"] == "completed"
    events = [call.args[0]["payload"]["event_type"] for call in run.pushes.await_args_list]
    assert events.count("group.execution.created") == 1


@pytest.mark.asyncio
async def test_read_group_removes_retired_task_tools(service):
    import json

    group = await create(service, members=[{"kind": "agent_group", "id": "group", "member_name": "advisor"}])
    path = gs.group_path(group["group_id"])
    state = json.loads(path.read_text(encoding="utf-8"))
    state["compiled"]["advisor"]["template"]["tools"].extend([
        {"type": "swarm.group_adjust_task"}, {"type": "swarm.group_submit_task"},
    ])
    path.write_text(json.dumps(state), encoding="utf-8")
    loaded = gs.read_group(group["group_id"])
    tools = loaded["compiled"]["advisor"]["template"]["tools"]
    retired = {"swarm.group_adjust_task", "swarm.group_submit_task"}
    assert all(tool["type"] not in retired for tool in tools)
    assert any(tool["type"] == gc.RUN_TEAM for tool in tools)


@pytest.mark.asyncio
async def test_private_leader_question_waits_for_answer_without_ending_execution(private_run, service, monkeypatch):
    import json
    from openjiuwen.core.foundation.llm import ToolCall
    from jiuwenswarm.agents.swarm.group_questions import GroupAskUserRail
    from jiuwenswarm.server.runtime.group_questions import GroupQuestions

    run = private_run
    asked = asyncio.Event()
    resumed = asyncio.Event()

    class Owner:
        async def send_input(self, request):
            assert request.inputs["query"].user_inputs["private-question"]["answers"] == {"Budget?": "Low"}
            resumed.set()

    owner = Owner()

    async def stream(child, inputs, *, session):
        context = SimpleNamespace(session_id=session, member_name="team_leader", language="en")
        key = next(t.params["key"] for t in child.agents["leader"].tools if t.type == gc.COMPLETE_RUN)
        current = gc._private_runs[key]
        rail = GroupAskUserRail(context, {"group_id": run.context.session_id,
                                         "member_name": "advisor", "execution_id": current["execution_id"]})
        ctx = SimpleNamespace(agent=owner)
        await rail.before_invoke(ctx)
        call = ToolCall(id="private-question", type="function", name="ask_user", arguments=json.dumps({
            "questions": [{"question": "Budget?", "options": [{"label": "Low"}, {"label": "High"}]}],
        }))
        await rail.resolve_interrupt(ctx, call, None)
        yield SimpleNamespace(payload={"event_type": "team.idle"})
        asked.set()
        await resumed.wait()
        await submit_private(child, task=True)
        await asyncio.Event().wait()

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    accepted = await run.proxy.invoke({"action": "start", "run_id": "question", "content": "Plan a trip"})
    await asyncio.wait_for(asked.wait(), 2)
    pending = service.get(run.context.session_id, user_id="owner", channel_id="web")["pending_questions"]
    assert len(pending) == 1
    record = run.store.get(accepted.data["execution_id"])
    assert record["status"] == "running" and record["presentation"] == "message"
    await GroupQuestions(run.context.session_id).answer(
        pending[0]["request_id"], [{"question": "Budget?", "selected_options": ["Low"]}],
    )
    result = await wait_worker(run, accepted)
    assert result["status"] == "completed" and result["presentation"] == "card"
    assert GroupQuestions(run.context.session_id).pending() == []
    run.backend.append_group_message.assert_awaited_once()
