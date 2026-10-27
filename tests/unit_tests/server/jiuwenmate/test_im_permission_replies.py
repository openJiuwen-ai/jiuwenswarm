"""IM approvals bypass the blocked business queue and resume the exact request."""

from dataclasses import replace
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.jiuwenmate import execution_driver
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_dingtalk_production_wiring import (
    NOW,
    _event,
    _group_requests,
    _requests,
    host,
)
from tests.unit_tests.server.jiuwenmate.test_session_scheduler import settle
from tests.unit_tests.server.jiuwenmate.test_permission_approvals import paused_request


async def pause(host, monkeypatch, *, call_count=1, next_call_count=None, group=False):
    notices, resumed = [], []
    host.manager._thinking_notice_sender = AsyncMock(side_effect=lambda message: notices.append(message))
    original = host.manager._scheduler._driver._execute

    async def execute(spec, context, inputs):
        if inputs.text != "needs permission":
            async for event in original(spec, context, inputs):
                yield event
            return
        event = core.AgentEvent.create(
            context.execution, event_type=core.EventType.EXECUTION_PAUSED, local_seq=1,
            occurred_at=NOW, payload={"reason": "awaiting_confirmation", "intercepted": [
                {"tool_call_id": f"call-{i}", "tool_name": "bash", "arguments": "pwd"}
                for i in range(1, (next_call_count if resumed and next_call_count is not None else call_count) + 1)
            ]},
        )
        await context.ports.commit.record(context.execution, event)
        yield event

    async def resume(spec, context, reply):
        resumed.append((context.execution.request_id, reply, spec))
        event = core.AgentEvent.create(
            context.execution, event_type=core.EventType.EXECUTION_FINAL, local_seq=1,
            occurred_at=NOW, payload={"status": "completed", "output_refs": ()},
        )
        await context.ports.commit.record(context.execution, event)
        yield event

    monkeypatch.setattr(host.manager._scheduler._driver, "_execute", execute)
    monkeypatch.setattr(execution_driver.core, "resume_persistent_request", resume)
    event = _event(conversation_type="2", receiver_bot_mentioned=True) if group else _event()
    await host.inbound.handle(replace(event, text="needs permission"))
    rows = _group_requests if group else _requests
    await settle(lambda: rows(host)[0].outcome == "paused" and not host.manager.scheduler.running)
    repository = PermissionApprovalRepository(host.db)
    approval, = repository.pending("local")
    await settle(lambda: bool(notices))
    return approval, notices, resumed


@pytest.mark.parametrize("text,choice", [("批准", "approve"), ("拒绝", "reject")])
async def test_dingtalk_bare_decision_resumes_without_another_request(host, monkeypatch, text, choice):
    approval, notices, resumed = await pause(host, monkeypatch)
    await host.inbound.handle(replace(_event(), message_id="decision-1", text=text))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    assert len(_requests(host)) == 1
    assert len(resumed) == 1 and resumed[0][0] == approval.request_id
    assert resumed[0][1]["responses"][0]["choice"] == choice
    assert any("已批准" in item["text"] or "已拒绝" in item["text"] for item in notices)
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "consumed"


@pytest.mark.parametrize("text", ["批准 {id} <工具调用ID>", "批准了，你再试试", "批准 {id} bad-call"])
async def test_invalid_approval_gets_feedback_without_queueing(host, monkeypatch, text):
    approval, notices, resumed = await pause(host, monkeypatch)
    await host.inbound.handle(replace(_event(), message_id="invalid-1", text=text.format(id=approval.approval_id)))
    assert len(_requests(host)) == 1 and not resumed
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"
    assert "call-1" in notices[-1]["text"]


async def test_waiting_business_message_is_queued_with_feedback_then_runs(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    await host.inbound.handle(replace(_event(), message_id="next-task", text="another task"))
    assert len(_requests(host)) == 2 and _requests(host)[1].status == "queued"
    assert "等待确认" in notices[-1]["text"]
    await host.inbound.handle(replace(_event(), message_id="decision-1", text="批准"))
    await settle(lambda: all(row.outcome == "completed" for row in _requests(host)))
    assert len(_requests(host)) == 2 and len(resumed) == 1


async def test_duplicate_bare_approval_does_not_approve_a_later_interruption(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    decision = replace(_event(), message_id="decision-1", text="批准")
    await host.inbound.handle(decision)
    await settle(lambda: _requests(host)[0].outcome == "completed")
    await host.inbound.handle(replace(_event(), message_id="second-task", text="needs permission"))
    await settle(lambda: len(_requests(host)) == 2 and _requests(host)[1].outcome == "paused")
    later, = PermissionApprovalRepository(host.db).pending("local")
    await host.inbound.handle(decision)
    assert len(_requests(host)) == 2 and len(resumed) == 1
    assert PermissionApprovalRepository(host.db).get(later.approval_id).status == "pending"
    assert "已批准" in notices[-1]["text"]


@pytest.mark.parametrize("changes", [
    {"sender_staff_id": "other-user"},
    {"receiving_app_id": "other-app"},
    {"channel": "feishu"},
    {"agent_id": "other-agent"},
    {"namespace_id": "other-namespace"},
    {"target_id": "other-conversation"},
])
async def test_explicit_approval_cannot_cross_identity_or_route(host, monkeypatch, changes):
    from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot

    approval, notices, resumed = await pause(host, monkeypatch)
    sender = changes.get("sender_staff_id", "staff-test")
    app = changes.get("receiving_app_id", "app-dingtalk")
    await host.manager.handle_im_permission_reply(
        f"批准 {approval.approval_id} call-1",
        namespace_id=changes.get("namespace_id", "local"), agent_id=changes.get("agent_id", "agent-dingtalk"),
        channel=changes.get("channel", "dingtalk"), app_id=app,
        target_id=changes.get("target_id", sender), source_message_id="foreign-1",
        access=AccessSnapshot(principal_ref=f"dingtalk:{app}:{sender}", startup_scope=frozenset({"chat"}),
                              source_scope=frozenset({"chat"}), policy_revision="test"),
    )
    assert not resumed
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"
    assert "call-1" not in notices[-1]["text"]


async def test_expired_approval_gets_rejected_and_unblocks_queue(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    monkeypatch.setattr(host.manager, "_clock", lambda: NOW + timedelta(minutes=16))
    await host.inbound.handle(replace(_event(), message_id="late-1", text=f"批准 {approval.approval_id} call-1"))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    assert resumed[0][1]["responses"][0]["choice"] == "reject"
    assert "过期" in notices[-1]["text"]


async def test_multi_call_approval_requires_selection_and_notice_has_real_commands(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch, call_count=2)
    assert "<工具调用ID>" not in notices[0]["text"]
    assert f"批准 {approval.approval_id} call-1" in notices[0]["text"]
    assert "其余" in notices[0]["text"]
    await host.inbound.handle(replace(_event(), message_id="ambiguous-1", text="批准"))
    assert not resumed and len(_requests(host)) == 1
    await host.inbound.handle(replace(_event(), message_id="select-1", text=f"批准 {approval.approval_id} call-2"))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    assert [(item["tool_call_id"], item["choice"]) for item in resumed[0][1]["responses"]] == [
        ("call-1", "reject"), ("call-2", "approve"),
    ]


async def test_published_permission_change_does_not_auto_approve_old_request(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    original = host.service.snapshot
    monkeypatch.setattr(host.service, "current_snapshot", lambda ns, aid: original(ns, aid, "v2"))
    await host.inbound.handle(replace(_event(), message_id="new-config-task", text="another task"))
    rows = _requests(host)
    assert rows[1].record.config_version == "v2" and rows[1].status == "queued"
    assert not resumed and "等待确认" in notices[-1]["text"]
    await host.inbound.handle(replace(_event(), message_id="decision-1", text="批准"))
    await settle(lambda: all(row.outcome == "completed" for row in _requests(host)))
    assert resumed[0][2].card.id == "agent-dingtalk"
    assert _requests(host)[0].record.config_version == "v1"


@pytest.mark.parametrize("text", ["批准", "批准 {id} call-1"])
async def test_feishu_uses_the_same_authenticated_reply_handler(tmp_path, text):
    from jiuwenswarm.server.jiuwenmate.permission_replies import IMPermissionReplies

    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = paused_request(channel="feishu", app_id="app-feishu")
    event = event.model_copy(update={"payload": event.payload.model_copy(update={
        "intercepted": event.payload.intercepted[:1],
    })})
    approval = repository.create(state, event, now=NOW)
    notices = []

    async def decide(approval_id, *, access, **kwargs):
        return repository.decide(approval_id, principal_ref=access.principal_ref, now=NOW, **kwargs)

    handler = IMPermissionReplies(repository, decide=decide,
                                  send=AsyncMock(side_effect=lambda message: notices.append(message)))
    assert await handler.handle(
        text.format(id=approval.approval_id), namespace_id=state.record.namespace_id,
        agent_id=state.record.agent_id, access=state.record.access, channel="feishu",
        target_id="conversation-1", app_id="app-feishu", source_message_id="feishu-approval-1",
    )
    assert repository.get(approval.approval_id).decision == "approve"
    assert notices[-1]["channel"] == "feishu" and "已批准" in notices[-1]["text"]
    restored = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    assert restored.reply_approval(
        namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
        principal_ref=state.record.access.principal_ref, channel="feishu", app_id="app-feishu",
        target_id="conversation-1", message_id="feishu-approval-1",
    ) == approval.approval_id


async def test_concurrent_decisions_resume_once(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    await asyncio.gather(*(
        host.inbound.handle(replace(_event(), message_id=f"decision-{i}",
                                   text=f"批准 {approval.approval_id} call-1"))
        for i in range(2)
    ))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    assert len(resumed) == 1 and len(_requests(host)) == 1


async def test_feedback_transport_failure_does_not_queue_or_repeat_approval(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    host.manager._thinking_notice_sender = AsyncMock(side_effect=RuntimeError("transport unavailable"))
    await host.inbound.handle(replace(_event(), message_id="decision-1", text="批准"))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    assert len(resumed) == 1 and len(_requests(host)) == 1


async def test_ordinary_agreement_without_an_approval_remains_a_business_message(tmp_path):
    from jiuwenswarm.server.jiuwenmate.permission_replies import IMPermissionReplies

    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, _ = paused_request(channel="feishu", app_id="app-feishu")
    decide, send = AsyncMock(), AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=send)
    assert not await handler.handle(
        "同意", namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
        access=state.record.access, channel="feishu", target_id="conversation-1",
        app_id="app-feishu", source_message_id="ordinary-agreement",
    )
    decide.assert_not_awaited()
    send.assert_not_awaited()


async def test_other_group_user_gets_waiting_feedback_without_approval_details(tmp_path):
    from jiuwenswarm.server.jiuwenmate.permission_replies import IMPermissionReplies

    repository = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = paused_request(channel="feishu", app_id="app-feishu")
    approval = repository.create(state, event, now=NOW)
    decide, send = AsyncMock(), AsyncMock()
    handler = IMPermissionReplies(repository, decide=decide, send=send)
    assert not await handler.handle(
        "帮我看看其他问题", namespace_id=state.record.namespace_id, agent_id=state.record.agent_id,
        access=replace(state.record.access, principal_ref="other-user"), channel="feishu",
        target_id="conversation-1", app_id="app-feishu", source_message_id="another-user-task",
    )
    decide.assert_not_awaited()
    message = send.await_args.args[0]["text"]
    assert "等待" in message and "确认" in message
    assert approval.approval_id not in message and "call-1" not in message


async def test_unmatched_bare_approval_cannot_authorize_a_later_task_after_reopening_store(host, monkeypatch):
    orphan = replace(_event(), message_id="orphan-decision", text="批准")
    await host.inbound.handle(orphan)
    approval, notices, resumed = await pause(host, monkeypatch)
    host.manager._permission_approvals = PermissionApprovalRepository(host.db)
    await host.inbound.handle(orphan)
    assert not resumed and len(_requests(host)) == 1
    assert PermissionApprovalRepository(host.db).get(approval.approval_id).status == "pending"


async def test_ambiguous_bare_approval_cannot_authorize_a_later_single_call(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch, call_count=2, next_call_count=1)
    ambiguous = replace(_event(), message_id="ambiguous-decision", text="批准")
    await host.inbound.handle(ambiguous)
    await host.inbound.handle(replace(_event(), message_id="select-1", text=f"批准 {approval.approval_id} call-1"))
    await settle(lambda: _requests(host)[0].outcome == "completed")
    await host.inbound.handle(replace(_event(), message_id="second-task", text="needs permission"))
    await settle(lambda: len(_requests(host)) == 2 and _requests(host)[1].outcome == "paused")
    later, = PermissionApprovalRepository(host.db).pending("local")
    host.manager._permission_approvals = PermissionApprovalRepository(host.db)
    await host.inbound.handle(ambiguous)
    assert len(resumed) == 1
    assert PermissionApprovalRepository(host.db).get(later.approval_id).status == "pending"


async def test_agreement_with_a_new_task_is_queued_while_approval_waits(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    await host.inbound.handle(replace(_event(), message_id="business-agreement", text="同意这个方案，请再整理会议纪要"))
    assert len(_requests(host)) == 2 and _requests(host)[1].status == "queued"
    assert not resumed and "等待" in notices[-1]["text"]


async def test_bare_agreement_is_not_treated_as_approval_while_waiting(host, monkeypatch):
    approval, notices, resumed = await pause(host, monkeypatch)
    await host.inbound.handle(replace(_event(), message_id="ordinary-yes", text="同意"))
    assert len(_requests(host)) == 2 and _requests(host)[1].status == "queued"
    assert not resumed


async def test_invalid_approval_id_without_pending_approval_gets_feedback(host):
    notices = []
    host.manager._thinking_notice_sender = AsyncMock(side_effect=lambda message: notices.append(message))
    await host.inbound.handle(replace(_event(), message_id="bad-id-message", text="批准 bad-id"))
    assert not _requests(host)
    assert notices and "审批指令" in notices[-1]["text"]


async def test_dingtalk_group_approval_preserves_group_delivery_and_bypasses_reply_policy(host, monkeypatch):
    from types import SimpleNamespace

    approval, notices, resumed = await pause(host, monkeypatch, group=True)
    assert notices[0]["is_direct_message"] is False
    assert notices[0]["chat_id"] == "conversation-test"
    host.service.read_settings = lambda ns, aid: SimpleNamespace(group_reply_mode="passive")
    event = replace(_event(conversation_type="2", receiver_bot_mentioned=False),
                    message_id="group-approval", text="批准")
    await host.inbound.handle(event)
    await settle(lambda: _group_requests(host)[0].outcome == "completed")
    assert len(_group_requests(host)) == 1 and len(resumed) == 1
    assert notices[-1]["is_direct_message"] is False
    assert notices[-1]["chat_id"] == "conversation-test"
