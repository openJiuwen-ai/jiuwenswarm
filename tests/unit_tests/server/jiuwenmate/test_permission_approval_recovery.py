"""Recover only live, latest confirmation pauses from the durable host log."""

from datetime import timedelta
import sqlite3

import pytest

from openjiuwen.harness import persistent_agent as core
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.permission_approval_repository import PermissionApprovalRepository
from tests.unit_tests.server.jiuwenmate.test_commit_port import NS, NOW, accept_and_start


def durable_pause(tmp_path, *, ended=True, reason="awaiting_confirmation"):
    sessions = ExecutionRepository(tmp_path / "agents.sqlite3")
    execution = accept_and_start(sessions, "conversation", "source")
    event = core.AgentEvent.create(
        execution, event_type=core.EventType.EXECUTION_PAUSED, local_seq=1, occurred_at=NOW,
        payload={"reason": reason, "intercepted": [
            {"tool_call_id": "call", "tool_name": "read_file", "arguments": "file.txt"},
        ] if reason == "awaiting_confirmation" else []},
    )
    sessions.record_events([event], now=NOW)
    if ended:
        sessions.finish_request(NS, execution.request_id, "paused", now=NOW)
    return sessions, execution, event, PermissionApprovalRepository(sessions.db_path)


@pytest.mark.parametrize("ended", [False, True])
def test_missing_native_row_is_a_durable_session_barrier(tmp_path, ended):
    sessions, execution, event, approvals = durable_pause(tmp_path, ended=ended)
    assert approvals.unregistered_pauses(NS) == [event]
    assert approvals.unregistered_pauses(NS, session_id=execution.session_id) == [event]
    assert approvals.unregistered_pauses("other") == []
    assert approvals.unregistered_pauses(NS, session_id="other") == []
    assert approvals.has_pending_session(NS, execution.session_id)


@pytest.mark.parametrize("status,outcome", [("queued", None), ("ended", "failed"), ("ended", "completed")])
def test_closed_or_queued_requests_do_not_replay_old_pauses(tmp_path, status, outcome):
    sessions, execution, event, approvals = durable_pause(tmp_path)
    with sessions._transaction() as conn:
        conn.execute("UPDATE requests SET status=?, outcome=?", (status, outcome))
    assert not approvals.unregistered_pauses(NS)
    assert not approvals.has_pending_session(NS, execution.session_id)


@pytest.mark.parametrize("replacement", ["new_execution", "consumed_native"])
def test_superseded_or_consumed_pause_is_never_registered_again(tmp_path, replacement):
    sessions, execution, event, approvals = durable_pause(tmp_path)
    if replacement == "new_execution":
        # Equal timestamps deliberately exercise the latest execution's rowid tie-break.
        with sessions._transaction() as conn:
            conn.execute("INSERT INTO executions VALUES (?,?,?,?,?)",
                         (NS, "new-execution", execution.request_id, execution.execution_id, NOW.isoformat()))
    else:
        state = sessions.get_request(NS, execution.request_id)
        native = approvals.create(state, event, now=NOW)
        approvals.reject_unrouted(native.approval_id)
        approvals.consume(native.approval_id)
    assert not approvals.unregistered_pauses(NS)
    assert not approvals.has_pending_session(NS, execution.session_id)


def test_nonconfirmation_pause_does_not_create_an_approval(tmp_path):
    sessions, execution, event, approvals = durable_pause(tmp_path, reason="stopped")
    assert not approvals.unregistered_pauses(NS)
    assert not approvals.has_pending_session(NS, execution.session_id)


def test_rebuilt_native_keeps_original_pause_deadline_and_is_idempotent(tmp_path):
    sessions, execution, event, approvals = durable_pause(tmp_path)
    state = sessions.get_request(NS, execution.request_id)
    later = NOW + timedelta(minutes=20)
    native = approvals.create(state, event, now=later, recovered=True)
    assert native.created_at == NOW and native.expires_at == NOW + timedelta(minutes=15)
    assert approvals.create(state, event, now=later + timedelta(hours=1), recovered=True) == native
    assert approvals.unregistered_pauses(NS) == []
    expired, = approvals.expire_pending(now=later)
    assert expired.status == "expired" and expired.reply["responses"][0]["choice"] == "reject"


def test_repair_rechecks_latest_pause_inside_the_creation_transaction(tmp_path):
    sessions, execution, event, approvals = durable_pause(tmp_path)
    state = sessions.get_request(NS, execution.request_id)
    assert approvals.unregistered_pauses(NS) == [event]
    with sessions._transaction() as conn:
        conn.execute("INSERT INTO executions VALUES (?,?,?,?,?)",
                     (NS, "new-execution", execution.request_id, execution.execution_id, NOW.isoformat()))
    with pytest.raises(PersistentAgentError):
        approvals.create(state, event, now=NOW, recovered=True)
    assert approvals.pending(NS) == []


def test_approval_only_database_has_no_unregistered_host_pauses(tmp_path):
    approvals = PermissionApprovalRepository(tmp_path / "permissions.sqlite3")
    assert approvals.unregistered_pauses(NS) == []
    assert not approvals.has_pending_session(NS, "session")


def test_registration_during_session_check_cannot_drop_the_barrier(tmp_path, monkeypatch):
    sessions, execution, event, approvals = durable_pause(tmp_path)
    writer = PermissionApprovalRepository(sessions.db_path)
    state = sessions.get_request(NS, execution.request_id)

    def connect_writer():
        conn = sqlite3.connect(sessions.db_path, timeout=0.01, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(writer, "_connect", connect_writer)

    class InterleavedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT 1 FROM permission_approvals WHERE namespace_id"):
                # Registration races the native lookup and the missing-pause lookup.
                try:
                    writer.create(state, event, now=NOW)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc):
                        raise
            return cursor

    def connect_reader():
        conn = sqlite3.connect(sessions.db_path, isolation_level=None, factory=InterleavedConnection)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(approvals, "_connect", connect_reader)
    assert approvals.has_pending_session(NS, execution.session_id)
