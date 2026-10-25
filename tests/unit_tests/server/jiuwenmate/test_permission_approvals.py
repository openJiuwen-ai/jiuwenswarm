"""JM-26: durable, exact-call permission approval state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jiuwenswarm.server.jiuwenmate.execution_models import (
    AccessSnapshot,
    RequestRecord,
    RequestRouteSnapshot,
    RequestState,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from openjiuwen.harness import persistent_agent as core

NOW = datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)


def paused_request(*, channel="web", principal="user-1", target="conversation-1", app_id=None):
    route = RequestRouteSnapshot(
        channel=channel,
        target_id=target,
        app_id=app_id,
        source_message_id="message-1" if channel == "feishu" else None,
    )
    record = RequestRecord(
        namespace_id="team-1",
        agent_id="employee-1",
        session_id="session-1",
        request_id="request-1",
        source_ref="source-1",
        config_version="v1",
        config_fingerprint="a" * 64,
        access=AccessSnapshot(
            principal_ref=principal,
            startup_scope=frozenset({"chat"}),
            source_scope=frozenset({"chat"}),
            policy_revision="policy-1",
        ),
        request_input=core.RequestInput(
            kind="direct_message",
            text="整理文件",
            source=core.DirectMessageSource(
                message_id="message-1", sender_id=principal, sender_name="User", received_at=NOW
            ),
            reply_target="conversation-1",
        ),
        received_at=NOW,
        route_snapshot=route,
    )
    execution = core.ExecutionContext(
        namespace_id=record.namespace_id,
        agent_id=record.agent_id,
        session_id=record.session_id,
        request_id=record.request_id,
        execution_id="execution-1",
        source_ref=record.source_ref,
        config_version=record.config_version,
        config_fingerprint=record.config_fingerprint,
        principal_ref=principal,
        startup_scope=frozenset({"chat"}),
        source_scope=frozenset({"chat"}),
        policy_revision="policy-1",
        capability_snapshot=frozenset({"workspace_read"}),
    )
    event = core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=1,
        occurred_at=NOW,
        payload={
            "reason": "awaiting_confirmation",
            "intercepted": [
                {"tool_call_id": "call-1", "tool_name": "read_file", "arguments": "path=a.txt"},
                {"tool_call_id": "call-2", "tool_name": "write_file", "arguments": "path=b.txt"},
            ],
        },
    )
    return RequestState(record=record, seq=1, status="dispatched", outcome=None, updated_at=NOW), event


def test_approval_is_durable_and_bound_to_initiator_and_route(tmp_path):
    repository = PermissionApprovalRepository(tmp_path / "team.sqlite3")
    state, event = paused_request(channel="feishu", target="oc_group", app_id="app-1")

    approval = repository.create(state, event, now=NOW)
    restored = PermissionApprovalRepository(tmp_path / "team.sqlite3").get(approval.approval_id)

    assert restored == approval
    assert restored.status == "pending"
    assert restored.expires_at == NOW + timedelta(minutes=15)
    assert repository.has_pending_session("team-1", "session-1")
    with pytest.raises(PersistentAgentError, match="another initiator"):
        repository.decide(
            approval.approval_id,
            decision="approve",
            principal_ref="other-user",
            channel="feishu",
            target_id="oc_group",
            app_id="app-1",
            tool_call_id=None,
            now=NOW,
        )
    with pytest.raises(PersistentAgentError, match="another channel"):
        repository.decide(
            approval.approval_id,
            decision="approve",
            principal_ref="user-1",
            channel="feishu",
            target_id="other-group",
            app_id="app-1",
            tool_call_id=None,
            now=NOW,
        )


def test_decision_is_one_time_and_contains_only_the_intercepted_tool_calls(tmp_path):
    repository = PermissionApprovalRepository(tmp_path / "team.sqlite3")
    state, event = paused_request()
    approval = repository.create(state, event, now=NOW)
    with pytest.raises(PersistentAgentError, match="one intercepted tool call"):
        repository.decide(
            approval.approval_id,
            decision="approve",
            principal_ref="user-1",
            channel="web",
            target_id="conversation-1",
            app_id=None,
            tool_call_id=None,
            now=NOW,
        )

    decided = repository.decide(
        approval.approval_id,
        decision="approve",
        principal_ref="user-1",
        channel="web",
        target_id="conversation-1",
        app_id=None,
        tool_call_id="call-1",
        now=NOW,
    )

    assert decided.status == "approved"
    assert [item["tool_call_id"] for item in decided.reply["responses"]] == ["call-1", "call-2"]
    assert [item["choice"] for item in decided.reply["responses"]] == ["approve", "reject"]
    with pytest.raises(PersistentAgentError, match="no longer pending"):
        repository.decide(
            approval.approval_id,
            decision="reject",
            principal_ref="user-1",
            channel="web",
            target_id="conversation-1",
            app_id=None,
            tool_call_id="call-2",
            now=NOW,
        )


def test_expired_approval_is_durably_rejected_and_unblocks_after_consumption(tmp_path):
    repository = PermissionApprovalRepository(tmp_path / "team.sqlite3")
    state, event = paused_request()
    approval = repository.create(state, event, now=NOW)

    expired, = repository.expire_pending(now=approval.expires_at)

    assert expired.status == "expired"
    assert all(item["choice"] == "reject" for item in expired.reply["responses"])
    assert repository.ready("team-1") == [expired]
    repository.consume(approval.approval_id)
    assert not repository.has_pending_session("team-1", "session-1")
