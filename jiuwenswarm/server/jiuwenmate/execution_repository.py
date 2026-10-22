"""Sessions, accepted Requests, their Executions, event logs and candidate outputs.

A Session is found by the full ``(namespace, agent, conversation kind, channel,
key)`` tuple, each part in its own column, so two conversations never share a
Session because of joined or truncated strings. Requests are written before
dispatch and survive restarts; a supplementary message is always a new Request.
Events are stored once per ``event_id``; a candidate output keeps the ``output_id``
of the event that first proposed it, and a file candidate's file is retained by
``OutputFileStore`` before the candidate is stored.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.execution_models import (
    ENDED_OUTCOMES,
    AccessSnapshot,
    ConversationRef,
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
    RequestRouteSnapshot,
    RequestState,
    SessionRecord,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.output_files import FILE_SHARE_MODES, output_file_key
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore
from openjiuwen.harness.persistent_agent import AgentEvent, EventType, RequestInput

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
CREATE TABLE IF NOT EXISTS execution_events (
    namespace_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    local_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    event_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, event_id),
    UNIQUE (namespace_id, execution_id, local_seq)
);
CREATE TABLE IF NOT EXISTS candidate_outputs (
    namespace_id TEXT NOT NULL,
    output_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    share_mode TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, output_id)
);
CREATE TABLE IF NOT EXISTS result_deliveries (
    namespace_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    output_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    external_message_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    file_key TEXT,
    PRIMARY KEY (namespace_id, delivery_id),
    UNIQUE (namespace_id, request_id, output_id, channel, target_id)
);
CREATE INDEX IF NOT EXISTS result_deliveries_by_status
    ON result_deliveries (namespace_id, channel, status, created_at);
CREATE TABLE IF NOT EXISTS group_delivery_archives (
    namespace_id TEXT NOT NULL, delivery_id TEXT NOT NULL, PRIMARY KEY(namespace_id, delivery_id)
);
CREATE TABLE IF NOT EXISTS execution_links (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    predecessor_id TEXT,
    relation TEXT NOT NULL,
    PRIMARY KEY (namespace_id, execution_id)
);
"""
_TERMINAL_EVENTS = (EventType.EXECUTION_PAUSED.value, EventType.EXECUTION_FINAL.value)


def _deliverable(share_mode: str, payload: dict[str, object]) -> bool:
    """A nonblank message, or a file candidate with its reference; other modes get no delivery."""
    if share_mode == "message":
        text = payload.get("text")
        return isinstance(text, str) and bool(text.strip())
    return (
        share_mode in FILE_SHARE_MODES
        and isinstance(payload.get("file_ref"), str)
        and isinstance(payload.get("file_path"), str)
        and bool(payload["file_path"])
    )


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
        with self._transaction() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(result_deliveries)")}
            if "file_key" not in columns:
                conn.execute("ALTER TABLE result_deliveries ADD COLUMN file_key TEXT")
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

    def find_session(self, namespace_id: str, agent_id: str, conversation: ConversationRef) -> SessionRecord | None:
        """The colleague's Session for a conversation, or None before first contact; nothing is created."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE namespace_id = ? AND agent_id = ? AND kind = ? "
                "AND channel = ? AND conversation_key = ?",
                (namespace_id, agent_id, conversation.kind, conversation.channel, conversation.key),
            ).fetchone()
        return None if row is None else self._session(row)

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
        route_snapshot: RequestRouteSnapshot | None = None,
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
                route_snapshot=route_snapshot,
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
            execution = ExecutionRecord(
                request_id=request_id,
                execution_id=self._execution_id(),
                recovery_ref=self._pending_continuation(conn, namespace_id, request_id),
            )
            conn.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?)",
                (namespace_id, execution.execution_id, request_id, execution.recovery_ref, now.isoformat()),
            )
            self._save_link(conn, row, execution)
            conn.execute(
                "UPDATE requests SET status = 'dispatched', updated_at = ? WHERE namespace_id = ? AND request_id = ?",
                (now.isoformat(), namespace_id, request_id),
            )
        return execution

    def reopen_for_continuation(self, namespace_id: str, request_id: str, *, now: datetime) -> bool:
        """Queue a paused Request again when its latest interruption has not been continued.

        A saved final, a stop or a cancel stays ended. The same interrupted Execution
        is continued at most once; a later interruption is a new judgment.
        """
        with self._transaction() as conn:
            row = self._require_request(conn, namespace_id, request_id)
            if row["status"] != "ended" or row["outcome"] != "paused":
                return False
            if self._pending_continuation(conn, namespace_id, request_id) is None:
                return False
            conn.execute(
                "UPDATE requests SET status = 'queued', outcome = NULL, updated_at = ? "
                "WHERE namespace_id = ? AND request_id = ? AND status = 'ended'",
                (now.isoformat(), namespace_id, request_id),
            )
        return True

    def execution_link(self, namespace_id: str, request_id: str, execution_id: str) -> dict[str, object] | None:
        """The link saved before that Execution was dispatched, when it belongs to this Request."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM execution_links WHERE namespace_id = ? AND execution_id = ?",
                (namespace_id, execution_id),
            ).fetchone()
        if row is None or row["request_id"] != request_id:
            return None
        return {
            "namespace_id": row["namespace_id"],
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "request_id": row["request_id"],
            "execution_id": row["execution_id"],
            "predecessor_id": row["predecessor_id"],
            "relation": row["relation"],
        }

    @staticmethod
    def _pending_continuation(conn: sqlite3.Connection, namespace_id: str, request_id: str) -> str | None:
        try:
            row = conn.execute(
                "SELECT execution_id FROM request_recoveries WHERE namespace_id = ? AND request_id = ? "
                "AND decision = 'awaiting_continuation' ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                (namespace_id, request_id),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        taken = conn.execute(
            "SELECT 1 FROM executions WHERE namespace_id = ? AND request_id = ? AND recovery_ref = ?",
            (namespace_id, request_id, row["execution_id"]),
        ).fetchone()
        return None if taken is not None else row["execution_id"]

    @staticmethod
    def _save_link(conn: sqlite3.Connection, request_row: sqlite3.Row, execution: ExecutionRecord) -> None:
        conn.execute(
            "INSERT INTO execution_links VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_row["namespace_id"],
                request_row["agent_id"],
                request_row["session_id"],
                execution.request_id,
                execution.execution_id,
                execution.recovery_ref,
                "first" if execution.recovery_ref is None else "continuation",
            ),
        )

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

    def latest_session_request(self, namespace_id: str, session_id: str) -> RequestState | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE namespace_id = ? AND session_id = ? ORDER BY seq DESC LIMIT 1",
                (namespace_id, session_id),
            ).fetchone()
        return None if row is None else self._request(row)

    def record_events(self, events: Sequence[AgentEvent], *, now: datetime) -> None:
        """Store events of one Execution in one transaction; nothing is kept if any of them is rejected.

        A resubmitted event with the same content is skipped. A final or paused event
        is rejected unless every output it references is stored for the same Request,
        either earlier or in this batch; its message and file outputs get their
        deliveries in the same transaction. A later Execution of the Request may
        propose an output again under the same ID only with the same content.
        """
        if not events:
            return
        with self._transaction() as conn:
            for event in events:
                self._insert_event(conn, event, now)

    def _insert_event(self, conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
        owner = conn.execute(
            "SELECT r.agent_id, r.session_id, r.source_ref FROM executions e JOIN requests r "
            "ON r.namespace_id = e.namespace_id AND r.request_id = e.request_id "
            "WHERE e.namespace_id = ? AND e.execution_id = ? AND e.request_id = ?",
            (event.namespace_id, event.execution_id, event.request_id),
        ).fetchone()
        if owner is None or (owner["agent_id"], owner["session_id"], owner["source_ref"]) != (
            event.agent_id,
            event.session_id,
            event.source_ref,
        ):
            raise PersistentAgentError("event does not belong to a stored Execution", code="NOT_FOUND")
        digest = event.proposal_digest()
        stored = conn.execute(
            "SELECT digest FROM execution_events WHERE namespace_id = ? AND event_id = ?",
            (event.namespace_id, event.event_id),
        ).fetchone()
        if stored is not None:
            if stored["digest"] != digest:
                raise PersistentAgentError("event_id was recorded with other content", code="CONFLICT")
            if event.event_type.value in _TERMINAL_EVENTS:
                self._ensure_result_deliveries(conn, event, now)
            return
        if event.event_type.value in _TERMINAL_EVENTS:
            terminal = conn.execute(
                "SELECT event_id FROM execution_events WHERE namespace_id = ? AND execution_id = ? "
                "AND event_type IN (?, ?)",
                (event.namespace_id, event.execution_id, *_TERMINAL_EVENTS),
            ).fetchone()
            if terminal is not None:
                raise PersistentAgentError("execution already has a final or paused event", code="CONFLICT")
            self._require_outputs(conn, event)
        if event.event_type == EventType.OUTPUT_PROPOSED:
            self._insert_output(conn, event, now)
        conn.execute(
            "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.namespace_id,
                event.event_id,
                event.execution_id,
                event.request_id,
                event.local_seq,
                event.event_type.value,
                digest,
                event.model_dump_json(),
                now.isoformat(),
            ),
        )
        if event.event_type.value in _TERMINAL_EVENTS:
            self._ensure_result_deliveries(conn, event, now)

    @staticmethod
    def _ensure_result_deliveries(conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
        request = conn.execute(
            "SELECT record_json FROM requests WHERE namespace_id = ? AND request_id = ?",
            (event.namespace_id, event.request_id),
        ).fetchone()
        if request is None:
            raise PersistentAgentError("ending Request is missing", code="NOT_FOUND")
        record = RequestRecord.from_dict(json.loads(request["record_json"]))
        route = record.route_snapshot
        if route is None:
            return

        for output_id in event.payload.output_refs:
            output = conn.execute(
                "SELECT share_mode, payload_json FROM candidate_outputs "
                "WHERE namespace_id = ? AND request_id = ? AND output_id = ?",
                (event.namespace_id, event.request_id, output_id),
            ).fetchone()
            if output is None or not _deliverable(output["share_mode"], json.loads(output["payload_json"])):
                continue
            status = "available" if route.channel == "web" else "pending"
            conn.execute(
                "INSERT OR IGNORE INTO result_deliveries (namespace_id, delivery_id, request_id, execution_id, "
                "output_id, channel, target_id, status, idempotency_key, attempt_count, external_message_id, "
                "error_code, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)",
                (
                    event.namespace_id,
                    f"del_{uuid.uuid4().hex}",
                    event.request_id,
                    event.execution_id,
                    output_id,
                    route.channel,
                    route.target_id,
                    status,
                    str(uuid.uuid4()),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

    @staticmethod
    def _insert_output(conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
        output = event.payload
        existing = conn.execute(
            "SELECT request_id, payload_json FROM candidate_outputs WHERE namespace_id = ? AND output_id = ?",
            (event.namespace_id, output.output_id),
        ).fetchone()
        if existing is not None:
            # A later Execution of the same Request proposes the same outcome under the same ID.
            if existing["request_id"] == event.request_id and existing["payload_json"] == output.model_dump_json():
                return
            raise PersistentAgentError("output_id was proposed with other content", code="CONFLICT")
        conn.execute(
            "INSERT INTO candidate_outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.namespace_id,
                output.output_id,
                event.request_id,
                event.execution_id,
                event.event_id,
                output.target_ref,
                output.share_mode,
                output.model_dump_json(),
                now.isoformat(),
            ),
        )

    @staticmethod
    def _require_outputs(conn: sqlite3.Connection, event: AgentEvent) -> None:
        for output_id in event.payload.output_refs:
            row = conn.execute(
                "SELECT 1 FROM candidate_outputs WHERE namespace_id = ? AND output_id = ? AND request_id = ?",
                (event.namespace_id, output_id, event.request_id),
            ).fetchone()
            if row is None:
                raise PersistentAgentError(f"ending references unsaved output {output_id}", code="OUTPUT_MISSING")

    def read_execution_log(
        self, namespace_id: str, session_id: str, request_id: str, execution_id: str
    ) -> tuple[AgentEvent, ...]:
        """Saved events of an Execution of the given Session and Request, in ``local_seq`` order."""
        with self._transaction() as conn:
            owner = conn.execute(
                "SELECT 1 FROM executions e JOIN requests r "
                "ON r.namespace_id = e.namespace_id AND r.request_id = e.request_id "
                "WHERE e.namespace_id = ? AND e.execution_id = ? AND r.request_id = ? AND r.session_id = ?",
                (namespace_id, execution_id, request_id, session_id),
            ).fetchone()
            if owner is None:
                raise PersistentAgentError("execution is not part of this Request", code="NOT_FOUND")
            rows = conn.execute(
                "SELECT event_json FROM execution_events WHERE namespace_id = ? AND execution_id = ? "
                "ORDER BY local_seq",
                (namespace_id, execution_id),
            ).fetchall()
        return tuple(AgentEvent.model_validate_json(row["event_json"]) for row in rows)

    def candidate_output(self, namespace_id: str, output_id: str) -> AgentEvent | None:
        """The stored ``output.proposed`` event of an output, for delivery handoff."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT e.event_json FROM candidate_outputs c "
                "JOIN execution_events e ON e.namespace_id = c.namespace_id AND e.event_id = c.event_id "
                "WHERE c.namespace_id = ? AND c.output_id = ?",
                (namespace_id, output_id),
            ).fetchone()
        return None if row is None else AgentEvent.model_validate_json(row["event_json"])

    def result_deliveries(self, namespace_id: str, request_id: str) -> list[dict[str, object]]:
        """Delivery states for one Request, oldest first; output bodies are not copied here."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT delivery_id, request_id, execution_id, output_id, channel, target_id, status, "
                "idempotency_key, attempt_count, external_message_id, error_code, created_at, updated_at "
                "FROM result_deliveries WHERE namespace_id = ? AND request_id = ? ORDER BY rowid",
                (namespace_id, request_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_pending_result_deliveries(
        self,
        namespace_id: str,
        *,
        limit: int = 100,
        exclude_delivery_ids: Sequence[str] = (),
        now: datetime,
    ) -> list[dict[str, object]]:
        """Atomically claim Feishu deliveries and return their stored route and payload.

        A file delivery also carries the key of its retained copy and the ``file_key`` of
        an earlier upload, so a retry sends the uploaded file again instead of uploading anew.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        exclude_clause = ""
        query_params: list[object] = [namespace_id]
        if exclude_delivery_ids:
            placeholders = ", ".join("?" for _ in exclude_delivery_ids)
            exclude_clause = f"AND d.delivery_id NOT IN ({placeholders}) "
            query_params.extend(exclude_delivery_ids)
        query_params.append(limit)
        jobs: list[dict[str, object]] = []
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT d.*, r.record_json, c.payload_json FROM result_deliveries d "
                "JOIN requests r ON r.namespace_id = d.namespace_id AND r.request_id = d.request_id "
                "JOIN candidate_outputs c ON c.namespace_id = d.namespace_id AND c.output_id = d.output_id "
                "WHERE d.namespace_id = ? AND d.channel = 'feishu' AND d.status = 'pending' "
                + exclude_clause
                + "ORDER BY d.created_at, d.rowid LIMIT ?",
                query_params,
            ).fetchall()
            for row in rows:
                record = RequestRecord.from_dict(json.loads(row["record_json"]))
                route = record.route_snapshot
                if route is None or route.channel != "feishu" or route.app_id is None:
                    conn.execute(
                        "UPDATE result_deliveries SET status = 'failed', error_code = 'ROUTE_MISSING', "
                        "updated_at = ? WHERE namespace_id = ? AND delivery_id = ? AND status = 'pending'",
                        (now.isoformat(), namespace_id, row["delivery_id"]),
                    )
                    continue
                payload = json.loads(row["payload_json"])
                share_mode = payload.get("share_mode")
                text = payload.get("text")
                if not _deliverable(share_mode, payload):
                    conn.execute(
                        "UPDATE result_deliveries SET status = 'failed', error_code = 'OUTPUT_EMPTY', "
                        "updated_at = ? WHERE namespace_id = ? AND delivery_id = ? AND status = 'pending'",
                        (now.isoformat(), namespace_id, row["delivery_id"]),
                    )
                    continue
                changed = conn.execute(
                    "UPDATE result_deliveries SET status = 'sending', attempt_count = attempt_count + 1, "
                    "updated_at = ? WHERE namespace_id = ? AND delivery_id = ? AND status = 'pending'",
                    (now.isoformat(), namespace_id, row["delivery_id"]),
                ).rowcount
                if changed != 1:
                    continue
                job: dict[str, object] = {
                    "delivery_id": row["delivery_id"],
                    "namespace_id": record.namespace_id,
                    "agent_id": record.agent_id,
                    "session_id": record.session_id,
                    "request_kind": record.request_input.kind,
                    "request_id": row["request_id"],
                    "execution_id": row["execution_id"],
                    "output_id": row["output_id"],
                    "channel": row["channel"],
                    "target_id": route.target_id,
                    "app_id": route.app_id,
                    "source_message_id": (
                        None if record.request_input.kind == "direct_message" else route.source_message_id
                    ),
                    "source_reply_to": route.source_reply_to,
                    "is_direct_message": record.request_input.kind == "direct_message",
                    "text": text,
                    "idempotency_key": row["idempotency_key"],
                    "attempt_count": row["attempt_count"] + 1,
                }
                if share_mode in FILE_SHARE_MODES:
                    try:
                        job["file"] = {
                            "key": output_file_key(namespace_id, payload["file_ref"], payload["file_path"]),
                            "name": payload["file_path"].rsplit("/", 1)[-1],
                            "size": payload.get("file_size"),
                            "file_ref": payload["file_ref"],
                            "file_key": row["file_key"],
                        }
                    except PersistentAgentError:
                        conn.execute(
                            "UPDATE result_deliveries SET status = 'failed', error_code = 'OUTPUT_FILE_INVALID', "
                            "updated_at = ? WHERE namespace_id = ? AND delivery_id = ?",
                            (now.isoformat(), namespace_id, row["delivery_id"]),
                        )
                        continue
                jobs.append(job)
        return jobs

    def update_result_delivery(
        self,
        namespace_id: str,
        delivery_id: str,
        *,
        outcome: str,
        external_message_id: str | None,
        error_code: str | None,
        now: datetime,
        file_key: str | None = None,
    ) -> dict[str, object]:
        """Persist a Gateway ACK; a late ACK may resolve an ``unknown`` delivery.

        A ``file_key`` reported by the upload stage is kept whatever the send outcome,
        and is never replaced by an empty one.
        """
        if outcome not in {"pending", "delivered", "failed", "unknown"}:
            raise ValueError("invalid delivery outcome")
        if outcome == "delivered" and (not isinstance(external_message_id, str) or not external_message_id.strip()):
            raise ValueError("delivered outcome requires external_message_id")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM result_deliveries WHERE namespace_id = ? AND delivery_id = ?",
                (namespace_id, delivery_id),
            ).fetchone()
            if row is None:
                raise PersistentAgentError("result delivery not found", code="NOT_FOUND")
            if row["status"] in {"delivered", "failed"}:
                if (
                    row["status"] == outcome
                    and row["external_message_id"] == external_message_id
                    and row["error_code"] == error_code
                ):
                    return dict(row)
                raise PersistentAgentError("result delivery already has a terminal outcome", code="CONFLICT")
            if (
                row["status"] == "pending"
                and outcome == "pending"
                and row["external_message_id"] == external_message_id
                and row["error_code"] == error_code
            ):
                return dict(row)
            if row["status"] not in {"sending", "unknown"}:
                raise PersistentAgentError(f"result delivery is {row['status']}", code="CONFLICT")
            if row["status"] == "unknown" and outcome == "pending":
                raise PersistentAgentError(
                    "unknown result delivery cannot be retried without reconciliation",
                    code="CONFLICT",
                )
            if not isinstance(file_key, str) or not file_key.strip():
                file_key = None
            conn.execute(
                "UPDATE result_deliveries SET status = ?, external_message_id = ?, error_code = ?, updated_at = ?, "
                "file_key = COALESCE(?, file_key) WHERE namespace_id = ? AND delivery_id = ?",
                (outcome, external_message_id, error_code, now.isoformat(), file_key, namespace_id, delivery_id),
            )
            updated = conn.execute(
                "SELECT * FROM result_deliveries WHERE namespace_id = ? AND delivery_id = ?",
                (namespace_id, delivery_id),
            ).fetchone()
        return dict(updated)

    def pending_delivery_archives(self, namespace_id: str, *, limit: int = 100,
                                  after: tuple[str, str] | None = None) -> list[dict[str, object]]:
        """Confirmed platform sends not yet saved as public records; never eligible for sending again."""
        position = "AND (d.updated_at,d.delivery_id)>(?,?) " if after is not None else ""
        params = (namespace_id, *after, limit) if after is not None else (namespace_id, limit)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT d.*, r.record_json, c.payload_json FROM result_deliveries d "
                "JOIN requests r ON r.namespace_id=d.namespace_id AND r.request_id=d.request_id "
                "JOIN candidate_outputs c ON c.namespace_id=d.namespace_id AND c.output_id=d.output_id "
                "LEFT JOIN group_delivery_archives a ON a.namespace_id=d.namespace_id AND a.delivery_id=d.delivery_id "
                "WHERE d.namespace_id=? AND d.status='delivered' AND d.channel='feishu' "
                "AND a.delivery_id IS NULL " + position + "ORDER BY d.updated_at,d.delivery_id LIMIT ?", params,
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivery_archived(self, namespace_id: str, delivery_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO group_delivery_archives VALUES (?,?)", (namespace_id, delivery_id))

    def recover_sending_result_deliveries(self, namespace_id: str, *, now: datetime) -> int:
        """Fence deliveries interrupted after dispatch; never blindly resend an unknown outcome."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE result_deliveries SET status = 'unknown', error_code = 'ACK_UNKNOWN', updated_at = ? "
                "WHERE namespace_id = ? AND channel = 'feishu' AND status = 'sending'",
                (now.isoformat(), namespace_id),
            )
            return cursor.rowcount

    def pending_requests(self, namespace_id: str) -> list[RequestState]:
        """Queued and dispatched Requests, grouped by Session in arrival order, for restart recovery."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE namespace_id = ? AND status IN ('queued', 'dispatched') "
                "ORDER BY session_id, seq",
                (namespace_id,),
            ).fetchall()
        return [self._request(row) for row in rows]
