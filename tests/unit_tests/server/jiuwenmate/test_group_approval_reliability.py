"""Regression coverage for authenticated group controls and stable access checks."""

from dataclasses import replace
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.jiuwenmate.failure_notices import failure_notice_text
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from jiuwenswarm.server.jiuwenmate.permission_replies import IMPermissionReplies
from tests.unit_tests.server.jiuwenmate.test_permission_approvals import NOW, paused_request
from tests.unit_tests.server.jiuwenmate.test_group_chat_port import append, records
from tests.unit_tests.server.jiuwenmate.test_dingtalk_production_wiring import host, _event, _group_requests
from tests.unit_tests.server.jiuwenmate.test_im_permission_replies import pause
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import settle


@pytest.mark.parametrize("text", [
    "@小九 批准", "@小九 批准 {id}", "@小九 {id}", "@小九 {id} call-1",
    "{id}", "{id} call-1", "批准 call-1", "call-1",
])
async def test_permission_references_resume_the_exact_owned_operation(tmp_path, text):
    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = paused_request(channel="feishu", app_id="app-feishu")
    event = event.model_copy(update={"payload": event.payload.model_copy(update={
        "intercepted": event.payload.intercepted[:1],
    })})
    approval = repository.create(state, event, now=NOW)

    async def decide(approval_id, *, access, **kwargs):
        return repository.decide(approval_id, principal_ref=access.principal_ref, now=NOW, **kwargs)

    send = AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=send)
    assert await handler.handle(
        text.format(id=approval.approval_id), namespace_id=state.record.namespace_id,
        agent_id=state.record.agent_id, access=state.record.access, channel="feishu",
        target_id="conversation-1", app_id="app-feishu", source_message_id="decision-1",
        is_direct_message=False,
        receiver_bot_mentioned=text.startswith("@"),
    )
    assert repository.get(approval.approval_id).decision == "approve"
    assert "已批准" in send.await_args.args[0]["text"]


async def test_an_unaddressed_bot_without_a_pending_operation_stays_quiet(tmp_path):
    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, _ = paused_request(channel="feishu", app_id="app-feishu")
    decide, send = AsyncMock(), AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=send)
    assert await handler.handle(
        "批准", namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
        access=state.record.access, channel="feishu", target_id="conversation-1",
        app_id="app-feishu", source_message_id="unaddressed", is_direct_message=False,
    )
    decide.assert_not_awaited()
    send.assert_not_awaited()


async def test_normal_message_observation_does_not_revoke_a_group_read(records):
    from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort

    groups, group = records.groups, records.group
    groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=NOW,
                          message_time=NOW, source_ref="initial")
    before = groups.access_for("local", "agent", group.group_chat_id)
    append(records, 1)
    messages = records.archive.current_messages("local", group.group_chat_id, ("m1",))
    groups.observe_access("local", "agent", group.group_chat_id, "app",
                          observed_at=NOW + timedelta(seconds=2), message_time=NOW + timedelta(seconds=2),
                          source_ref="ordinary-message")
    after = groups.access_for("local", "agent", group.group_chat_id)
    assert before != after and before.visible_from == after.visible_from
    port = HostGroupChatPort(groups, records.archive, None, namespace_id="local", agent_id="agent")
    port._access = AsyncMock(return_value=(after, group))
    execution = SimpleNamespace(namespace_id="local", agent_id="agent")
    checked = await port._checked_messages(execution, before, group, messages)
    assert checked[0].message_id == "m1"


@pytest.mark.parametrize("changed", ["app_id", "visible_from"])
async def test_real_authorization_changes_still_refuse_the_read(records, changed):
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
    from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort

    groups, group = records.groups, records.group
    groups.observe_access("local", "agent", group.group_chat_id, "app", observed_at=NOW,
                          source_ref="initial")
    before = groups.access_for("local", "agent", group.group_chat_id)
    value = "other-app" if changed == "app_id" else NOW + timedelta(seconds=2)
    after = replace(before, **{changed: value})
    append(records, 1)
    messages = records.archive.current_messages("local", group.group_chat_id, ("m1",))
    port = HostGroupChatPort(groups, records.archive, None, namespace_id="local", agent_id="agent")
    port._access = AsyncMock(return_value=(after, group))
    with pytest.raises(PersistentAgentError):
        await port._checked_messages(SimpleNamespace(namespace_id="local", agent_id="agent"),
                                     before, group, messages)


def test_permission_termination_notice_is_specific_and_does_not_disclose_details():
    text = failure_notice_text("req-1", reason="group_access_lost", detail="private path and credentials")
    assert "群访问权限" in text and "终止" in text and "req-1" in text
    assert "内部异常" not in text and "private" not in text


def test_effective_execution_fingerprint_is_durable_and_immutable(tmp_path):
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
    from tests.unit_tests.server.jiuwenmate.test_execution_failure_notices import start

    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = start(repository)
    repository.bind_runtime_fingerprint(execution.namespace_id, execution.request_id,
                                        execution.execution_id, "a" * 64)
    reopened = ExecutionRepository(repository.db_path)
    assert reopened.runtime_fingerprint(execution.namespace_id, execution.request_id,
                                       execution.execution_id) == "a" * 64
    with pytest.raises(PersistentAgentError):
        reopened.bind_runtime_fingerprint(execution.namespace_id, execution.request_id,
                                           execution.execution_id, "b" * 64)
    assert reopened.runtime_fingerprint(execution.namespace_id, "other-request",
                                       execution.execution_id) is None


@pytest.mark.parametrize("text", ["@小九 批准", "@小九 批准 {id}", "@小九 {id}", "{id}", "批准 {id} call-1", "call-1"])
async def test_dingtalk_group_controls_use_the_live_entry_before_passive_policy(host, monkeypatch, text):
    approval, notices, resumed = await pause(host, monkeypatch, group=True)
    host.service.read_settings = lambda ns, aid: SimpleNamespace(group_reply_mode="passive")
    event = replace(_event(conversation_type="2"), message_id="control", text=text.format(id=approval.approval_id),
                    receiver_bot_mentioned=text.startswith("@"), mention_ids=("bot",) if text.startswith("@") else ())
    await host.inbound.handle(event)
    await settle(lambda: _group_requests(host)[0].outcome == "completed")
    assert len(_group_requests(host)) == 1 and len(resumed) == 1
    assert resumed[0][0] == approval.request_id
    assert notices[-1]["is_direct_message"] is False


async def test_a_group_control_older_than_the_access_boundary_cannot_resume(host, monkeypatch):
    approval, _, resumed = await pause(host, monkeypatch, group=True)
    event = replace(_event(conversation_type="2", receiver_bot_mentioned=True), message_id="stale-control",
                    text=f"@小九 批准 {approval.approval_id}", received_at=NOW - timedelta(days=3))
    await host.inbound.handle(event)
    assert not resumed and len(_group_requests(host)) == 1
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"


async def test_other_bot_mentions_cannot_approve_the_receiving_bot(host, monkeypatch):
    approval, _, resumed = await pause(host, monkeypatch, group=True)
    event = replace(_event(conversation_type="2"), message_id="other-mention",
                    text=f"@other 批准 {approval.approval_id}", mention_ids=("other-bot",),
                    receiver_bot_mentioned=False)
    await host.inbound.handle(event)
    assert not resumed and PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"


@pytest.mark.parametrize("text", ["批准", "call-1"])
async def test_unaddressed_shortcuts_do_not_choose_between_bots(tmp_path, text):
    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = paused_request(channel="feishu", app_id="app-feishu")
    event = event.model_copy(update={"payload": event.payload.model_copy(update={"intercepted": event.payload.intercepted[:1]})})
    first = repository.create(state, event, now=NOW)
    other_record = replace(state.record, agent_id="other-agent", request_id="other-request", session_id="other-session")
    other_event = event.model_copy(update={"agent_id": "other-agent", "request_id": "other-request",
                                           "session_id": "other-session", "execution_id": "other-execution"})
    repository.create(replace(state, record=other_record), other_event, now=NOW)
    decide, send = AsyncMock(), AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=send)
    await handler.handle(text, namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
                         access=state.record.access, channel="feishu", target_id="conversation-1",
                         app_id="app-feishu", source_message_id="ambiguous", is_direct_message=False)
    decide.assert_not_awaited()
    assert "完整命令" in send.await_args.args[0]["text"]
    assert repository.get(first.approval_id).status == "pending"


@pytest.mark.parametrize("text", ["@小九 批准", "@小九 批准 {id}", "@小九 {id}", "{id}"])
async def test_feishu_group_controls_resume_through_the_real_fanout(tmp_path, monkeypatch, text):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from tests.unit_tests.gateway.test_persistent_group_fanout import _Host, _binding, _event, NOW as FEISHU_NOW, NS

    host = _Host(tmp_path)
    agent_id = host.connected("小九", "app-dev")
    settings = host.service.read_settings(NS, host.config_of(agent_id))
    host.service.update_settings(NS, host.config_of(agent_id), replace(settings, group_reply_mode="passive"))
    resumed = []

    async def execute(spec, context, inputs):
        event = core.AgentEvent.create(context.execution, event_type=core.EventType.EXECUTION_PAUSED,
            local_seq=1, occurred_at=FEISHU_NOW, payload={"reason": "awaiting_confirmation", "intercepted": [
                {"tool_call_id": "call-1", "tool_name": "bash", "arguments": "pwd"},
            ]})
        await context.ports.commit.record(context.execution, event)
        yield event

    async def resume(spec, context, reply):
        resumed.append(reply)
        event = core.AgentEvent.create(context.execution, event_type=core.EventType.EXECUTION_FINAL,
                                      local_seq=1, occurred_at=FEISHU_NOW,
                                      payload={"status": "completed", "output_refs": ()})
        await context.ports.commit.record(context.execution, event)
        yield event

    monkeypatch.setattr(host.manager._scheduler._driver, "_execute", execute)
    monkeypatch.setattr(execution_driver.core, "resume_persistent_request", resume)
    host.manager._thinking_notice_sender = AsyncMock()
    fanout = host.fanout([_binding("app-dev", agent_id)])
    original = _event(text="needs permission", receiver_bot_mentioned=True)
    await host.start()
    try:
        await fanout.handle(original)
        await settle(lambda: host.request_states(agent_id, original)[0].outcome == "paused")
        approval, = PermissionApprovalRepository(host.db).pending(NS)
        await fanout.handle(replace(original, message_id="decision", text=text.format(id=approval.approval_id),
                                    receiver_bot_mentioned=text.startswith("@"),
                                    mention_ids=("bot",) if text.startswith("@") else ()))
        await settle(lambda: host.request_states(agent_id, original)[0].outcome == "completed")
        assert len(host.request_states(agent_id, original)) == 1
        assert len(resumed) == 1 and resumed[0]["responses"][0]["choice"] == "approve"
    finally:
        await host.close()


async def test_cleanup_prefers_original_paused_runtime_and_consumes_only_after_core_checks(tmp_path, monkeypatch):
    from openjiuwen.harness import persistent_agent as core
    from jiuwenswarm.server.jiuwenmate.permission_cleanup import discard_permission_confirmation

    state, event = paused_request()
    approvals = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    approval = approvals.create(state, event, now=NOW)
    sessions = SimpleNamespace(runtime_fingerprint=Mock(return_value="b" * 64))
    windows = SimpleNamespace(bind=Mock(return_value=object()))
    consume = Mock()
    monkeypatch.setattr(approvals, "consume", consume)
    discard = AsyncMock(side_effect=ValueError("wrong saved identity"))
    monkeypatch.setattr(core, "discard_persistent_confirmation", discard)
    args = dict(sessions=sessions, windows=windows, sharing=None, approvals=approvals,
                record=state.record, approval=approval, runtime_fingerprint="c" * 64)
    with pytest.raises(ValueError):
        await discard_permission_confirmation(**args)
    consume.assert_not_called()
    assert discard.await_args.kwargs["spec_fingerprint"] == "b" * 64
    sessions.runtime_fingerprint.assert_called_once_with(state.record.namespace_id, state.record.request_id,
                                                        approval.execution_id)
    discard.side_effect = None
    await discard_permission_confirmation(**args)
    consume.assert_called_once_with(approval.approval_id)


@pytest.mark.parametrize("legacy", [False, True])
async def test_terminal_recovery_uses_the_original_runtime_instead_of_published_spec(host, monkeypatch, legacy):
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from jiuwenswarm.server.jiuwenmate.spec_provider import build_invocation

    monkeypatch.setattr(execution_driver, "build_invocation", build_invocation)
    approval, _, _ = await pause(host, monkeypatch, group=True)
    state = host.repository.get_request("local", approval.request_id)
    driver = host.manager._scheduler._driver
    expected = host.repository.runtime_fingerprint("local", approval.request_id, approval.execution_id)
    assert expected != state.record.config_fingerprint
    if legacy:
        with host.repository._transaction() as conn:
            conn.execute("DELETE FROM execution_runtime_specs")
    assert await driver.permission_cleanup_fingerprint(state.record, approval) == expected
    repository = PermissionApprovalRepository(host.db)
    decided = repository.decide(approval.approval_id, decision="approve", principal_ref=approval.principal_ref,
                               channel="dingtalk", target_id="conversation-test", app_id="app-dingtalk",
                               tool_call_id="call-1", now=NOW)
    # Model a restart after the request failed but before confirmation cleanup completed.
    with host.repository._transaction() as conn:
        conn.execute("UPDATE requests SET outcome='failed' WHERE request_id=?", (approval.request_id,))
    discard = AsyncMock(return_value=True)
    monkeypatch.setattr(execution_driver.core, "discard_persistent_confirmation", discard)
    await host.manager._resume_permission_approval(decided)
    assert discard.await_args.kwargs["spec_fingerprint"] == expected
    assert repository.get(approval.approval_id).status == "consumed"


@pytest.mark.parametrize("cleanup", ["cleared", "absent", "mismatch"])
async def test_terminal_recovery_checks_core_when_legacy_reconstruction_fails(host, monkeypatch, caplog, cleanup):
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    approval, _, _ = await pause(host, monkeypatch)
    repository = PermissionApprovalRepository(host.db)
    decided = repository.decide(approval.approval_id, decision="approve", principal_ref=approval.principal_ref,
                               channel=approval.route.channel, target_id=approval.route.target_id,
                               app_id=approval.route.app_id,
                               tool_call_id="call-1", now=NOW)
    with host.repository._transaction() as conn:
        conn.execute("UPDATE requests SET outcome='failed' WHERE request_id=?", (approval.request_id,))
        conn.execute("DELETE FROM execution_runtime_specs")
    record = host.repository.get_request("local", approval.request_id).record
    monkeypatch.setattr(host.manager._scheduler._driver, "_snapshots",
                        Mock(side_effect=ValueError("private diagnostic")))
    discard = AsyncMock(return_value=cleanup == "cleared")
    if cleanup == "mismatch":
        discard.side_effect = PersistentAgentError("waiting Spec mismatch", code="INVALID_CONFIG")
    monkeypatch.setattr(execution_driver.core, "discard_persistent_confirmation", discard)
    wake = AsyncMock()
    monkeypatch.setattr(host.manager._scheduler, "wake", wake)

    if cleanup == "mismatch":
        with pytest.raises(PersistentAgentError, match="waiting Spec mismatch"):
            await host.manager._resume_permission_approval(decided)
        assert repository.get(approval.approval_id).status == decided.status
        wake.assert_not_awaited()
    else:
        await host.manager._resume_permission_approval(decided)
        assert repository.get(approval.approval_id).status == "consumed"
        wake.assert_awaited_once_with("local", approval.session_id)
    assert discard.await_args.kwargs["spec_fingerprint"] == record.config_fingerprint
    assert "ValueError" in caplog.text and "private diagnostic" not in caplog.text


async def test_legacy_cleanup_reports_a_missing_original_execution(host, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    approval, _, _ = await pause(host, monkeypatch)
    record = host.repository.get_request("local", approval.request_id).record
    monkeypatch.setattr(host.repository, "runtime_fingerprint", Mock(return_value=None))
    monkeypatch.setattr(host.repository, "executions", Mock(return_value=[]))
    with pytest.raises(PersistentAgentError, match="approval execution is not part of this request") as error:
        await host.manager._scheduler._driver.permission_cleanup_fingerprint(record, approval)
    assert error.value.code == "NOT_FOUND"


async def test_core_cleanup_keeps_fingerprint_validation_and_preserves_completed_results(tmp_path):
    from openjiuwen.harness import persistent_agent as core
    from openjiuwen.core.common.exception.errors import BaseError
    from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
    from openjiuwen.core.single_agent.interrupt.state import ToolInterruptionState, ToolInterruptEntry
    from openjiuwen.harness.persistent_agent.schema.session_record import encode_messages, encode_interruption
    from openjiuwen.harness.persistent_agent.execution.session_state import session_key_prefix
    from jiuwenswarm.server.jiuwenmate.permission_cleanup import discard_permission_confirmation
    from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore

    state, event = paused_request()
    record = state.record
    approvals = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    approval = approvals.create(state, event, now=NOW)
    approval = approvals.decide(approval.approval_id, decision="approve", principal_ref=record.access.principal_ref,
                                channel="web", target_id="conversation-1", app_id=None, tool_call_id="call-1", now=NOW)
    calls = [ToolCall(id=call_id, type="function", name="bash", arguments='{"command":"pwd"}')
             for call_id in ("done", "call-1")]
    assistant = AssistantMessage(tool_calls=calls)
    scene = ToolInterruptionState(ai_message=assistant, iteration=1, original_query="task", interrupted_tools={
        "call-1": ToolInterruptEntry(tool_call=calls[1], interrupt_requests={"permission": InterruptRequest(message="approve?")}),
    })
    saved = dict(schema_version=1, agent_id=record.agent_id, session_id=record.session_id,
                 messages=encode_messages([UserMessage(content="task"), assistant,
                                           ToolMessage(tool_call_id="done", content="REAL RESULT")]),
                 interruption=encode_interruption(scene), auto_confirm={}, deepagent_state=None, checkpoint=None,
                 last_group_message_id=None, current_window_id=None, window_seq=None, last_rollover=None,
                 ending={"outcome": "awaiting_confirmation", "request_id": record.request_id,
                         "execution_id": approval.execution_id, "spec_fingerprint": "b" * 64})
    windows = WindowBackendStore(tmp_path / "agents.sqlite3")
    backend = windows.bind(record.namespace_id, record.agent_id, record.session_id)
    key = session_key_prefix(record.agent_id, record.session_id) + "state"
    original = json.dumps(saved)
    await backend.set(key, original)
    with pytest.raises(BaseError):
        await core.discard_persistent_confirmation(backend, agent_id=record.agent_id, session_id=record.session_id,
                                                  request_id=record.request_id, spec_fingerprint=record.config_fingerprint)
    assert await backend.get(key) == original
    with pytest.raises(BaseError):
        await discard_permission_confirmation(sessions=SimpleNamespace(runtime_fingerprint=Mock(return_value=None)),
                                              windows=windows, sharing=None, approvals=approvals, record=record,
                                              approval=approval, runtime_fingerprint=None)
    assert await backend.get(key) == original
    assert approvals.get(approval.approval_id).status == "approved"
    await discard_permission_confirmation(sessions=SimpleNamespace(runtime_fingerprint=Mock(return_value="b" * 64)),
                                          windows=windows, sharing=None, approvals=approvals, record=record,
                                          approval=approval)
    result = json.loads(await backend.get(key))
    assert result["interruption"] is None and result["ending"]["outcome"] == "error"
    assert any(item.get("tool_call_id") == "done" and item.get("content") == "REAL RESULT"
               for item in result["messages"])
    assert approvals.get(approval.approval_id).status == "consumed"


@pytest.mark.parametrize("change", ["left", "history_boundary"])
async def test_access_lost_during_approval_resume_has_a_durable_notice_even_when_delivery_fails(host, monkeypatch, change):
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from tests.unit_tests.server.jiuwenmate.test_dingtalk_production_wiring import NOW as HOST_NOW

    approval, _, resumed = await pause(host, monkeypatch, group=True)
    attached, release = asyncio.Event(), asyncio.Event()
    original = execution_driver.ExecutionHandle.attach

    async def attach(handle, context):
        await original(handle, context)
        attached.set()
        await release.wait()

    monkeypatch.setattr(execution_driver.ExecutionHandle, "attach", attach)
    sender = AsyncMock(return_value=None)
    host.deliveries._send_delivery = sender
    await host.inbound.handle(replace(_event(conversation_type="2"), message_id="approval", text="批准"))
    await asyncio.wait_for(attached.wait(), timeout=5)
    state = host.repository.get_request("local", approval.request_id)
    session = host.repository.get_session("local", state.record.session_id)
    groups = host.manager.group_chats
    try:
        groups.update_membership("local", state.record.agent_id, session.conversation.key, "app-dingtalk",
                                 status="left", observed_at=HOST_NOW + timedelta(seconds=1), source_ref="left")
        if change == "history_boundary":
            groups.observe_access("local", state.record.agent_id, session.conversation.key, "app-dingtalk",
                                  observed_at=HOST_NOW + timedelta(seconds=2), message_time=HOST_NOW + timedelta(seconds=2),
                                  source_ref="rejoin")
    finally:
        release.set()
    await settle(lambda: host.repository.get_request("local", approval.request_id).outcome == "failed")
    await settle(lambda: not host.manager.scheduler.running)
    assert not resumed
    view = await host.manager.request_view("local", approval.request_id)
    assert len(view.replies) == 1 and "终止" in view.replies[0]
    assert "内部异常" not in view.replies[0]
    assert ("群访问权限" if change == "left" else "历史范围") in view.replies[0]
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "consumed"
    await host.deliveries.dispatch_pending()
    assert sender.await_count >= 1
    assert len(host.repository.result_deliveries("local", approval.request_id)) == 1
    assert host.repository.result_deliveries("local", approval.request_id)[0]["status"] == "pending"


@pytest.mark.parametrize("text", ["a business request", "@小九 批准", "@小九 123456abcdef"])
async def test_feishu_stale_group_events_are_filtered_before_controls_or_admission(tmp_path, text):
    from tests.unit_tests.gateway.test_persistent_group_fanout import _Host, _binding, _event, NS, NOW as FEISHU_NOW

    host = _Host(tmp_path)
    agent_id = host.connected("小九", "app-dev")
    group = host.groups.resolve(NS, "feishu", "oc_project", now=FEISHU_NOW)
    host.groups.observe_access(NS, agent_id, group.group_chat_id, "app-dev",
                               observed_at=FEISHU_NOW + timedelta(minutes=1), source_ref="current-membership")
    host.manager.handle_im_permission_reply = AsyncMock(return_value=True)
    await host.start()
    try:
        fanout = host.fanout([_binding("app-dev", agent_id)])
        await fanout.handle(_event(text=text, receiver_bot_mentioned=True))
        assert host.row_counts() == (1, 0, 0)
        host.manager.handle_im_permission_reply.assert_not_awaited()
        assert host.groups.access_for(NS, agent_id, group.group_chat_id).visible_from == FEISHU_NOW + timedelta(minutes=1)
    finally:
        await host.close()


async def test_permission_resume_task_failure_is_observed_and_remains_retryable(host, monkeypatch, caplog):
    approval, _, _ = await pause(host, monkeypatch)
    monkeypatch.setattr(host.manager, "_resume_permission_approval", AsyncMock(side_effect=ValueError("private diagnostic")))
    host.manager._schedule_permission_resume(approval)
    await settle(lambda: approval.approval_id not in host.manager._permission_resume_tasks)
    assert "permission resume will retry" in caplog.text
    assert "ValueError" in caplog.text and "private diagnostic" not in caplog.text
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"


@pytest.mark.parametrize("text", ["@小九 批准", "@小九 {id}", "{id}", "call-1"])
async def test_im_reference_shortcuts_cannot_grant_owner_only_group_sharing(host, monkeypatch, text):
    approval, notices, resumed = await pause(host, monkeypatch, group=True)
    monkeypatch.setattr(host.manager.sharing_repository, "for_native", lambda approval_id: {})
    await host.inbound.handle(replace(_event(conversation_type="2"), message_id="sharing-control",
                                     text=text.format(id=approval.approval_id), receiver_bot_mentioned=text.startswith("@")))
    assert not resumed
    assert "所有者" in notices[-1]["text"] and "WorkSwarm" in notices[-1]["text"]
    assert len(_group_requests(host)) == 1
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"


@pytest.mark.parametrize("field,value", [
    ("agent_id", "other-agent"), ("app_id", "other-app"), ("namespace_id", "other-namespace"),
    ("channel", "dingtalk"), ("target_id", "other-conversation"), ("principal_ref", "other-user"),
])
async def test_id_only_reply_retains_all_identity_boundaries(tmp_path, field, value):
    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = paused_request(channel="feishu", app_id="app-feishu")
    approval = repository.create(state, event, now=NOW)
    args = dict(namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
                app_id="app-feishu", channel="feishu", target_id="conversation-1", principal_ref=approval.principal_ref)
    args[field] = value
    principal = args.pop("principal_ref")
    decide = AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=AsyncMock())
    await handler.handle(approval.approval_id, access=replace(state.record.access, principal_ref=principal),
                         source_message_id="foreign-reference", is_direct_message=False, **args)
    decide.assert_not_awaited()
    assert repository.get(approval.approval_id).status == "pending"
