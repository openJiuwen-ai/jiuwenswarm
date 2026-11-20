"""Message admission and lifecycle retirement share one durable authority boundary."""

from datetime import timedelta

import pytest

from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from tests.unit_tests.server.jiuwenmate.test_group_share_repository import (
    NOW, approve, create, decide, repository, scope,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


def groups(tmp_path):
    repo = GroupChatRepository(tmp_path / "agents.sqlite3", id_factory=lambda: "source-a")
    group = repo.resolve("local", "feishu", "source-chat", now=NOW)
    return repo, group.group_chat_id


def observe(repo, group_id, when=NOW, agent="agent-1", **extra):
    repo.observe_access("local", agent, group_id, "app-1", observed_at=when,
                        source_ref="feishu:message:trusted", **extra)


def leave(repo, group_id, when=NOW + timedelta(seconds=1), agent="agent-1"):
    repo.update_membership("local", agent, group_id, "app-1", status="left",
                           observed_at=when, source_ref="feishu:event:left")


def test_positive_membership_is_not_message_admission(tmp_path):
    repo, gid = groups(tmp_path)
    repo.update_membership("local", "agent-1", gid, "app-1", status="active",
                           observed_at=NOW, source_ref="event:joined")
    assert repo.active_accesses("local", "agent-1") == ()
    observe(repo, gid)
    assert len(repo.active_accesses("local", "agent-1")) == 1


def test_rejoin_requires_new_platform_message_time(tmp_path):
    repo, gid = groups(tmp_path)
    observe(repo, gid)
    leave(repo, gid)
    repo.update_membership("local", "agent-1", gid, "app-1", status="active",
                           observed_at=NOW + timedelta(seconds=2), source_ref="probe:active")
    assert not repo.active_accesses("local", "agent-1")
    observe(repo, gid, NOW + timedelta(seconds=3), message_time=NOW)
    assert not repo.active_accesses("local", "agent-1")
    observe(repo, gid, NOW + timedelta(seconds=4), message_time=NOW + timedelta(seconds=4))
    assert repo.access_for("local", "agent-1", gid).visible_from == NOW + timedelta(seconds=4)


def test_dissolution_is_terminal_even_for_later_positive_facts(tmp_path):
    repo, gid = groups(tmp_path)
    observe(repo, gid)
    repo.mark_dissolved("local", gid, observed_at=NOW + timedelta(seconds=1), source_ref="event:disband")
    repo.update_membership("local", "agent-1", gid, "app-1", status="active",
                           observed_at=NOW + timedelta(seconds=3), source_ref="probe:active")
    observe(repo, gid, NOW + timedelta(seconds=4), message_time=NOW + timedelta(seconds=4))
    assert not repo.active_accesses("local", "agent-1")


@pytest.mark.parametrize("decision", ["once", "permanent"])
def test_departure_invalidates_receipts_outbox_and_old_context_after_rejoin(tmp_path, decision):
    access, gid = groups(tmp_path)
    observe(access, gid)
    sharing = repository(tmp_path)
    sharing.bind_request_window("local", "agent-1", "session-1", "request-1")
    approved = approve(sharing, decision=decision)
    before = sharing.window_generation("local", "agent-1", "session-1", request_id="next")
    leave(access, gid)
    assert not sharing.request_window_is_current("local", "request-1")
    assert sharing.window_generation("local", "agent-1", "session-1", request_id="next") != before
    assert not sharing.has_grant(scope(), gid)
    # Receipts remain available as file provenance, never as renewed authority.
    assert sharing.approvals_for_session("local", "agent-1", "session-1")[0]["approval_id"] == approved["approval_id"]
    observe(access, gid, NOW + timedelta(seconds=2), message_time=NOW + timedelta(seconds=2))
    assert not sharing.request_window_is_current("local", "request-1")
    assert not sharing.has_grant(scope(), gid)
    assert sharing.request_window_is_current("local", "new-request")


def test_departure_expires_pending_without_affecting_other_agent(tmp_path):
    access, gid = groups(tmp_path)
    observe(access, gid)
    observe(access, gid, agent="other-agent")
    sharing = repository(tmp_path)
    pending = create(sharing)
    sharing.bind_native("local", "request-1", "native")
    other = approve(sharing, scope(agent_id="other-agent", request_id="other-request"))
    leave(access, gid)
    assert sharing.get(pending["approval_id"])["expires_at"] <= (NOW + timedelta(seconds=1)).isoformat()
    assert not sharing.request_window_is_current("local", "request-1")
    assert sharing.request_window_is_current("local", "other-request")
    assert sharing.has_grant(other["scope"], gid)
    assert len(sharing.expire_pending(now=NOW + timedelta(seconds=2))) == 1


def test_target_departure_revokes_source_grants_by_platform_route(tmp_path):
    access = GroupChatRepository(tmp_path / "agents.sqlite3")
    target = access.resolve("local", "feishu", "dest-1", now=NOW)
    observe(access, target.group_chat_id)
    sharing = repository(tmp_path)
    approve(sharing)
    leave(access, target.group_chat_id)
    assert not sharing.has_grant(scope(), "source-a")
    assert not sharing.request_window_is_current("local", "request-1")


def test_dissolution_retires_all_agents_grants(tmp_path):
    access, gid = groups(tmp_path)
    sharing = repository(tmp_path)
    for agent in ("agent-1", "other-agent"):
        observe(access, gid, agent=agent)
        row = create(sharing, scope(agent_id=agent, request_id=agent + "-request"))
        sharing.bind_native("local", row["request_id"], "native-" + agent)
        decide(sharing, row)
    access.mark_dissolved("local", gid, observed_at=NOW + timedelta(seconds=1), source_ref="event:disband")
    for agent in ("agent-1", "other-agent"):
        assert not access.active_accesses("local", agent)
        assert not sharing.has_grant(scope(agent_id=agent), gid)
        assert not sharing.request_window_is_current("local", agent + "-request")


def test_retirement_cannot_be_raced_by_freezing_a_new_receipt_or_late_decision(tmp_path):
    access, gid = groups(tmp_path)
    observe(access, gid)
    sharing = repository(tmp_path)
    pending = create(sharing)
    sharing.bind_native("local", "request-1", "native")
    leave(access, gid)
    with pytest.raises(PersistentAgentError, match="Group access changed"):
        decide(sharing, pending)
    with pytest.raises(PersistentAgentError, match="Source group access changed"):
        create(sharing, scope(request_id="late-request"))
    assert not sharing.has_grant(scope(), gid)


def test_same_platform_id_on_other_platform_does_not_revoke_grants(tmp_path):
    access = GroupChatRepository(tmp_path / "agents.sqlite3")
    unrelated = access.resolve("local", "dingtalk", "dest-1", now=NOW)
    observe(access, unrelated.group_chat_id)
    sharing = repository(tmp_path)
    approve(sharing)
    leave(access, unrelated.group_chat_id)
    assert sharing.has_grant(scope(), "source-a")
    assert sharing.request_window_is_current("local", "request-1")


def test_owner_read_receipt_and_context_also_retire(tmp_path):
    access, gid = groups(tmp_path)
    observe(access, gid)
    sharing = repository(tmp_path)
    owner = scope(principal_ref="local", release_policy="owner_direct",
                  target={"channel": "web", "app_id": "", "kind": "direct", "id": "owner", "name": "所有者"})
    row = create(sharing, owner)
    assert row["status"] == "owner_read"
    leave(access, gid)
    assert not sharing.request_window_is_current("local", "request-1")
    assert sharing.window_generation("local", "agent-1", "session-1")


def test_late_removal_cannot_override_newer_message(tmp_path):
    access, gid = groups(tmp_path)
    observe(access, gid)
    observe(access, gid, NOW + timedelta(seconds=3), message_time=NOW + timedelta(seconds=3))
    leave(access, gid, NOW + timedelta(seconds=1))
    assert access.active_accesses("local", "agent-1")


def test_target_retirement_invalidates_bound_request_before_any_receipt_exists(tmp_path):
    from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
    from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef

    access = GroupChatRepository(tmp_path / "agents.sqlite3")
    target = access.resolve("local", "feishu", "dest-1", now=NOW)
    observe(access, target.group_chat_id)
    sessions = ExecutionRepository(tmp_path / "agents.sqlite3")
    session = sessions.resolve_session("local", "agent-1", ConversationRef.group(target.group_chat_id), now=NOW)
    sharing = repository(tmp_path)
    sharing.bind_request_window("local", "agent-1", session.session_id, "request-1")
    leave(access, target.group_chat_id)
    assert not sharing.request_window_is_current("local", "request-1")
    with pytest.raises(PersistentAgentError, match="Target group access changed"):
        create(sharing, scope(request_id="unbound-old-request"))
    observe(access, target.group_chat_id, NOW + timedelta(seconds=2), message_time=NOW + timedelta(seconds=2))
    with pytest.raises(PersistentAgentError, match="Group access changed"):
        create(sharing)
