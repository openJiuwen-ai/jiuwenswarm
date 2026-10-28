# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Owner result receipts survive restarts and require ordered, fenced acknowledgement."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams.organization.schema import OrgTask
from openjiuwen.agent_teams.runtime.dispatch import RunActionKind
from openjiuwen.agent_teams.runtime.pool import RuntimeState
from openjiuwen.core.runner.runner import GLOBAL_RUNNER
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.team.expert_org.inbox_tool import OrgAckOwnerResultTool
from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
from jiuwenswarm.agents.harness.team.rails.organization_owner_inbox_rail import OrganizationOwnerInboxRail
from jiuwenswarm.server.runtime import organization_owner_inbox as inbox_module
from jiuwenswarm.server.runtime import organization_owner_results as results
from jiuwenswarm.server.runtime.organization_owner_inbox import OwnerInbox
from jiuwenswarm.server.runtime.organization_execution import stopping_teams
from jiuwenswarm.server.runtime.session import session_metadata


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_module, "get_agent_root_dir", lambda: tmp_path)
    metadata = {"team_name": "owner", "organization_owner_stopped": False}
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: metadata)
    pool = SimpleNamespace(list_tasks=AsyncMock(return_value=[]), get_task_review=AsyncMock(return_value=None))
    messages = SimpleNamespace(list_leader_messages=AsyncMock(return_value=[]))
    manager = SimpleNamespace(organization_id="org", task_pool=pool, message_service=messages)
    backend = SimpleNamespace(org_task_manager=manager)
    runtime = SimpleNamespace(pool=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        agent=SimpleNamespace(team_backend=backend),
    ))))
    monkeypatch.setattr(GLOBAL_RUNNER, "_team_runtime_manager", runtime, raising=False)
    store = inbox_module.owner_inbox()
    store.enable("session", "owner", 0.0)
    return SimpleNamespace(store=store, metadata=metadata, manager=manager, backend=backend, runtime=runtime)


def task(task_id, team="child", updated_at=1000):
    return OrgTask(
        task_id=task_id, root_task_id="root", parent_task_id="root",
        created_by={"creator_id": "leader", "organization_id": "org", "team_id": "owner"},
        created_at=0, updated_at=updated_at, title=task_id, description="scope", status="COMPLETED",
        assignment={"team_id": team}, output_context={"description": "full result " + task_id},
    )


def test_fifo_acknowledgements_and_duplicate_producer_calls_survive_reopen(environment):
    store = environment.store
    first = store.append("session", "owner", {"result": "one"})
    second = store.append("session", "owner", {"result": "two"})
    assert store.append("session", "owner", {"result": "one"}) == first
    delivery = store.begin_delivery("session", "owner")
    assert store.next_result("session", "owner", delivery)["message_id"] == first
    assert store.next_result("session", "owner", delivery) is None
    with pytest.raises(ValueError):
        store.acknowledge("session", "owner", delivery, second)
    assert store.acknowledge("session", "owner", delivery, first)
    assert not store.acknowledge("session", "owner", delivery, first)
    restored = OwnerInbox(store.path)
    next_delivery = restored.begin_delivery("session", "owner")
    assert restored.next_result("session", "owner", next_delivery)["message_id"] == second
    assert restored.acknowledge("session", "owner", next_delivery, second)
    assert restored.next_result("session", "owner", next_delivery) is None


@pytest.mark.asyncio
async def test_unknown_or_undelivered_receipts_are_not_duplicate_successes(environment):
    tool = OrgAckOwnerResultTool("owner", "session")
    unknown = await tool.invoke({"message_id": "missing", "delivery_id": "missing"})
    assert not unknown.success
    message = environment.store.append("session", "owner", {"result": "one"})
    stale = await tool.invoke({"message_id": message, "delivery_id": "missing"})
    assert not stale.success
    assert environment.store.get_result("session", "owner", message)["acknowledged_at"] is None


def test_failed_stop_rollback_preserves_receipts_for_later_recovery(environment):
    store = environment.store
    message = store.append("session", "owner", {"result": "captured during stop"})
    delivery = store.begin_delivery("session", "owner")
    store.next_result("session", "owner", delivery)
    store.disable("session", "owner")
    assert store.enabled_at("session", "owner") is None
    with pytest.raises(ValueError, match="Stale"):
        store.acknowledge("session", "owner", delivery, message)
    store.enable("session", "owner")
    assert store.next_result("session", "owner", store.begin_delivery("session", "owner"))["message_id"] == message


@pytest.mark.asyncio
async def test_root_relay_with_text_input_can_use_an_existing_inbox_receipt(environment, monkeypatch):
    monkeypatch.setattr(results, "queue_owner_notification", AsyncMock(return_value={"acknowledged_at": None}))
    entry = SimpleNamespace(current_session_id="session", agent=SimpleNamespace(spec=SimpleNamespace(metadata={})))
    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=AsyncMock(return_value=entry)), run_organization_turn=AsyncMock(return_value=True),
    )
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    assert await launcher.run_organization_turn(
        "owner", "session", "queued notification", source="org_root_background",
    )
    assert "ordered Owner result inbox" in runtime.run_organization_turn.call_args.kwargs["inputs"]["query"]


@pytest.mark.asyncio
async def test_context_injection_cancellation_releases_receipt_and_propagates(environment):
    import asyncio

    store = environment.store
    message = store.append("session", "owner", {"result": "one"})
    rail = OrganizationOwnerInboxRail("owner", "session")
    context = SimpleNamespace(add_messages=AsyncMock(side_effect=asyncio.CancelledError()))
    ctx = AgentCallbackContext(agent=SimpleNamespace(), context=context, session=SimpleNamespace())
    with pytest.raises(asyncio.CancelledError):
        await rail.before_model_call(ctx)
    context.add_messages.side_effect = None
    await rail.before_model_call(ctx)
    assert json.loads(context.add_messages.call_args.args[0][1].content)["message_id"] == message


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capture", "cleanup"])
async def test_receipt_storage_errors_preserve_turn_terminal_and_history(environment, monkeypatch, failure):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_packages.team_package_availability", lambda *args: {},
    )
    team = "owner" if failure == "cleanup" else "child"

    async def stream(inputs, session):
        environment.metadata["organization_owner_stopped"] = True
        yield SimpleNamespace(payload={"event_type": "team.completed"})

    agent = SimpleNamespace(
        spec=SimpleNamespace(name=team, metadata={"expert_team": True, "channel_id": "web"}),
        team_backend=environment.backend, stream=stream,
    )
    entry = SimpleNamespace(agent=agent, current_session_id="session", state=RuntimeState.PAUSED)
    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=AsyncMock(return_value=entry)), finalize=AsyncMock(),
        activate=AsyncMock(return_value=SimpleNamespace(
            agent=agent, action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
            session=SimpleNamespace(post_run=AsyncMock()),
        )),
    )
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    launcher.push_expert_payload = AsyncMock()
    launcher._flush_expert_history = AsyncMock()
    inputs = {"query": "complete"}
    if failure == "capture":
        monkeypatch.setattr(results, "capture_child_completion", AsyncMock(side_effect=OSError("storage failed")))
    else:
        inputs["_org_relay_source"] = "org_root_background"
        monkeypatch.setattr(
            results, "queue_owner_notification", AsyncMock(side_effect=[None, OSError("storage failed")]),
        )
    assert await launcher.run_organization_turn(team, "session", inputs)
    launcher._flush_expert_history.assert_awaited_once()
    events = [call.args[0]["event_type"] for call in launcher.push_expert_payload.call_args_list]
    assert "org.turn_completed" in events
    assert events[-1] == "chat.processing_status"


def test_new_invocation_fences_old_ack_and_cannot_cross_owner_or_session(environment):
    store = environment.store
    message = store.append("session", "owner", {"result": "one"})
    old = store.begin_delivery("session", "owner")
    store.next_result("session", "owner", old)
    new = store.begin_delivery("session", "owner")
    assert store.next_result("session", "owner", old) is None
    with pytest.raises(ValueError, match="Stale"):
        store.acknowledge("session", "owner", old, message)
    assert store.next_result("session", "owner", new)["message_id"] == message
    for session, owner in [("other", "owner"), ("session", "child")]:
        with pytest.raises(ValueError, match="Unknown"):
            store.acknowledge(session, owner, new, message)
    assert store.acknowledge("session", "owner", new, message)


def test_simultaneous_duplicate_writers_create_only_one_receipt(environment):
    store = environment.store
    with ThreadPoolExecutor(max_workers=4) as workers:
        ids = list(workers.map(lambda _: store.append("session", "owner", {"result": "same"}), range(12)))
    assert len(set(ids)) == 1
    delivery = store.begin_delivery("session", "owner")
    assert store.next_result("session", "owner", delivery)["message_id"] == ids[0]
    store.acknowledge("session", "owner", delivery, ids[0])
    assert store.next_result("session", "owner", delivery) is None


def test_process_restart_reclaims_unacknowledged_receipt(environment):
    store = environment.store
    receipt = store.append("session", "owner", {"result": "survives restart"})
    delivery = store.begin_delivery("session", "owner")
    store.next_result("session", "owner", delivery)
    script = (
        "import json,sys; from pathlib import Path; "
        "from jiuwenswarm.server.runtime.organization_owner_inbox import OwnerInbox; "
        "s=OwnerInbox(Path(sys.argv[1])); d=s.begin_delivery('session','owner'); "
        "r=s.next_result('session','owner',d); print(json.dumps(r)); "
        "s.acknowledge('session','owner',d,r['message_id'])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(store.path)], capture_output=True, text=True, check=True,
    )
    assert json.loads(completed.stdout)["message_id"] == receipt
    assert store.next_result("session", "owner", store.begin_delivery("session", "owner")) is None


@pytest.mark.asyncio
async def test_saved_task_results_backfill_and_repeated_recovery_do_not_duplicate(environment):
    environment.manager.task_pool.list_tasks.return_value = [task("later", updated_at=2000), task("first")]
    await results.capture_task_results("session", environment.backend)
    await results.capture_task_results("session", environment.backend)
    store = environment.store
    delivery = store.begin_delivery("session", "owner")
    for task_id in ["first", "later"]:
        record = store.next_result("session", "owner", delivery)
        snapshot = record["payload"]["task"]
        assert snapshot["task_id"] == task_id
        assert snapshot["assigned_team_id"] == "child"
        assert snapshot["output_context"]["description"] == "full result " + task_id
        store.acknowledge("session", "owner", delivery, record["message_id"])
    assert store.next_result("session", "owner", delivery) is None


@pytest.mark.asyncio
async def test_child_completion_keeps_full_text_and_excludes_owner_tasks(environment):
    environment.manager.task_pool.list_tasks.return_value = [task("owner-task", team="owner"), task("child-task")]
    await results.capture_child_completion("session", "child", environment.backend, "exact leader output")
    store = environment.store
    token = store.begin_delivery("session", "owner")
    first = store.next_result("session", "owner", token)
    assert first["payload"]["task"]["task_id"] == "child-task"
    store.acknowledge("session", "owner", token, first["message_id"])
    second = store.next_result("session", "owner", token)
    assert second["payload"]["content"] == "exact leader output"


@pytest.mark.asyncio
async def test_native_message_and_its_wake_share_one_receipt(environment):
    environment.metadata["organization_owner_stopped"] = True
    queued = await results.queue_owner_notification("session", "owner", "org_root_background", {
        "query": "Leader message message-1 arrived in organization org from team child.",
    })
    environment.manager.message_service.list_leader_messages.return_value = [{
        "message_id": "message-1", "organization_id": "org", "from_team_id": "child", "to_team_id": "owner",
        "content": "full message", "metadata": {}, "created_at": 10,
    }]
    await results.capture_leader_messages("session", "owner", environment.manager)
    store = environment.store
    token = store.begin_delivery("session", "owner")
    record = store.next_result("session", "owner", token)
    assert record["message_id"] == queued["message_id"]
    assert record["payload"]["message"]["content"] == "full message"
    store.acknowledge("session", "owner", token, record["message_id"])
    await results.capture_leader_messages("session", "owner", environment.manager)
    assert store.next_result("session", "owner", token) is None


@pytest.mark.asyncio
async def test_model_receives_fifo_tool_results_and_next_item_only_after_ack(environment):
    store = environment.store
    ids = [store.append("session", "owner", {"result": value}) for value in ["first", "second"]]
    rail = OrganizationOwnerInboxRail("owner", "session")
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=SimpleNamespace(), context=context, session=SimpleNamespace())
    await rail.before_model_call(ctx)
    await rail.before_model_call(ctx)
    assert context.add_messages.await_count == 1
    pair = context.add_messages.call_args.args[0]
    data = json.loads(pair[1].content)
    assert pair[0].tool_calls[0].id == pair[1].tool_call_id
    assert data["message_id"] == ids[0]
    ack = OrgAckOwnerResultTool("owner", "session")
    acknowledged = await ack.invoke({"message_id": ids[0], "delivery_id": data["delivery_id"]})
    assert acknowledged.success
    await rail.before_model_call(ctx)
    next_data = json.loads(context.add_messages.call_args.args[0][1].content)
    assert next_data["message_id"] == ids[1]


@pytest.mark.asyncio
async def test_injection_failure_releases_only_its_own_receipt_for_retry(environment):
    store = environment.store
    message_id = store.append("session", "owner", {"result": "one"})
    rail = OrganizationOwnerInboxRail("owner", "session")
    context = SimpleNamespace(add_messages=AsyncMock(side_effect=RuntimeError("context unavailable")))
    ctx = AgentCallbackContext(agent=SimpleNamespace(), context=context, session=SimpleNamespace())
    with pytest.raises(RuntimeError, match="context unavailable"):
        await rail.before_model_call(ctx)
    context.add_messages.side_effect = None
    await rail.before_model_call(ctx)
    assert json.loads(context.add_messages.call_args.args[0][1].content)["message_id"] == message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("team,stopped", [("child", False), ("owner", True)])
async def test_stopped_owner_and_child_cannot_consume_or_ack(environment, team, stopped):
    environment.metadata["organization_owner_stopped"] = stopped
    message_id = environment.store.append("session", "owner", {"result": "one"})
    context = SimpleNamespace(add_messages=AsyncMock())
    await OrganizationOwnerInboxRail(team, "session").before_model_call(
        AgentCallbackContext(agent=SimpleNamespace(), context=context, session=SimpleNamespace()),
    )
    context.add_messages.assert_not_awaited()
    result = await OrgAckOwnerResultTool(team, "session").invoke({"message_id": message_id, "delivery_id": "fake"})
    assert not result.success
    assert environment.store.get_result("session", "owner", message_id)["acknowledged_at"] is None


@pytest.mark.asyncio
async def test_nested_subagent_without_main_session_cannot_consume_or_reset_owner_delivery(environment):
    environment.store.append("session", "owner", {"result": "one"})
    context = SimpleNamespace(add_messages=AsyncMock())
    ctx = AgentCallbackContext(agent=SimpleNamespace(), context=context)
    ctx.extra["organization_owner_inbox_delivery"] = "parent-delivery"
    rail = OrganizationOwnerInboxRail("owner", "session")
    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)
    assert ctx.extra["organization_owner_inbox_delivery"] == "parent-delivery"
    context.add_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_owner_notification_is_persisted_without_activation_and_not_run_after_ack(environment):
    environment.metadata["organization_owner_stopped"] = True
    launcher = JiuwenExpertTeamLauncher(runtime_manager=environment.runtime)
    inputs = {"_org_relay_source": "org_root_background", "query": "Review the completed child task task-1"}
    async with stopping_teams("session", {"owner"}):
        assert await launcher.run_organization_turn("owner", "session", inputs)
    environment.runtime.pool.get.assert_not_awaited()
    store = environment.store
    token = store.begin_delivery("session", "owner")
    record = store.next_result("session", "owner", token)
    assert record["payload"]["query"] == inputs["query"]
    store.acknowledge("session", "owner", token, record["message_id"])
    environment.metadata["organization_owner_stopped"] = False
    assert await launcher.run_organization_turn("owner", "session", inputs)
    environment.runtime.pool.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_leader_message_must_be_handled_before_owner_receipt_ack(environment):
    store = environment.store
    message_id = store.append("session", "owner", {
        "kind": "leader_message", "message": {"message_id": "native-1", "content": "result"},
    })
    token = store.begin_delivery("session", "owner")
    store.next_result("session", "owner", token)
    getter = AsyncMock(return_value={"handled_at": None})
    environment.manager.message_service.get_leader_message = getter
    tool = OrgAckOwnerResultTool("owner", "session")
    params = {"message_id": message_id, "delivery_id": token}
    assert not (await tool.invoke(params)).success
    assert store.get_result("session", "owner", message_id)["acknowledged_at"] is None
    getter.return_value = {"handled_at": 1000}
    assert (await tool.invoke(params)).success
    duplicate = await tool.invoke(params)
    assert duplicate.success
    assert duplicate.data["duplicate"] is True


@pytest.mark.asyncio
async def test_pending_task_review_cannot_be_acknowledged_as_processed(environment):
    await results.capture_child_completion("session", "child", environment.backend, "")
    environment.manager.task_pool.list_tasks.return_value = [task("review-required")]
    await results.capture_task_results("session", environment.backend)
    store = environment.store
    token = store.begin_delivery("session", "owner")
    record = store.next_result("session", "owner", token)
    environment.manager.task_pool.get_task_review.return_value = SimpleNamespace(review_status="PENDING")
    tool = OrgAckOwnerResultTool("owner", "session")
    params = {"message_id": record["message_id"], "delivery_id": token}
    assert not (await tool.invoke(params)).success
    assert store.get_result("session", "owner", record["message_id"])["acknowledged_at"] is None
    environment.manager.task_pool.get_task_review.return_value = SimpleNamespace(review_status="ACCEPTED")
    assert (await tool.invoke(params)).success


@pytest.mark.asyncio
async def test_recovered_native_messages_remain_ordered_across_all_pages(environment):
    source = [{
        "message_id": f"message-{index:03}", "created_at": index, "content": str(index),
        "organization_id": "org", "from_team_id": "child", "to_team_id": "owner",
    } for index in range(103)]
    environment.manager.message_service.list_leader_messages.side_effect = [
        list(reversed(source[3:])), list(reversed(source[:3])),
    ]
    await results.capture_leader_messages("session", "owner", environment.manager)
    store = environment.store
    token = store.begin_delivery("session", "owner")
    for expected in source:
        record = store.next_result("session", "owner", token)
        assert record["payload"]["message"]["message_id"] == expected["message_id"]
        store.acknowledge("session", "owner", token, record["message_id"])
    assert store.next_result("session", "owner", token) is None


@pytest.mark.asyncio
async def test_no_saved_task_results_are_silently_cut_off_by_default_page_size(environment):
    tasks = [task(str(index), updated_at=1000 + index) for index in range(105)]
    environment.manager.task_pool.list_tasks.side_effect = [tasks[:100], tasks]
    await results.capture_task_results("session", environment.backend)
    store = environment.store
    token = store.begin_delivery("session", "owner")
    for index in range(105):
        record = store.next_result("session", "owner", token)
        assert record["payload"]["task"]["task_id"] == str(index)
        store.acknowledge("session", "owner", token, record["message_id"])
    assert store.next_result("session", "owner", token) is None


@pytest.mark.asyncio
async def test_child_launcher_saves_completion_before_returning_success_to_scheduler(environment, monkeypatch):
    environment.metadata["organization_owner_stopped"] = True
    environment.manager.task_pool.list_tasks.return_value = [task("completed-child-task")]
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.organization_packages.team_package_availability", lambda *args: {},
    )

    async def stream(inputs, session):
        yield SimpleNamespace(payload={"event_type": "team.completed"})

    agent = SimpleNamespace(
        spec=SimpleNamespace(name="child", metadata={"expert_team": True, "channel_id": "web"}),
        team_backend=environment.backend, stream=stream,
    )
    entry = SimpleNamespace(agent=agent, current_session_id="session", state=RuntimeState.PAUSED)
    runtime = SimpleNamespace(
        pool=SimpleNamespace(get=AsyncMock(return_value=entry)), finalize=AsyncMock(),
        activate=AsyncMock(return_value=SimpleNamespace(
            agent=agent, action=SimpleNamespace(kind=RunActionKind.RESUME_FROM_PAUSE),
            session=SimpleNamespace(post_run=AsyncMock()),
        )),
    )
    launcher = JiuwenExpertTeamLauncher(runtime_manager=runtime)
    launcher.push_expert_payload = AsyncMock()
    launcher._flush_expert_history = AsyncMock()
    assert await launcher.run_organization_turn("child", "session", {"query": "complete task"})
    runtime.activate.assert_awaited_once()
    store = environment.store
    record = store.next_result("session", "owner", store.begin_delivery("session", "owner"))
    assert record["payload"]["task"]["task_id"] == "completed-child-task"
    assert record["payload"]["task"]["output_context"]["description"] == "full result completed-child-task"
