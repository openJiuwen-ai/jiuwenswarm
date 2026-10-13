"""Sessions, accepted Requests and their Executions.

A Session is found by the full ``(namespace, agent, conversation kind, channel,
key)`` tuple, each part in its own column, so two conversations never share a
Session because of joined or truncated strings. Requests are written before
dispatch and survive restarts; a supplementary message is always a new Request.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jiuwenswarm.server.runtime.persistent_agents.execution_models import (
    ENDED_OUTCOMES,
    AccessSnapshot,
    ConversationRef,
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
    RequestState,
    SessionRecord,
)
from jiuwenswarm.server.runtime.persistent_agents.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.runtime.persistent_agents.spec_provider import ConfigSnapshot
from jiuwenswarm.server.runtime.persistent_agents.sqlite_store import SQLiteStore
from openjiuwen.harness.persistent_agent import RequestInput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    namespace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    next_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, session_id),
    UNIQUE (namespace_id, agent_id, kind, channel, conversation_key)
);
CREATE TABLE IF NOT EXISTS requests (
    namespace_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, request_id),
    UNIQUE (namespace_id, agent_id, source_ref),
    UNIQUE (namespace_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS requests_by_status ON requests (namespace_id, status, session_id, seq);
CREATE TABLE IF NOT EXISTS executions (
    namespace_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    recovery_ref TEXT,
    started_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, execution_id)
);
CREATE INDEX IF NOT EXISTS executions_by_request ON executions (namespace_id, request_id, started_at);
"""


def _uuid_ids(prefix: str) -> Callable[[], str]:
    return lambda: prefix + uuid.uuid4().hex


class ExecutionRepository(SQLiteStore):
    """Session lookup, the per-Session Request queue and Execution attempts."""

    _schema = _SCHEMA

    def __init__(
        self,
        db_path: Path,
        *,
        session_ids: Callable[[], str] | None = None,
        request_ids: Callable[[], str] | None = None,
        execution_ids: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(db_path)
        self._session_id = session_ids or _uuid_ids("sess_")
        self._request_id = request_ids or _uuid_ids("req_")
        self._execution_id = execution_ids or _uuid_ids("exe_")

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            namespace_id=row["namespace_id"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            conversation=ConversationRef(kind=row["kind"], channel=row["channel"], key=row["conversation_key"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _request(row: sqlite3.Row) -> RequestState:
        return RequestState(
            record=RequestRecord.from_dict(json.loads(row["record_json"])),
            seq=row["seq"],
            status=row["status"],
            outcome=row["outcome"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _execution(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            request_id=row["request_id"], execution_id=row["execution_id"], recovery_ref=row["recovery_ref"]
        )

    def resolve_session(
        self, namespace_id: str, agent_id: str, conversation: ConversationRef, *, now: datetime
    ) -> SessionRecord:
        """Return the colleague's Session for a conversation, creating it on first contact."""
        require_identifier("namespace_id", namespace_id)
        require_identifier("agent_id", agent_id)
        key = (namespace_id, agent_id, conversation.kind, conversation.channel, conversation.key)
        query = (
            "SELECT * FROM sessions WHERE namespace_id = ? AND agent_id = ? AND kind = ? "
            "AND channel = ? AND conversation_key = ?"
        )
        with self._transaction() as conn:
            row = conn.execute(query, key).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (namespace_id, self._session_id(), *key[1:], now.isoformat()),
                )
                row = conn.execute(query, key).fetchone()
        return self._session(row)

    def get_session(self, namespace_id: str, session_id: str) -> SessionRecord:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE namespace_id = ? AND session_id = ?", (namespace_id, session_id)
            ).fetchone()
        if row is None:
            raise PersistentAgentError("session not found", code="NOT_FOUND")
        return self._session(row)

    def accept_request(
        self,
        session: SessionRecord,
        *,
        snapshot: ConfigSnapshot,
        source_ref: str,
        access: AccessSnapshot,
        request_input: RequestInput,
        received_at: datetime,
    ) -> tuple[RequestState, bool]:
        """Queue a Request pinned to ``snapshot``; a repeated ``source_ref`` returns the stored one.

        The boolean is ``False`` when the source event was already accepted, so a
        redelivered platform event is not dispatched twice.
        """
        require_identifier("source_ref", source_ref)
        if snapshot.agent_id != session.agent_id:
            raise PersistentContractError("configuration snapshot belongs to another colleague")
        if not session.conversation.accepts(request_input.kind):
            raise PersistentContractError(
                f"{request_input.kind} cannot enter a {session.conversation.kind} Session",
                code="REQUEST_SOURCE_MISMATCH",
            )
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM requests WHERE namespace_id = ? AND agent_id = ? AND source_ref = ?",
                (session.namespace_id, session.agent_id, source_ref),
            ).fetchone()
            if existing is not None:
                if existing["session_id"] != session.session_id:
                    raise PersistentAgentError("source event already belongs to another Session", code="CONFLICT")
                return self._request(existing), False
            seq_row = conn.execute(
                "SELECT next_seq FROM sessions WHERE namespace_id = ? AND session_id = ? AND agent_id = ?",
                (session.namespace_id, session.session_id, session.agent_id),
            ).fetchone()
            if seq_row is None:
                raise PersistentAgentError("session not found", code="NOT_FOUND")
            record = RequestRecord(
                namespace_id=session.namespace_id,
                agent_id=session.agent_id,
                session_id=session.session_id,
                request_id=self._request_id(),
                source_ref=source_ref,
                config_version=snapshot.config_version,
                config_fingerprint=snapshot.fingerprint,
                access=access,
                request_input=request_input,
                received_at=received_at,
            )
            seq = seq_row["next_seq"]
            conn.execute(
                "UPDATE sessions SET next_seq = ? WHERE namespace_id = ? AND session_id = ?",
                (seq + 1, session.namespace_id, session.session_id),
            )
            conn.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)",
                (
                    record.namespace_id,
                    record.request_id,
                    record.agent_id,
                    record.session_id,
                    record.source_ref,
                    seq,
                    json.dumps(record.to_dict(), ensure_ascii=False),
                    record.received_at.isoformat(),
                ),
            )
        return RequestState(record=record, seq=seq, status="queued", outcome=None, updated_at=record.received_at), True

    def get_request(self, namespace_id: str, request_id: str) -> RequestState:
        with self._transaction() as conn:
            return self._request(self._require_request(conn, namespace_id, request_id))

    @staticmethod
    def _require_request(conn: sqlite3.Connection, namespace_id: str, request_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM requests WHERE namespace_id = ? AND request_id = ?", (namespace_id, request_id)
        ).fetchone()
        if row is None:
            raise PersistentAgentError("request not found", code="NOT_FOUND")
        return row

    def start_execution(self, namespace_id: str, request_id: str, *, now: datetime) -> ExecutionRecord:
        """Mark a queued Request dispatched and record its first Execution.

        A Session runs one Request at a time, so this fails while another Request
        of the same Session is dispatched.
        """
        with self._transaction() as conn:
            row = self._require_request(conn, namespace_id, request_id)
            if row["status"] != "queued":
                raise PersistentAgentError(f"request is {row['status']}", code="CONFLICT")
            busy = conn.execute(
                "SELECT 1 FROM requests WHERE namespace_id = ? AND session_id = ? AND status = 'dispatched'",
                (namespace_id, row["session_id"]),
            ).fetchone()
            if busy is not None:
                raise PersistentAgentError("session already has a dispatched request", code="SESSION_BUSY")
            execution = ExecutionRecord(request_id=request_id, execution_id=self._execution_id())
            conn.execute(
                "INSERT INTO executions VALUES (?, ?, ?, NULL, ?)",
                (namespace_id, execution.execution_id, request_id, now.isoformat()),
            )
            conn.execute(
                "UPDATE requests SET status = 'dispatched', updated_at = ? WHERE namespace_id = ? AND request_id = ?",
                (now.isoformat(), namespace_id, request_id),
            )
        return execution

    def finish_request(self, namespace_id: str, request_id: str, outcome: str, *, now: datetime) -> RequestState:
        """Close a queued or dispatched Request; finishing it again with the same outcome is a no-op."""
        if outcome not in ENDED_OUTCOMES:
            raise PersistentAgentError(f"invalid outcome {outcome!r}")
        with self._transaction() as conn:
            row = self._require_request(conn, namespace_id, request_id)
            if row["status"] == "ended":
                if row["outcome"] != outcome:
                    raise PersistentAgentError(f"request already ended as {row['outcome']}", code="CONFLICT")
                return self._request(row)
            conn.execute(
                "UPDATE requests SET status = 'ended', outcome = ?, updated_at = ? "
                "WHERE namespace_id = ? AND request_id = ?",
                (outcome, now.isoformat(), namespace_id, request_id),
            )
            return self._request(self._require_request(conn, namespace_id, request_id))

    def executions(self, namespace_id: str, request_id: str) -> list[ExecutionRecord]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM executions WHERE namespace_id = ? AND request_id = ? ORDER BY started_at, rowid",
                (namespace_id, request_id),
            ).fetchall()
        return [self._execution(row) for row in rows]

    def session_requests(self, namespace_id: str, session_id: str) -> list[RequestState]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE namespace_id = ? AND session_id = ? ORDER BY seq",
                (namespace_id, session_id),
            ).fetchall()
        return [self._request(row) for row in rows]

    def pending_requests(self, namespace_id: str) -> list[RequestState]:
        """Queued and dispatched Requests, grouped by Session in arrival order, for restart recovery."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE namespace_id = ? AND status IN ('queued', 'dispatched') "
                "ORDER BY session_id, seq",
                (namespace_id,),
            ).fetchall()
        return [self._request(row) for row in rows]
