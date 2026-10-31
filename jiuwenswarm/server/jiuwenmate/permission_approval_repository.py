"""Durable, one-time permission approvals bound to their original Request route."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from jiuwenswarm.server.jiuwenmate.execution_models import RequestRouteSnapshot, RequestState
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore

APPROVAL_TIMEOUT = timedelta(minutes=15)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS permission_approvals (
    approval_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    config_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    principal_ref TEXT NOT NULL,
    route_json TEXT,
    intercepted_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    reply_json TEXT,
    approver_agent_id TEXT NOT NULL DEFAULT '',
    UNIQUE (namespace_id, execution_id)
);
CREATE INDEX IF NOT EXISTS permission_approvals_by_session
    ON permission_approvals (namespace_id, session_id, status, created_at);
CREATE INDEX IF NOT EXISTS permission_approvals_by_request
    ON permission_approvals (namespace_id, request_id, created_at);
CREATE INDEX IF NOT EXISTS permission_approvals_by_initiator
    ON permission_approvals (namespace_id, agent_id, principal_ref, status, created_at);
CREATE TABLE IF NOT EXISTS permission_reply_messages (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    app_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    principal_ref TEXT NOT NULL,
    message_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    PRIMARY KEY (namespace_id, agent_id, channel, app_id, target_id, principal_ref, message_id)
);
"""


@dataclass(frozen=True, kw_only=True)
class PermissionApproval:
    approval_id: str
    namespace_id: str
    agent_id: str
    session_id: str
    request_id: str
    execution_id: str
    config_version: str
    config_fingerprint: str
    principal_ref: str
    route: RequestRouteSnapshot | None
    intercepted: tuple[dict[str, Any], ...]
    created_at: datetime
    expires_at: datetime
    status: str
    approver_agent_id: str = ""
    decision: str | None = None
    reply: dict[str, Any] | None = None

    def to_view(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "request_id": self.request_id,
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "intercepted": [dict(item) for item in self.intercepted],
        }


class PermissionApprovalRepository(SQLiteStore):
    """SQLite state needed to resume one exact native permission interruption after restart."""

    _schema = _SCHEMA

    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        with self._transaction() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(permission_approvals)")}
            if "approver_agent_id" not in columns:
                conn.execute("ALTER TABLE permission_approvals ADD COLUMN approver_agent_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _approval(row) -> PermissionApproval:
        route = json.loads(row["route_json"]) if row["route_json"] else None
        return PermissionApproval(
            approval_id=row["approval_id"],
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            request_id=row["request_id"],
            execution_id=row["execution_id"],
            config_version=row["config_version"],
            config_fingerprint=row["config_fingerprint"],
            principal_ref=row["principal_ref"],
            route=None if route is None else RequestRouteSnapshot.from_dict(route),
            intercepted=tuple(json.loads(row["intercepted_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            status=row["status"],
            approver_agent_id=row["approver_agent_id"] or row["agent_id"],
            decision=row["decision"],
            reply=json.loads(row["reply_json"]) if row["reply_json"] else None,
        )

    @staticmethod
    def _reply(
        intercepted: tuple[dict[str, Any], ...],
        choice: Literal["approve", "reject"],
        feedback: str = "",
        approved_tool_call_id: str | None = None,
    ):
        from openjiuwen.harness.persistent_agent import ConfirmReply

        return ConfirmReply.model_validate({
            "responses": [
                {
                    "kind": "permission",
                    "tool_call_id": item["tool_call_id"],
                    "choice": (
                        "approve"
                        if choice == "approve" and item["tool_call_id"] == approved_tool_call_id
                        else "reject"
                    ),
                    **({"feedback": feedback} if feedback else {}),
                }
                for item in intercepted
            ]
        }).model_dump(mode="json")

    def create(self, state: RequestState, event, *, now: datetime, recovered: bool = False) -> PermissionApproval:
        """Create or return one pending approval from a durably recorded pause event."""
        record = state.record
        payload = event.payload
        intercepted = tuple(item.model_dump(mode="json") for item in payload.intercepted)
        if not intercepted or payload.reason != "awaiting_confirmation":
            raise PersistentAgentError("execution is not awaiting permission confirmation")
        route = record.route_snapshot
        principal = record.access.principal_ref
        approver_agent_id = record.agent_id
        # Business-sharing confirmations retain their dedicated owner-only decision path.
        sharing = any(item.get("approval_ref") for item in intercepted)
        if not sharing and record.permission_origin is not None:
            origin = record.permission_origin
            principal, route, approver_agent_id = origin.principal_ref, origin.route, origin.agent_id
        elif (not sharing and record.request_input.kind == "group_message"
              and record.request_input.source.sender_kind == "agent"):
            # A Bot handoff without a verified human root must not be approved by
            # the Bot itself or by an arbitrary member of the current group.
            route = None
        approval_id = secrets.token_hex(6)
        created_at = event.occurred_at if recovered else now
        expires_at = created_at + APPROVAL_TIMEOUT
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM permission_approvals WHERE namespace_id = ? AND execution_id = ?",
                (record.namespace_id, event.execution_id),
            ).fetchone()
            if existing is not None:
                return self._approval(existing)
            if recovered and not any(
                row["event_id"] == event.event_id
                for row in self._unregistered_pauses(conn, record.namespace_id, record.session_id)
            ):
                raise PersistentAgentError("confirmation pause is no longer current", code="CONFLICT")
            # A previous one-time decision has now been applied; a new pause replaces it.
            conn.execute(
                "UPDATE permission_approvals SET status = 'consumed' WHERE namespace_id = ? AND request_id = ?"
                " AND status IN ('approved', 'rejected', 'expired')",
                (record.namespace_id, record.request_id),
            )
            conn.execute(
                "INSERT INTO permission_approvals (approval_id,namespace_id,agent_id,session_id,request_id,"
                "execution_id,config_version,config_fingerprint,principal_ref,route_json,intercepted_json,"
                "created_at,expires_at,status,decision,reply_json,approver_agent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)",
                (
                    approval_id,
                    record.namespace_id,
                    record.agent_id,
                    record.session_id,
                    record.request_id,
                    event.execution_id,
                    record.config_version,
                    record.config_fingerprint,
                    principal,
                    None if route is None else json.dumps(route.to_dict(), ensure_ascii=False),
                    json.dumps(intercepted, ensure_ascii=False),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                    approver_agent_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._approval(row)

    @staticmethod
    def _unregistered_pauses(conn: sqlite3.Connection, namespace_id: str, session_id: str | None) -> list[sqlite3.Row]:
        # Permission-only repositories do not own host request/log tables.
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('requests','executions','execution_events')",
        ).fetchall()
        if len(tables) != 3:
            return []
        session_clause = " AND r.session_id=?" if session_id is not None else ""
        params = (namespace_id, session_id) if session_id is not None else (namespace_id,)
        return conn.execute(
            "SELECT event.event_id, event.event_json FROM requests r "
            "JOIN executions e ON e.namespace_id=r.namespace_id AND e.request_id=r.request_id "
            "JOIN execution_events event ON event.namespace_id=e.namespace_id "
            "AND event.execution_id=e.execution_id AND event.request_id=r.request_id "
            "WHERE r.namespace_id=?" + session_clause + " "
            "AND (r.status='dispatched' OR (r.status='ended' AND r.outcome='paused')) "
            "AND e.rowid=(SELECT latest.rowid FROM executions latest "
            "WHERE latest.namespace_id=r.namespace_id AND latest.request_id=r.request_id "
            "ORDER BY latest.started_at DESC, latest.rowid DESC LIMIT 1) "
            "AND event.local_seq=(SELECT MAX(tail.local_seq) FROM execution_events tail "
            "WHERE tail.namespace_id=e.namespace_id AND tail.execution_id=e.execution_id) "
            "AND event.event_type='execution.paused' "
            "AND json_extract(event.event_json, '$.payload.reason')='awaiting_confirmation' "
            "AND NOT EXISTS (SELECT 1 FROM permission_approvals native "
            "WHERE native.namespace_id=e.namespace_id AND native.execution_id=e.execution_id) "
            "ORDER BY event.rowid",
            params,
        ).fetchall()

    def unregistered_pauses(self, namespace_id: str, session_id: str | None = None) -> list:
        """Latest live confirmation pauses without a native row, including the crash/write-failure gap.

        Any native row, even a consumed one, prevents historical re-registration.
        The creation transaction rechecks eligibility before repairing the gap.
        """
        from contextlib import closing

        from openjiuwen.harness.persistent_agent import AgentEvent

        with closing(self._connect()) as conn:
            rows = self._unregistered_pauses(conn, namespace_id, session_id)
        return [AgentEvent.model_validate_json(row["event_json"]) for row in rows]

    def pending_for_request(self, namespace_id: str, request_id: str) -> PermissionApproval | None:
        from contextlib import closing

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE namespace_id = ? AND request_id = ?"
                " AND status IN ('pending', 'approved', 'rejected', 'expired')"
                " ORDER BY created_at DESC LIMIT 1",
                (namespace_id, request_id),
            ).fetchone()
        return None if row is None else self._approval(row)

    def get(self, approval_id: str) -> PermissionApproval | None:
        from contextlib import closing

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return None if row is None else self._approval(row)

    def pending(self, namespace_id: str | None = None) -> list[PermissionApproval]:
        from contextlib import closing

        query = "SELECT * FROM permission_approvals WHERE status = 'pending'"
        values: tuple[Any, ...] = ()
        if namespace_id is not None:
            query += " AND namespace_id = ?"
            values = (namespace_id,)
        query += " ORDER BY created_at"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, values).fetchall()
        return [self._approval(row) for row in rows]

    def for_route(
        self, namespace_id: str, agent_id: str, *, principal_ref: str | None,
        channel: str, app_id: str, target_id: str,
    ) -> list[PermissionApproval]:
        """Find pending confirmations on a route, optionally limited to its initiator."""
        from contextlib import closing

        query = ("SELECT * FROM permission_approvals WHERE namespace_id = ? "
                 "AND COALESCE(NULLIF(approver_agent_id,''),agent_id) = ? AND status = 'pending'")
        values: tuple[Any, ...] = (namespace_id, agent_id)
        if principal_ref is not None:
            query += " AND principal_ref = ?"
            values += (principal_ref,)
        with closing(self._connect()) as conn:
            rows = conn.execute(query + " ORDER BY created_at", values).fetchall()
        approvals = [self._approval(row) for row in rows]
        return [item for item in approvals if item.route is not None
                and (item.route.channel, item.route.app_id, item.route.target_id) == (channel, app_id, target_id)]

    def reply_approval(
        self, *, namespace_id: str, agent_id: str, channel: str, app_id: str,
        target_id: str, principal_ref: str, message_id: str, approval_id: str | None = None,
    ) -> str | None:
        """Bind once; an empty approval ID records a command with no unambiguous target."""
        identity = (namespace_id, agent_id, channel, app_id, target_id, principal_ref, message_id)
        with self._transaction() as conn:
            if approval_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO permission_reply_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*identity, approval_id),
                )
            row = conn.execute(
                "SELECT approval_id FROM permission_reply_messages WHERE namespace_id = ? AND agent_id = ?"
                " AND channel = ? AND app_id = ? AND target_id = ? AND principal_ref = ? AND message_id = ?",
                identity,
            ).fetchone()
        return None if row is None else row["approval_id"]

    def ready(self, namespace_id: str | None = None) -> list[PermissionApproval]:
        from contextlib import closing

        query = (
            "SELECT * FROM permission_approvals WHERE status IN ('approved', 'rejected', 'expired')"
            " AND reply_json IS NOT NULL"
        )
        values: tuple[Any, ...] = ()
        if namespace_id is not None:
            query += " AND namespace_id = ?"
            values = (namespace_id,)
        query += " ORDER BY created_at"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, values).fetchall()
        return [self._approval(row) for row in rows]

    def has_pending_session(self, namespace_id: str, session_id: str) -> bool:
        from contextlib import closing

        with closing(self._connect()) as conn:
            # Both lookups must see one snapshot: registration can move the
            # barrier from the durable pause to a native row between reads.
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT 1 FROM permission_approvals WHERE namespace_id = ? AND session_id = ?"
                    " AND status IN ('pending', 'approved', 'rejected', 'expired')"
                    " LIMIT 1",
                    (namespace_id, session_id),
                ).fetchone()
                return row is not None or bool(self._unregistered_pauses(conn, namespace_id, session_id))
            finally:
                conn.rollback()

    def is_ready_for_request(self, namespace_id: str, session_id: str, request_id: str) -> bool:
        """Whether this queued Request is the continuation authorized by a durable decision."""
        from contextlib import closing

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM permission_approvals WHERE namespace_id = ? AND session_id = ? AND request_id = ?"
                " AND status IN ('approved', 'rejected', 'expired') AND reply_json IS NOT NULL LIMIT 1",
                (namespace_id, session_id, request_id),
            ).fetchone()
        return row is not None

    def reject_unrouted(self, approval_id: str) -> PermissionApproval:
        """Fail closed when no trusted interactive route was captured for the Request."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise PersistentAgentError("permission approval not found", code="NOT_FOUND")
            approval = self._approval(row)
            if approval.route is not None or approval.status != "pending":
                return approval
            reply = self._reply(
                approval.intercepted, "reject", "Interactive approval is unavailable for this Request."
            )
            conn.execute(
                "UPDATE permission_approvals SET status = 'rejected', decision = 'reject', reply_json = ?"
                " WHERE approval_id = ? AND status = 'pending'",
                (json.dumps(reply, ensure_ascii=False), approval_id),
            )
            updated = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._approval(updated)

    def decide(
        self,
        approval_id: str,
        *,
        decision: Literal["approve", "reject"],
        principal_ref: str,
        channel: str,
        target_id: str,
        app_id: str | None,
        tool_call_id: str | None,
        now: datetime,
        approver_agent_id: str | None = None,
    ) -> PermissionApproval:
        """Apply one authenticated response only on the stored channel, target, and initiator."""
        if decision not in ("approve", "reject"):
            raise PersistentAgentError("invalid permission decision")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise PersistentAgentError("permission approval not found", code="NOT_FOUND")
            approval = self._approval(row)
            if approval.status != "pending":
                raise PersistentAgentError("permission approval is no longer pending", code="CONFLICT")
            if approval.principal_ref != principal_ref or approval.route is None:
                raise PersistentAgentError("permission approval belongs to another initiator", code="FORBIDDEN")
            if approval.approver_agent_id != (approver_agent_id or approval.agent_id):
                raise PersistentAgentError("permission approval belongs to another receiving agent", code="FORBIDDEN")
            if (
                approval.route.channel != channel
                or approval.route.target_id != target_id
                or approval.route.app_id != app_id
            ):
                raise PersistentAgentError("permission approval belongs to another channel or conversation", code="FORBIDDEN")
            if decision == "approve" and tool_call_id not in {
                item["tool_call_id"] for item in approval.intercepted
            }:
                raise PersistentAgentError("approval must name one intercepted tool call")
            if decision == "reject" and tool_call_id is not None:
                raise PersistentAgentError("rejection cannot name a tool call")
            expired = now >= approval.expires_at
            effective = "reject" if expired else decision
            status = "expired" if expired else "approved" if effective == "approve" else "rejected"
            feedback = "Permission approval expired; the tool call was denied." if expired else ""
            reply = self._reply(
                approval.intercepted,
                effective,
                feedback,
                approved_tool_call_id=tool_call_id if effective == "approve" else None,
            )
            conn.execute(
                "UPDATE permission_approvals SET status = ?, decision = ?, reply_json = ?"
                " WHERE approval_id = ? AND status = 'pending'",
                (status, effective, json.dumps(reply, ensure_ascii=False), approval_id),
            )
            updated = conn.execute(
                "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._approval(updated)

    def expire_pending(self, *, now: datetime) -> list[PermissionApproval]:
        """Turn every overdue approval into a durable rejection response."""
        expired: list[PermissionApproval] = []
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM permission_approvals WHERE status = 'pending' AND expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                approval = self._approval(row)
                reply = self._reply(
                    approval.intercepted, "reject", "Permission approval expired; the tool call was denied."
                )
                conn.execute(
                    "UPDATE permission_approvals SET status = 'expired', decision = 'reject', reply_json = ?"
                    " WHERE approval_id = ? AND status = 'pending'",
                    (json.dumps(reply, ensure_ascii=False), approval.approval_id),
                )
                updated = conn.execute(
                    "SELECT * FROM permission_approvals WHERE approval_id = ?", (approval.approval_id,)
                ).fetchone()
                expired.append(self._approval(updated))
        return expired

    def decide_sharing(self, approval_id: str, *, decisions: dict[str, bool], now: datetime) -> PermissionApproval:
        """Apply host-authenticated owner decisions to exact business-sharing interruptions only.

        This internal entry is never a tool-approval API. The caller authenticates the owner and
        binds each decision to its sharing record; unrelated intercepted operations remain denied.
        """
        from openjiuwen.harness.persistent_agent import ConfirmReply

        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                raise PersistentAgentError("permission approval not found", code="NOT_FOUND")
            approval = self._approval(row)
            if approval.status != "pending":
                return approval
            calls = {item["tool_call_id"]: item for item in approval.intercepted}
            if not decisions or any(
                call_id not in calls or calls[call_id]["tool_name"] not in {
                    "search_group_messages", "bash", "share_file", "read_file", "list_files", "list_dir",
                }
                or not calls[call_id].get("approval_ref") or not isinstance(choice, bool)
                for call_id, choice in decisions.items()
            ):
                raise PersistentAgentError("sharing decisions must name exact business sharing calls", code="FORBIDDEN")
            expired = now >= approval.expires_at
            allowed = {key for key, choice in decisions.items() if choice and not expired}
            reply = ConfirmReply.model_validate({"responses": [
                {"kind": "permission", "tool_call_id": item["tool_call_id"],
                 "choice": "approve" if item["tool_call_id"] in allowed else "reject",
                 "feedback": None if item["tool_call_id"] in allowed else "Cross-group sharing was not approved."}
                for item in approval.intercepted
            ]}).model_dump(mode="json")
            status = "expired" if expired else "approved" if allowed else "rejected"
            conn.execute(
                "UPDATE permission_approvals SET status = ?, decision = ?, reply_json = ?"
                " WHERE approval_id = ? AND status = 'pending'",
                (status, "approve" if allowed else "reject", json.dumps(reply, ensure_ascii=False), approval_id),
            )
            updated = conn.execute("SELECT * FROM permission_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return self._approval(updated)

    def resume_reply(self, namespace_id: str, request_id: str) -> PermissionApproval | None:
        """Return the newest decided reply, retained until the resumed execution is durably terminal."""
        from contextlib import closing

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM permission_approvals WHERE namespace_id = ? AND request_id = ?"
                " AND status IN ('approved', 'rejected', 'expired') AND reply_json IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (namespace_id, request_id),
            ).fetchone()
        return None if row is None else self._approval(row)

    def consume(self, approval_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE permission_approvals SET status = 'consumed' WHERE approval_id = ?"
                " AND status IN ('approved', 'rejected', 'expired')",
                (approval_id,),
            )
