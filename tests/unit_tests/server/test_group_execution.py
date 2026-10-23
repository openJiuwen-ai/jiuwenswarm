"""Execution cards distinguish public conversation from task lifecycle events."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime import group_execution as ge
from jiuwenswarm.server.runtime import group_service as gs
from tests.unit_tests.server.test_group_service import service, create


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    monkeypatch.setattr(ge.GroupExecutions, "publish", AsyncMock())


async def make_group(service, *, kind="agent_template"):
    return await create(service, members=[{
        "kind": kind, "id": "one", "member_name": "advisor",
    }])


@pytest.mark.asyncio
async def test_execution_creation_idempotency_busy_and_owner_isolation(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    first = (await store.create(["advisor"], "Review code", "one"))[0]
    assert first["execution_kind"] == "agent" and first["progress"] is None
    assert await store.create(["advisor"], "Review code", "one") == [first]
    with pytest.raises(gs.GroupError, match="another task"):
        await store.create(["advisor"], "Changed", "one")
    with pytest.raises(gs.GroupError, match="active execution"):
        await store.create(["advisor"], "Other", "two")
    with pytest.raises(gs.GroupError, match="not found"):
        await service.execution_list(group["group_id"], {}, user_id="other", channel_id="web")
    public = await service.execution_get(
        group["group_id"], {"execution_id": first["execution_id"]}, user_id="owner", channel_id="web",
    )
    assert public["execution_id"] == first["execution_id"] and "owner" not in public
    page = await service.execution_list(group["group_id"], {}, user_id="owner", channel_id="web")
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_core_progress_does_not_complete_execution_and_revisions_ignore_duplicates(service):
    group = await make_group(service, kind="agent_group")
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    key = record["execution_id"]
    await store.update(key, status="running")
    payload = {"event_type": "team.task", "event": {"task_id": "task-one", "status": "completed"}}
    await store.task_event(key, payload)
    current = store.get(key)
    assert current["progress"] == {"completed": 1, "total": 1}
    assert current["status"] == "running"
    await store.task_event(key, payload)
    assert store.get(key)["revision"] == current["revision"]
    await store.update(key, status="failed", phase="failed")
    await store.task_event(key, payload)
    assert store.get(key)["status"] == "failed"


@pytest.mark.asyncio
async def test_result_retries_repair_publication_without_duplicate_message(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    backend = SimpleNamespace(
        member_name="advisor",
        append_group_message=AsyncMock(side_effect=[
            OSError("disconnected"), SimpleNamespace(message=SimpleNamespace(message_id="public-result")),
        ]),
    )
    attachments = [{"name": "report.md", "path": "artifacts/report.md"}]
    with pytest.raises(OSError):
        await store.finish(key, backend, "Done", attachments)
    assert store.get(key)["phase"] == "publishing"
    result = await store.finish(key, backend, "Done", attachments)
    assert result["status"] == "completed" and result["attachments"] == attachments
    assert backend.append_group_message.await_args_list[0] == backend.append_group_message.await_args_list[1]
    await store.finish(key, backend, "Done", attachments)
    assert backend.append_group_message.await_count == 2
    with pytest.raises(gs.GroupError, match="another result"):
        await store.finish(key, backend, "Changed", attachments)


@pytest.mark.asyncio
async def test_finish_cannot_publish_another_members_task(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    backend = SimpleNamespace(member_name="impostor", append_group_message=AsyncMock())
    with pytest.raises(gs.GroupError, match="not found"):
        await store.finish(key, backend, "Done", [])
    backend.append_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_cannot_recreate_deleted_group(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    gs.group_path(group["group_id"]).unlink()
    with pytest.raises(gs.GroupError, match="unavailable"):
        await store.update(key, status="running")
    assert not gs.group_path(group["group_id"]).exists()


@pytest.mark.asyncio
async def test_task_policy_injects_assignment_blocks_progress_and_detects_missing_result(service):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail

    group = await make_group(service)
    key = (await ge.GroupExecutions(group["group_id"]).create(["advisor"], "Review", "one"))[0]["execution_id"]
    rail = GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name="advisor"))
    builder = Mock()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(system_prompt_builder=builder), extra={},
        inputs=SimpleNamespace(tool_name="group_send_message", tool_call=None),
    )
    await rail.before_invoke(ctx)
    assert key in builder.add_section.call_args.args[0].content["en"]
    await rail.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"]
    await rail.after_invoke(ctx)
    assert ge.GroupExecutions(group["group_id"]).get(key)["status"] == "interrupted"


@pytest.mark.asyncio
async def test_public_chat_creates_no_execution_and_internal_frames_are_hidden(service):
    group = await make_group(service)
    for kind in ("chat.reasoning", "chat.delta", "chat.final", "chat.tool_call", "chat.tool_result", "team.task"):
        assert await ge.project_group_stream_event(group["group_id"], {"event_type": kind}) is None
    assert ge.GroupExecutions(group["group_id"]).read() == {}
    event = {"event_type": "team.runtime_ready"}
    assert await ge.project_group_stream_event(group["group_id"], event) == event
    assert await ge.project_group_stream_event("ordinary-session", {"event_type": "chat.final"}) is not None


@pytest.mark.asyncio
async def test_dispatch_does_not_create_tasks_before_runtime_admission(service):
    from jiuwenswarm.server.runtime.group_requests import dispatch_group_request
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    group = await make_group(service)
    server = SimpleNamespace(get_group_service=lambda: service)
    request = AgentRequest(
        request_id="request", req_method=ReqMethod.CHAT_SEND, user_id="owner", channel_id="web",
        params={"group_id": group["group_id"], "query": "Review", "mentions": ["advisor"]},
    )
    ws = SimpleNamespace(send=AsyncMock())
    assert not await dispatch_group_request(server, ws, request, asyncio.Lock())
    assert ge.GroupExecutions(group["group_id"]).read() == {}
    request.params["intent"] = "task"
    assert not await dispatch_group_request(server, ws, request, asyncio.Lock())
    assert ge.GroupExecutions(group["group_id"]).read() == {}
    assert request.params["intent"] == "task"


@pytest.mark.asyncio
async def test_organization_milestones_are_system_cards_and_not_group_messages(service):
    group = await make_group(service)
    params = {"root_task_id": "root-one", "phase": "source_accepted"}
    await ge.project_organization_progress(group["group_id"], group["team_name"], params)
    records = list(ge.GroupExecutions(group["group_id"]).read().values())
    assert len(records) == 1 and records[0]["execution_kind"] == "organization"
    assert records[0]["member_name"] is None and records[0]["progress"] is None
    params["phase"] = "summary_completed"
    await ge.project_organization_progress(group["group_id"], group["team_name"], params)
    record = ge.GroupExecutions(group["group_id"]).get(records[0]["execution_id"])
    assert record["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_can_start_work_and_submit_only_final_artifacts(service):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskTool, GroupMessageTool

    group = await make_group(service)
    backend = SimpleNamespace(
        member_name="advisor",
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="result"))),
    )
    context = SimpleNamespace(session_id=group["group_id"])
    started = await GroupTaskTool(backend, context).invoke({"run_id": "one", "task": "Review"})
    assert started.success and started.data["status"] == "running"
    result = await GroupMessageTool(backend, context, "Post").invoke({
        "execution_id": started.data["execution_id"], "content": "Done", "attachments": [{"name": "report.md"}],
    })
    assert result.success and result.data["status"] == "completed"
    backend.append_group_message.assert_awaited_once()
    assert backend.append_group_message.await_args.kwargs["attachments"] == [{"name": "report.md"}]


@pytest.mark.asyncio
async def test_core_task_events_without_status_use_factual_event_type(service):
    group = await make_group(service, kind="agent_group")
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    await store.task_event(key, {"event_type": "team.task", "event": {
        "type": "team.task.created", "task_id": "one",
    }})
    await store.task_event(key, {"event_type": "team.task", "event": {
        "type": "team.task.completed", "task_id": "one",
    }})
    assert store.get(key)["progress"] == {"completed": 1, "total": 1}
    assert store.get(key)["status"] == "queued"


@pytest.mark.asyncio
async def test_group_stream_projects_only_persisted_public_messages(service, monkeypatch):
    from openjiuwen.agent_teams.schema.conversation import ConversationMessage
    from openjiuwen.agent_teams.group_chat import handler

    group = await make_group(service)
    row = SimpleNamespace(meta={})
    backend = SimpleNamespace(db=SimpleNamespace(message=SimpleNamespace(get_message=AsyncMock(return_value=row))))
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=backend))
    event = {"event_type": "team.message", "event": {"message_id": "one"}}
    monkeypatch.setattr(handler, "group_metadata", lambda item: item.meta)
    assert await ge.project_group_stream_event(group["group_id"], event) is None
    row.meta = {"type": "group_chat"}
    monkeypatch.setattr(handler, "conversation_message", lambda *args: ConversationMessage(
        message_id="one", client_message_id="client", team_name=group["team_name"],
        session_id=group["group_id"], sender="advisor", sender_name="Advisor", content="Public reply", timestamp=1,
    ))
    result = await ge.project_group_stream_event(group["group_id"], event)
    assert result["event_type"] == "group.message" and result["message"]["content"] == "Public reply"


@pytest.mark.asyncio
async def test_private_team_updates_one_card_and_repairs_final_publication(service, tmp_path, monkeypatch):
    from jiuwenswarm.agents.swarm import group_chat as gc, assembly

    group = await make_group(service, kind="agent_group")
    state = gs.read_group(group["group_id"])
    tool = next(
        item for item in state["compiled"]["advisor"]["template"]["tools"] if item["type"] == gc.RUN_TEAM
    )
    context = SimpleNamespace(
        session_id=group["group_id"], mode="team", project_dir=None,
        trusted_dirs=None, user_id="owner", channel_id="web",
    )
    backend = SimpleNamespace(
        member_name="advisor", team_name=group["team_name"], conversation_mode="group_chat",
        group_conversation=AsyncMock(return_value=SimpleNamespace(history_path=tmp_path / "history.jsonl")),
        append_group_message=AsyncMock(side_effect=[
            OSError("offline"), SimpleNamespace(message=SimpleNamespace(message_id="final")),
        ]),
    )
    proxy = gc.RunTeam(backend, context, tool["params"]["child_spec"])
    monkeypatch.setattr(assembly, "enrich_team_spec_for_swarm", lambda *args, **kwargs: None)
    monkeypatch.setattr(gc.Runner, "pause_agent_team", AsyncMock())
    calls = []
    stopped = asyncio.Event()
    from openjiuwen.agent_teams.monitor.models import MonitorEvent, MonitorEventType

    async def monitor_events():
        yield MonitorEvent(
            event_type=MonitorEventType.TASK_COMPLETED, team_name="private-team", task_id="internal", timestamp=1,
        )
        await stopped.wait()

    monitor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(side_effect=stopped.set), events=monitor_events)
    monkeypatch.setattr(gc.Runner, "get_agent_team_monitor", AsyncMock(return_value=monitor))

    async def wait_for_progress():
        while store.get(key)["progress"] is None:
            await asyncio.sleep(0)

    async def stream(child, inputs, *, session):
        calls.append(child.team_name)
        yield SimpleNamespace(payload={"event_type": "team.runtime_ready"})
        await asyncio.wait_for(wait_for_progress(), timeout=2)
        submit = next(item for item in child.agents["leader"].tools if item.type == gc.COMPLETE_RUN)
        await gc.CompleteTeamRun(submit.params["key"]).invoke({
            "result": "Final report", "attachments": [{"name": "report.md"}], "execution_id": key,
        })
        await asyncio.Event().wait()

    monkeypatch.setattr(gc.Runner, "run_agent_team_streaming", stream)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "assignment"))[0]["execution_id"]
    from jiuwenswarm.agents.harness.team import get_team_manager

    accepted = await proxy.invoke({"run_id": key, "task": "Review"})
    assert accepted.success and accepted.data["accepted"]
    worker = get_team_manager("web").get_stream_task(store.get(accepted.data["execution_id"])["team_session_id"])
    await asyncio.wait_for(asyncio.shield(worker), 2)
    assert store.get(key)["phase"] == "publishing"
    retry = await proxy.invoke({"run_id": key, "task": "Review"})
    assert retry.success
    worker = get_team_manager("web").get_stream_task(store.get(retry.data["execution_id"])["team_session_id"])
    await asyncio.wait_for(asyncio.shield(worker), 2)
    assert store.get(key)["status"] == "completed" and len(calls) == 1
    assert (await proxy.invoke({"run_id": key, "task": "Review"})).success
    assert len(store.read()) == 1 and len(calls) == 1
    assert backend.append_group_message.await_count == 2
    assert store.get(key)["progress"] == {"completed": 1, "total": 1}
    monitor.start.assert_awaited_once()
    monitor.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_result_submission_publishes_once(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    backend = SimpleNamespace(
        member_name="advisor",
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="one"))),
    )
    results = await asyncio.gather(
        store.finish(key, backend, "Done", []), store.finish(key, backend, "Done", []),
    )
    assert results[0] == results[1]
    backend.append_group_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_created_organization_root_starts_a_card_via_existing_core_tool(service, monkeypatch):
    from jiuwenswarm.agents.swarm import group_execution as policy
    from openjiuwen.core.foundation.tool import ToolOutput

    group = await make_group(service)
    backend = SimpleNamespace(team_name=group["team_name"], org_task_manager=SimpleNamespace(organization_id="org"))
    monkeypatch.setattr(policy, "get_team_backend", lambda context: backend)
    rail = policy.GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name="host"))
    context = SimpleNamespace(inputs=SimpleNamespace(
        tool_name="org_create_task", tool_result=ToolOutput(success=True, data={
            "task_id": "root", "parent_task_id": None, "title": "Joint review",
        }),
    ))
    await rail.after_tool_call(context)
    records = list(ge.GroupExecutions(group["group_id"]).read().values())
    assert len(records) == 1
    assert records[0]["organization_id"] == "org" and records[0]["title"] == "Joint review"
    assert records[0]["status"] == "queued"
    context.inputs.tool_result = ToolOutput(success=False, error="rejected")
    await rail.after_tool_call(context)
    assert len(ge.GroupExecutions(group["group_id"]).read()) == 1


@pytest.mark.asyncio
async def test_task_assignment_rejects_runtime_without_execution_policy(service):
    group = await make_group(service)
    service.backend_resolver.return_value = SimpleNamespace(group_chat_spec=SimpleNamespace(agents={}))
    with pytest.raises(gs.GroupError, match="cold recovery"):
        await service.prepare_task(
            group["group_id"], {"mentions": ["advisor"], "query": "Review"},
            user_id="owner", channel_id="web", request_id="one",
        )
    assert ge.GroupExecutions(group["group_id"]).read() == {}


@pytest.mark.asyncio
async def test_organization_failure_is_terminal_and_late_queue_does_not_regress(service):
    group = await make_group(service)
    await ge.project_organization_progress(group["group_id"], group["team_name"], {
        "root_task_id": "root", "phase": "executing",
    })
    await ge.project_organization_progress(group["group_id"], group["team_name"], {
        "root_task_id": "root", "phase": "queued",
    })
    store = ge.GroupExecutions(group["group_id"])
    record = next(iter(store.read().values()))
    assert record["status"] == "running"
    await ge.project_organization_progress(group["group_id"], group["team_name"], {
        "root_task_id": "root", "phase": "failed",
    })
    assert store.get(record["execution_id"])["status"] == "failed"
    await ge.project_organization_progress(group["group_id"], group["team_name"], {
        "root_task_id": "root", "phase": "source_accepted",
    })
    assert store.get(record["execution_id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_dead_owner_is_interrupted_and_member_can_take_new_work(service, monkeypatch):
    group = await make_group(service)
    gid = group["group_id"]
    store = ge.GroupExecutions(gid)
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.update(record["execution_id"], status="running")
    monkeypatch.setattr(gs.lifecycle, "runtime_owner_alive", lambda owner: False)
    snapshot = await service.execution_get(
        gid, {"execution_id": record["execution_id"]}, user_id="owner", channel_id="web",
    )
    assert snapshot["status"] == "interrupted" and snapshot["error_code"] == "EXECUTION_OWNER_LOST"
    assert store.get(record["execution_id"])["status"] == "interrupted"
    monkeypatch.setattr(gs.lifecycle, "runtime_owner_alive", lambda owner: True)
    assert (await store.create(["advisor"], "New work", "two"))[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_recovery_preserves_live_or_unverifiable_owners(service, monkeypatch):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.reconcile()
    assert store.get(record["execution_id"])["status"] == "queued"

    def denied(owner):
        raise gs.lifecycle.LifecycleError("OPERATION_IN_PROGRESS", "cannot inspect process")

    monkeypatch.setattr(gs.lifecycle, "runtime_owner_alive", denied)
    await store.reconcile()
    assert store.get(record["execution_id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_accepted_delivery_is_not_failed_when_observer_disconnects(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.accept_delivery("one")
    await store.fail_pending([record["execution_id"]], "delivery_failed")
    assert store.get(record["execution_id"])["status"] == "queued"
    assert store.get(record["execution_id"])["delivery_status"] == "accepted"


@pytest.mark.asyncio
async def test_failed_delivery_releases_only_the_undelivered_assignment(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.fail_pending([record["execution_id"]], "delivery_failed")
    assert store.active("advisor") is None
    assert store.get(record["execution_id"])["error_code"] == "TASK_NOT_DELIVERED"
    assert (await store.create(["advisor"], "New work", "two"))[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_task_creation_and_failure_cleanup_run_inside_admitted_team_stream(service, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    group = await make_group(service)
    gid = group["group_id"]
    # Exercise the real wrapper and shared store, replacing only the actual model runtime.
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    manager = SimpleNamespace(
        get_startup_lock=lambda session_id: asyncio.Lock(), begin_request=Mock(), end_request=Mock(),
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    request = AgentRequest(
        request_id="one", session_id=gid, req_method=ReqMethod.CHAT_SEND, channel_id="web", user_id="owner",
        params={"intent": "task", "mentions": ["advisor"], "query": "Review", "type": "group_chat"},
    )

    async def denied(*args, **kwargs):
        assert ge.GroupExecutions(gid).active("advisor")["status"] == "queued"
        yield AgentResponseChunk(request_id="one", channel_id="web", payload={"event_type": "chat.error"})

    monkeypatch.setattr(team_helpers, "_process_team_message_stream", denied)
    chunks = [chunk async for chunk in team_helpers.process_team_message_stream(request, {}, object())]
    assert chunks[0].payload["event_type"] == "chat.error"
    assert ge.GroupExecutions(gid).active("advisor") is None
    manager.end_request.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_does_not_end_an_assignment_owned_by_the_background_stream(service, monkeypatch):
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    group = await make_group(service)
    gid = group["group_id"]
    monkeypatch.setattr(gs, "resolve_group_backend", AsyncMock(return_value=None))
    manager = SimpleNamespace(
        get_startup_lock=lambda session_id: asyncio.Lock(), begin_request=Mock(), end_request=Mock(),
        has_stream_task=lambda session_id, **kwargs: True,
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    request = AgentRequest(
        request_id="one", session_id=gid, req_method=ReqMethod.CHAT_SEND, channel_id="web", user_id="owner",
        params={"intent": "task", "mentions": ["advisor"], "query": "Review", "type": "group_chat"},
    )

    async def running(*args, **kwargs):
        yield AgentResponseChunk(request_id="one", channel_id="web", payload={"event_type": "team.runtime_ready"})
        await asyncio.Event().wait()

    monkeypatch.setattr(team_helpers, "_process_team_message_stream", running)
    stream = team_helpers.process_team_message_stream(request, {}, object())
    await anext(stream)
    await stream.aclose()
    assert ge.GroupExecutions(gid).active("advisor")["status"] == "queued"
    manager.end_request.assert_called_once()


def test_runtime_owner_identity_detects_pid_reuse():
    owner = gs.lifecycle.current_runtime_owner()
    assert gs.lifecycle.runtime_owner_alive(owner)
    assert not gs.lifecycle.runtime_owner_alive({**owner, "started_at": owner["started_at"] - 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
async def test_native_member_terminal_state_finishes_the_execution_card(service, status):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.update(record["execution_id"], status="running")
    await ge.project_group_stream_event(group["group_id"], {
        "event_type": "team.member", "event": {
            "type": "team.member.execution_changed", "member_id": "advisor", "new_status": status,
        },
    })
    assert store.get(record["execution_id"])["status"] == status
    assert store.active("advisor") is None


@pytest.mark.asyncio
async def test_fatal_runtime_error_releases_accepted_but_unstarted_work(service):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.accept_delivery("one")
    await ge.project_group_stream_event(group["group_id"], {"event_type": "team.error"})
    assert store.get(record["execution_id"])["status"] == "failed"
    assert store.active("advisor") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "timed_out", "failed"])
@pytest.mark.parametrize("before_update", [True, False])
async def test_finish_preserves_concurrent_terminal_state(service, monkeypatch, status, before_update):
    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    key = (await store.create(["advisor"], "Review", "one"))[0]["execution_id"]
    original_update = store.update

    async def concurrent_terminal(execution_id, **changes):
        if before_update:
            await original_update(execution_id, status=status, phase=status)
        result = await original_update(execution_id, **changes)
        if not before_update:
            await original_update(execution_id, status=status, phase=status)
        return result

    monkeypatch.setattr(store, "update", concurrent_terminal)
    backend = SimpleNamespace(member_name="advisor", append_group_message=AsyncMock())
    with pytest.raises(gs.GroupError, match="no longer active"):
        await store.finish(key, backend, "Late result", [])
    assert store.get(key)["status"] == status
    backend.append_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_completion_is_idempotent_and_correlates_result(service):
    from jiuwenswarm.agents.swarm.group_execution import GroupMessageTool
    from jiuwenswarm.server.runtime.group_reply import correlate_public_reply

    group = await make_group(service)
    group_id = group["group_id"]
    store = ge.GroupExecutions(group_id)
    record = (await store.create(["advisor"], "Review", "message-final"))[0]
    backend = SimpleNamespace(
        member_name="advisor", conversation_mode="group_chat",
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="final"))),
    )
    tool = GroupMessageTool(backend, SimpleNamespace(session_id=group_id), "Post")
    inputs = {"execution_id": record["execution_id"], "content": "Done", "attachments": [{"path": "report.md"}]}
    assert (await tool.invoke(inputs)).success
    assert (await tool.invoke(inputs)).success
    assert not (await tool.invoke({**inputs, "content": "Different"})).success
    backend.append_group_message.assert_awaited_once()
    posted = backend.append_group_message.await_args.kwargs
    correlation = correlate_public_reply(group_id, {"sender": "advisor", **posted})
    assert correlation["execution_id"] == record["execution_id"]
    assert store.get(record["execution_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_group_message_plain_chat_uses_core_path_without_finishing_task(service):
    from jiuwenswarm.agents.swarm.group_execution import GroupMessageTool

    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "plain", defer_card=True))[0]
    posted = Mock()
    posted.model_dump.return_value = {"message": "Hello"}
    backend = SimpleNamespace(
        member_name="advisor", conversation_mode="group_chat", append_group_message=AsyncMock(return_value=posted),
    )
    tool = GroupMessageTool(backend, SimpleNamespace(session_id=group["group_id"]), "Post")
    output = await tool.invoke({"content": "Hello", "client_message_id": "chat-1"})
    assert output.success
    assert store.get(record["execution_id"])["status"] == "queued"
    assert not (await tool.invoke({"content": "Missing id"})).success


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_member", "background", "bad_attachments", "mentions", "empty_id"])
async def test_group_message_rejects_invalid_completion(service, case):
    from jiuwenswarm.agents.swarm.group_execution import GroupMessageTool

    group = await make_group(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", case))[0]
    backend = SimpleNamespace(member_name="other" if case == "other_member" else "advisor")
    backend.append_group_message = AsyncMock()
    inputs = {"content": "Done", "execution_id": record["execution_id"]}
    if case == "background":
        await store.update(record["execution_id"], background=True)
    if case == "bad_attachments":
        inputs["attachments"] = ["report.md"]
    if case == "mentions":
        inputs["mentions"] = ["advisor"]
    if case == "empty_id":
        inputs["execution_id"] = ""
    tool = GroupMessageTool(backend, SimpleNamespace(session_id=group["group_id"]), "Post")
    assert not (await tool.invoke(inputs)).success
    backend.append_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_policy_replaces_core_message_schema_and_allows_explicit_final(service, monkeypatch):
    import json
    from openjiuwen.core.single_agent.ability_manager import AbilityManager
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.agent_teams.group_chat.tools import GroupSendMessageTool
    from jiuwenswarm.agents.swarm import group_execution as policies

    group = await make_group(service)
    group_id = group["group_id"]
    record = (await ge.GroupExecutions(group_id).create(["advisor"], "Review", "schema"))[0]
    backend = SimpleNamespace(member_name="advisor", conversation_mode="group_chat")
    monkeypatch.setattr(policies, "get_team_backend", lambda _: backend)
    abilities = AbilityManager(owner_id="group-schema-test")
    original = GroupSendMessageTool(backend, lambda _: "Post")
    abilities.add_ability(original.card, original)
    rail = policies.GroupTaskPolicyRail(SimpleNamespace(session_id=group_id, member_name="advisor"))
    ctx = SimpleNamespace(
        agent=SimpleNamespace(ability_manager=abilities, system_prompt_builder=Mock()), extra={},
        inputs=SimpleNamespace(tool_name="group_send_message", tool_call=ToolCall(
            id="finish", type="function", name="group_send_message", arguments=json.dumps({"execution_id": record["execution_id"]}),
        )),
    )
    try:
        await rail.before_invoke(ctx)
        await rail.before_invoke(ctx)
        cards = [card for card in abilities.list() if card.name == "group_send_message"]
        assert len(cards) == 1
        assert "execution_id" in cards[0].input_params["properties"]
        assert "attachments" in cards[0].input_params["properties"]
        await rail.before_tool_call(ctx)
        assert not ctx.extra
        ctx.inputs.tool_call.arguments = "{}"
        await rail.before_tool_call(ctx)
        assert ctx.extra["_skip_tool"]
    finally:
        abilities.remove_ability("group_send_message")


@pytest.mark.asyncio
@pytest.mark.parametrize("member_name", ["advisor", "host"])
async def test_group_policy_requires_archived_history_on_every_query(service, member_name):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail

    group = await make_group(service)
    builder = Mock()
    ctx = SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder))
    rail = GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name=member_name))
    for _ in range(2):
        await rail.before_invoke(ctx)
        content = builder.add_section.call_args.args[0].content
        assert content["en"] == content["cn"]
        assert "first use read_file to read the archived history.jsonl" in content["en"]
        assert "at the file path supplied in that query" in content["en"]
        assert "even to greetings and brief follow-ups" in content["en"]
        assert "continue reading until the archived history is complete" in content["en"]
        assert '"active_execution_id"' not in content["en"]
    assert builder.remove_section.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent_template", "agent_group"])
async def test_group_policy_scopes_member_identity_for_saved_and_dynamic_members(service, kind):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail

    group = await make_group(service, kind=kind)
    builder = Mock()
    ctx = SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder))
    rail = GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name="advisor"))
    for _ in range(2):
        await rail.before_invoke(ctx)
        content = builder.add_section.call_args.args[0].content["en"]
        assert "independent member, not the group leader" in content
        assert "peers, not your subordinates" in content
        assert ("consult it through run_team" in content) == (kind == "agent_group")
        assert ("Do not use start_group_task or implement deliverables yourself" in content) == (kind == "agent_group")


@pytest.mark.asyncio
async def test_group_host_keeps_its_coordinator_identity(service):
    from jiuwenswarm.agents.swarm.group_execution import GroupTaskPolicyRail

    group = await make_group(service)
    builder = Mock()
    ctx = SimpleNamespace(agent=SimpleNamespace(system_prompt_builder=builder))
    rail = GroupTaskPolicyRail(SimpleNamespace(session_id=group["group_id"], member_name="host"))
    await rail.before_invoke(ctx)
    content = builder.add_section.call_args.args[0].content["en"]
    assert "Coordinate public conversation" in content
    assert "not the group leader" not in content
