"""Approval recipients differ from executing Agents without inheriting their permissions."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot, PermissionOrigin, RequestRouteSnapshot,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from jiuwenswarm.server.jiuwenmate.permission_replies import IMPermissionReplies
from jiuwenswarm.server.jiuwenmate.web_adapter import JiuwenMateWebAdapter
from tests.unit_tests.server.jiuwenmate.test_permission_approvals import NOW, paused_request


def handoff_state(*, origin=True):
    state, event = paused_request(channel="feishu", principal="feishu:bot:code-bot", target="oc-group", app_id="app-code")
    source = core.GroupMessage(
        message_id="om-handoff", sender_id="architect", sender_name="Architect", sender_kind="agent",
        received_at=NOW, text="please implement")
    record = replace(state.record, namespace_id="local", request_input=core.RequestInput(
        kind="group_message", text=source.text, source=source, reply_target="oc-group"),
        permission_origin=PermissionOrigin(principal_ref="feishu:user:human-in-app-a", agent_id="architect",
            request_id="root-request", route=RequestRouteSnapshot(channel="feishu", app_id="app-architect",
                target_id="oc-group", source_message_id="om-human")) if origin else None)
    return replace(state, record=record), event


def test_delegated_approval_uses_original_app_and_human_but_targets_executing_request(tmp_path):
    repo = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = handoff_state()
    approval = repo.create(state, event, now=NOW)
    assert approval.agent_id == "employee-1" and approval.approver_agent_id == "architect"
    assert approval.principal_ref == "feishu:user:human-in-app-a"
    assert approval.route.app_id == "app-architect"
    found = repo.for_route("local", "architect", principal_ref=approval.principal_ref,
                          channel="feishu", app_id="app-architect", target_id="oc-group")
    assert found == [approval]
    with pytest.raises(PersistentAgentError):
        repo.decide(approval.approval_id, decision="approve", principal_ref=approval.principal_ref,
            channel="feishu", app_id="app-architect", target_id="oc-group", tool_call_id="call-1",
            approver_agent_id="employee-1", now=NOW)
    decided = repo.decide(approval.approval_id, decision="approve", principal_ref=approval.principal_ref,
        channel="feishu", app_id="app-architect", target_id="oc-group", tool_call_id="call-1",
        approver_agent_id="architect", now=NOW)
    assert decided.request_id == state.record.request_id
    assert decided.reply["responses"][0]["choice"] == "approve"


def test_unattributed_bot_task_fails_closed_without_an_approval_route(tmp_path):
    repo = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = handoff_state(origin=False)
    approval = repo.create(state, event, now=NOW)
    assert approval.route is None
    assert approval.approver_agent_id == state.record.agent_id
    rejected = repo.reject_unrouted(approval.approval_id)
    assert rejected.status == "rejected"
    assert rejected.reply["responses"][0]["choice"] == "reject"
    restored = PermissionApprovalRepository(repo.db_path).get(approval.approval_id)
    assert restored == rejected


@pytest.mark.asyncio
async def test_web_permission_rpc_passes_selected_agent_for_route_validation():
    captured = {}

    class Manager:
        async def decide_permission_approval(self, approval_id, **kwargs):
            captured.update(approval_id=approval_id, **kwargs)
            return SimpleNamespace(to_dict=lambda: {"request_id": "request-a"})

    adapter = JiuwenMateWebAdapter()
    adapter._manager = Manager()
    request = AgentRequest(
        request_id="web-decision", channel_id="web", req_method=ReqMethod.JIUWENMATE_PERMISSION_DECIDE,
        params={"agent_id": "config-code", "approval_id": "approval-a", "decision": "reject",
                "conversation_id": "web-conversation"},
    )
    response = await adapter.handle(request)
    assert response.ok
    assert captured["approver_agent_id"] == "config-code"


@pytest.mark.asyncio
async def test_original_agent_accepts_the_human_decision_for_the_executing_agent(tmp_path):
    repo = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    state, event = handoff_state()
    approval = repo.create(state, event, now=NOW)
    decide, send = AsyncMock(), AsyncMock()
    handler = IMPermissionReplies(repo, decide=decide, send=send)
    access = AccessSnapshot(principal_ref=approval.principal_ref, startup_scope=frozenset({"chat"}),
                            source_scope=frozenset({"chat"}), policy_revision="test")
    await handler.handle(f"批准 {approval.approval_id} call-1", namespace_id="local", agent_id="architect",
        access=access, channel="feishu", app_id="app-architect", target_id="oc-group", source_message_id="om-decision")
    assert decide.await_args.kwargs["approver_agent_id"] == "architect"
    assert decide.await_args.args == (approval.approval_id,)


@pytest.mark.asyncio
async def test_permission_feedback_has_a_stable_platform_uuid(tmp_path):
    repo = PermissionApprovalRepository(tmp_path / "agents.sqlite3")
    send = AsyncMock()
    handler = IMPermissionReplies(repo, decide=AsyncMock(), send=send)
    access = AccessSnapshot(principal_ref="feishu:user:human", startup_scope=frozenset(),
                            source_scope=frozenset(), policy_revision="test")
    for _ in range(2):
        await handler.handle("批准 000000000000 call-1", namespace_id="local", agent_id="agent", access=access,
            channel="feishu", app_id="app", target_id="oc-group", source_message_id="om-reply")
    keys = [item.args[0]["idempotency_key"] for item in send.await_args_list]
    assert keys[0] == keys[1] and str(UUID(keys[0])) == keys[0]


