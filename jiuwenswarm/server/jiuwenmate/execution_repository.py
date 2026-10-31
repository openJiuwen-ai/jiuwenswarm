"""Sessions, accepted Requests, their Executions, event logs and candidate outputs.

A Session is found by the full ``(namespace, agent, conversation kind, channel,
key)`` tuple, each part in its own column, so two conversations never share a
Session because of joined or truncated strings. Requests are written before
dispatch and survive restarts; a supplementary message is always a new Request.
Events are stored once per ``event_id``; a candidate output keeps the ``output_id``
of the event that first proposed it, and a file candidate is verified in the
originating Session's ``sessions/<session_id>/output/`` directory before it is stored.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jiuwenswarm.server.jiuwenmate.execution_models import (
    ENDED_OUTCOMES,
    AccessSnapshot,
    PermissionOrigin,
    ConversationRef,
    ExecutionRecord,
    PersistentContractError,
    RequestRecord,
    RequestRouteSnapshot,
    RequestState,
    SessionRecord,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.failure_notices import failure_notice_text
from jiuwenswarm.server.jiuwenmate.output_files import FILE_SHARE_MODES, shared_output_file_name, workspace_output_key
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore
from openjiuwen.harness.persistent_agent import AgentEvent, EventType, RequestInput


def _notice_delivery_job(job: Mapping[str, object], notice_text: str) -> dict[str, object]:
    """Keep receipt/routing metadata only when replacing a public payload."""
    fields = (
        "delivery_id", "namespace_id", "agent_id", "session_id", "request_kind",
        "request_id", "execution_id", "output_id", "channel", "target_id", "app_id",
        "source_message_id", "source_reply_to", "is_direct_message", "idempotency_key",
        "attempt_count", "operation", "external_message_id",
    )
    return {**{key: job[key] for key in fields if key in job}, "text": notice_text}


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
CREATE TABLE IF NOT EXISTS session_artifacts (
    namespace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_group_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    output_id TEXT NOT NULL,
    title TEXT NOT NULL,
    file_name TEXT,
    file_path TEXT,
    file_ref TEXT,
    file_size INTEGER,
    url TEXT,
    snippet TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, artifact_id),
    UNIQUE (namespace_id, session_id, output_id, artifact_kind, url)
);
CREATE INDEX IF NOT EXISTS session_artifacts_scope
    ON session_artifacts (namespace_id, agent_id, source_group_id, created_at);
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
    notice_text TEXT,
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
_DOCUMENT_HOSTS = ("feishu.cn", "larksuite.com", "dingtalk.com")
_DOCUMENT_PATH_MARKERS = ("/docx/", "/wiki/", "/sheets/", "/bitable/", "/alidocs/", "/i/nodes/", "/drive/")
_URL_PATTERN = re.compile(r"https?://[^\s<>\])}]+", re.IGNORECASE)


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


def _failure_of(event: AgentEvent) -> tuple[str, str | None] | None:
    if event.event_type == EventType.EXECUTION_PAUSED and event.payload.reason in {
        "error", "context_unfit", "budget_denied",
    }:
        return event.payload.reason, event.payload.detail
    if event.event_type == EventType.EXECUTION_FINAL and event.payload.status == "failed":
        return "error", event.payload.reason
    return None


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
            if "notice_text" not in columns:
                conn.execute("ALTER TABLE result_deliveries ADD COLUMN notice_text TEXT")
            if "job_json" not in columns:
                conn.execute("ALTER TABLE result_deliveries ADD COLUMN job_json TEXT")
            if "handoff_done" not in columns:
                conn.execute("ALTER TABLE result_deliveries ADD COLUMN handoff_done INTEGER NOT NULL DEFAULT 0")
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

    @staticmethod
    def _handoff_permission_origin(conn, session, request_input, route) -> PermissionOrigin | None:
        """Resolve a human only through a delivered host output naming this receiving Agent."""
        if (request_input.kind != "group_message" or request_input.source.sender_kind != "agent"
                or route is None or route.channel != "feishu"):
            return None
        rows = conn.execute(
            "SELECT r.record_json, o.payload_json FROM result_deliveries d "
            "JOIN requests r ON r.namespace_id=d.namespace_id AND r.request_id=d.request_id "
            "JOIN candidate_outputs o ON o.namespace_id=d.namespace_id AND o.output_id=d.output_id "
            "AND o.request_id=d.request_id AND o.execution_id=d.execution_id "
            "WHERE d.namespace_id=? AND d.channel=? AND d.target_id=? AND d.external_message_id=? "
            "AND d.status='delivered' AND d.notice_text IS NULL AND o.share_mode='message'",
            (session.namespace_id, route.channel, route.target_id, request_input.source.message_id),
        ).fetchall()
        origins = []
        for row in rows:
            parent = RequestRecord.from_dict(json.loads(row["record_json"]))
            parent_route = parent.route_snapshot
            text = json.loads(row["payload_json"]).get("text")
            if (parent.request_input.kind != "group_message" or parent_route is None
                    or (parent_route.channel, parent_route.target_id) != (route.channel, route.target_id)
                    or not isinstance(text, str) or f"[[agent:{session.agent_id}]]" not in text):
                continue
            origin = parent.permission_origin
            if origin is None and parent.request_input.source.sender_kind == "human":
                if not parent.access.principal_ref.startswith("feishu:user:"):
                    continue
                origin = PermissionOrigin(principal_ref=parent.access.principal_ref, agent_id=parent.agent_id,
                                          request_id=parent.request_id, route=parent_route)
            if (origin is not None and origin.principal_ref.startswith("feishu:user:")
                    and (origin.route.channel, origin.route.target_id) == (route.channel, route.target_id)):
                origins.append(origin)
        return origins[0] if len(origins) == 1 else None

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
                permission_origin=self._handoff_permission_origin(conn, session, request_input, route_snapshot),
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

    def pending_continuation_ref(self, namespace_id: str, request_id: str) -> str | None:
        """The interrupted Execution awaiting a continuation that has not started yet."""
        with self._transaction() as conn:
            return self._pending_continuation(conn, namespace_id, request_id)

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
        session = conn.execute(
            "SELECT kind, conversation_key FROM sessions WHERE namespace_id=? AND session_id=? AND agent_id=?",
            (record.namespace_id, record.session_id, record.agent_id),
        ).fetchone()
        source_group_id = (
            session["conversation_key"]
            if event.event_type == EventType.EXECUTION_FINAL and _failure_of(event) is None
            and session is not None and session["kind"] == "group"
            else None
        )
        if route is None:
            failure = _failure_of(event)
            if failure is not None:
                ExecutionRepository._enqueue_failure_notice(
                    conn, record, event.execution_id, reason=failure[0], detail=failure[1], now=now,
                )
            return

        for output_id in event.payload.output_refs:
            output = conn.execute(
                "SELECT share_mode, payload_json FROM candidate_outputs "
                "WHERE namespace_id = ? AND request_id = ? AND output_id = ?",
                (event.namespace_id, event.request_id, output_id),
            ).fetchone()
            if output is None:
                continue
            payload = json.loads(output["payload_json"])
            if not _deliverable(output["share_mode"], payload):
                continue
            if source_group_id is not None:
                ExecutionRepository._index_session_artifacts(
                    conn, record, source_group_id, output_id, output["share_mode"], payload, now,
                )
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

        failure = _failure_of(event)
        if failure is not None:
            ExecutionRepository._enqueue_failure_notice(
                conn, record, event.execution_id, reason=failure[0], detail=failure[1], now=now,
            )

    @staticmethod
    def _index_session_artifacts(
        conn: sqlite3.Connection,
        record: RequestRecord,
        source_group_id: str,
        output_id: str,
        share_mode: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        """Index only final file outputs and Feishu/DingTalk document links from group Sessions."""
        base = (record.namespace_id, record.agent_id, record.session_id, source_group_id)
        if share_mode in FILE_SHARE_MODES:
            file_path, file_ref, file_size = payload.get("file_path"), payload.get("file_ref"), payload.get("file_size")
            if (not isinstance(file_path, str) or not isinstance(file_ref, str)
                    or isinstance(file_size, bool) or not isinstance(file_size, int)):
                return
            file_name = file_path.rsplit("/", 1)[-1]
            snippet = payload.get("text")
            if not isinstance(snippet, str):
                snippet = None
            elif len(snippet) > 4096:
                snippet = snippet[:4096]
            artifact_id = "art_" + hashlib.sha256(
                f"{record.namespace_id}\0{record.agent_id}\0{record.session_id}\0{output_id}\0file".encode()
            ).hexdigest()[:40]
            conn.execute(
                "INSERT OR IGNORE INTO session_artifacts (namespace_id, agent_id, session_id, source_group_id, "
                "artifact_id, artifact_kind, output_id, title, file_name, file_path, file_ref, file_size, "
                "url, snippet, created_at) VALUES (?,?,?,?,?,'file',?,?,?,?,?,?,NULL,?,?)",
                (*base, artifact_id, output_id, file_name, file_name, file_path, file_ref, file_size,
                 snippet, now.isoformat()),
            )
            return

        text = payload.get("text")
        if share_mode != "message" or not isinstance(text, str):
            return
        for raw_url in _URL_PATTERN.findall(text):
            url = raw_url.rstrip(".,;:!?\"'`)")
            if len(url) > 4096:
                continue
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower().rstrip(".")
                port = parsed.port
            except ValueError:
                continue
            if (parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None
                    or port is not None or not any(host == domain or host.endswith("." + domain)
                                                             for domain in _DOCUMENT_HOSTS)
                    or not any(marker in parsed.path.lower() for marker in _DOCUMENT_PATH_MARKERS)):
                continue
            artifact_id = "art_" + hashlib.sha256(
                f"{record.namespace_id}\0{record.agent_id}\0{record.session_id}\0{output_id}\0{url}".encode()
            ).hexdigest()[:40]
            prefix = text.split(raw_url, 1)[0].strip()
            title = (prefix.rsplit("\n", 1)[-1] or "在线文档")[:512]
            conn.execute(
                "INSERT OR IGNORE INTO session_artifacts (namespace_id, agent_id, session_id, source_group_id, "
                "artifact_id, artifact_kind, output_id, title, file_name, file_path, file_ref, file_size, "
                "url, snippet, created_at) VALUES (?,?,?,?,?,'online_doc',?,?,NULL,NULL,NULL,NULL,?,?,?)",
                (*base, artifact_id, output_id, title, url, text[:4096], now.isoformat()),
            )

    @staticmethod
    def _enqueue_failure_notice(
        conn: sqlite3.Connection, record: RequestRecord, execution_id: str, *,
        reason: str, detail: str | None, now: datetime,
    ) -> None:
        route = record.route_snapshot
        # Missing external routing must not hide local status or invent a Feishu destination.
        channel = route.channel if route is not None else "web"
        target_id = route.target_id if route is not None else record.session_id
        # Host status belongs to the outbox, not to agent candidate outputs.
        conn.execute(
            "INSERT OR IGNORE INTO result_deliveries (namespace_id, delivery_id, request_id, execution_id, "
            "output_id, channel, target_id, status, idempotency_key, created_at, updated_at, notice_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.namespace_id, f"del_failure_{execution_id}", record.request_id, execution_id,
                f"notice_{uuid.uuid4().hex}", channel, target_id,
                "available" if channel == "web" else "pending", str(uuid.uuid4()),
                now.isoformat(), now.isoformat(),
                failure_notice_text(record.request_id, reason=reason, detail=detail),
            ),
        )

    def record_execution_failure(
        self, namespace_id: str, request_id: str, execution_id: str, *,
        reason: str, detail: str | None, now: datetime,
    ) -> None:
        """Atomically close a host-failed request and save its notice, without fabricating a core event."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT r.record_json FROM requests r JOIN executions e "
                "ON e.namespace_id=r.namespace_id AND e.request_id=r.request_id "
                "WHERE r.namespace_id=? AND r.request_id=? AND e.execution_id=?",
                (namespace_id, request_id, execution_id),
            ).fetchone()
            if row is None:
                raise PersistentAgentError("failure does not belong to a stored Execution", code="NOT_FOUND")
            self._enqueue_failure_notice(
                conn, RequestRecord.from_dict(json.loads(row[0])), execution_id,
                reason=reason, detail=detail, now=now,
            )
            # Recovery must not resume a known host failure between notice persistence
            # and the scheduler's (idempotent) finish_request call.
            conn.execute(
                "UPDATE requests SET status = 'ended', outcome = 'failed', updated_at = ? "
                "WHERE namespace_id = ? AND request_id = ? AND status = 'dispatched'",
                (now.isoformat(), namespace_id, request_id),
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

    def search_session_artifacts(
        self, namespace_id: str, agent_id: str, group_ids: Sequence[str], *, limit: int = 10_000,
    ) -> tuple[dict[str, object], ...]:
        """List only final artifacts indexed from group Sessions in the requested Agent scope."""
        if not group_ids:
            return ()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("artifact search limit is invalid")
        for group_id in group_ids:
            require_identifier("source_group_id", group_id)
        placeholders = ",".join("?" for _ in group_ids)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM session_artifacts WHERE namespace_id=? AND agent_id=? "
                f"AND source_group_id IN ({placeholders}) ORDER BY created_at DESC, artifact_id LIMIT ?",
                (namespace_id, agent_id, *group_ids, limit),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def session_artifact_candidates(self, namespace_id: str, agent_id: str) -> tuple[dict, ...]:
        """Registered group files for host-only Session-aware tool preflight."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM session_artifacts WHERE namespace_id=? AND agent_id=? AND artifact_kind='file' "
                "ORDER BY created_at, artifact_id LIMIT 10001", (namespace_id, agent_id),
            ).fetchall()
        if len(rows) > 10_000:
            raise PersistentAgentError("artifact provenance index is too large", code="ARTIFACT_SEARCH_UNAVAILABLE")
        return tuple(dict(row) for row in rows)

    def result_deliveries(self, namespace_id: str, request_id: str) -> list[dict[str, object]]:
        """Delivery states for one Request, oldest first; output bodies are not copied here."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT delivery_id, request_id, execution_id, output_id, channel, target_id, status, "
                "idempotency_key, attempt_count, external_message_id, error_code, created_at, updated_at, notice_text "
                "FROM result_deliveries WHERE namespace_id = ? AND request_id = ? ORDER BY rowid",
                (namespace_id, request_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_pending_result_deliveries(
        self,
        namespace_id: str,
        *,
        channels: frozenset[str] = frozenset({"feishu"}),
        limit: int = 100,
        exclude_delivery_ids: Sequence[str] = (),
        now: datetime,
    ) -> list[dict[str, object]]:
        """Atomically claim IM deliveries and return their stored route and payload.

        A file delivery also carries a logical key for the originating Session's ``output/`` file
        and the ``file_key`` of an earlier upload, so a retry sends the uploaded file again
        instead of uploading anew.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not channels:
            return []
        if not channels <= {"feishu", "dingtalk", "welink"}:
            raise ValueError("unsupported IM delivery channel")
        channel_placeholders = ", ".join("?" for _ in channels)
        exclude_clause = ""
        query_params: list[object] = [namespace_id, *sorted(channels)]
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
                "LEFT JOIN candidate_outputs c ON c.namespace_id = d.namespace_id AND c.output_id = d.output_id "
                "AND c.request_id = d.request_id AND d.notice_text IS NULL "
                f"WHERE d.namespace_id = ? AND d.channel IN ({channel_placeholders}) AND d.status = 'pending' "
                + exclude_clause
                + "ORDER BY d.created_at, d.rowid LIMIT ?",
                query_params,
            ).fetchall()
            for row in rows:
                record = RequestRecord.from_dict(json.loads(row["record_json"]))
                route = record.route_snapshot
                if route is None or route.channel != row["channel"] or route.app_id is None:
                    conn.execute(
                        "UPDATE result_deliveries SET status = 'failed', error_code = 'ROUTE_MISSING', "
                        "updated_at = ? WHERE namespace_id = ? AND delivery_id = ? AND status = 'pending'",
                        (now.isoformat(), namespace_id, row["delivery_id"]),
                    )
                    continue
                payload = (
                    json.loads(row["payload_json"]) if row["payload_json"] is not None
                    else {"share_mode": "message", "text": row["notice_text"]}
                )
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
                if row["channel"] == "dingtalk" and record.request_input.kind == "group_message":
                    source = record.request_input.source
                    sender_staff_id = getattr(source, "sender_id", None)
                    if isinstance(sender_staff_id, str) and sender_staff_id.strip():
                        job["sender_staff_id"] = sender_staff_id
                if share_mode in FILE_SHARE_MODES:
                    try:
                        file_path = payload["file_path"]
                        file_ref = payload["file_ref"]
                        file_size = payload.get("file_size")
                        job["file"] = {
                            "key": workspace_output_key(
                                namespace_id, record.agent_id, file_path, file_ref, file_size
                            ),
                            "name": shared_output_file_name(file_path, file_ref, file_size),
                            "size": file_size,
                            "file_ref": file_ref,
                            "file_key": row["file_key"],
                            "share_mode": share_mode,
                        }
                    except PersistentAgentError:
                        conn.execute(
                            "UPDATE result_deliveries SET status = 'failed', error_code = 'OUTPUT_FILE_INVALID', "
                            "updated_at = ? WHERE namespace_id = ? AND delivery_id = ?",
                            (now.isoformat(), namespace_id, row["delivery_id"]),
                        )
                        continue
                if row["job_json"] and row["notice_text"] is None:
                    job.update(json.loads(row["job_json"]))
                if row["notice_text"] is not None:
                    job = _notice_delivery_job(job, row["notice_text"])
                jobs.append(job)
        return jobs

    def unknown_welink_result_deliveries(
        self, namespace_id: str, *, limit: int = 20, after: str = "",
    ) -> list[dict[str, object]]:
        """Read unresolved routes for observation only; never claim them for sending."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT d.*, r.record_json FROM result_deliveries d "
                "JOIN requests r ON r.namespace_id=d.namespace_id AND r.request_id=d.request_id "
                "WHERE d.namespace_id=? AND d.channel='welink' AND d.status='unknown' "
                "AND d.delivery_id>? ORDER BY d.delivery_id LIMIT ?", (namespace_id, after, limit),
            ).fetchall()
        jobs = []
        for row in rows:
            record = RequestRecord.from_dict(json.loads(row["record_json"]))
            route = record.route_snapshot
            if route is None or route.channel != "welink":
                continue
            job = json.loads(row["job_json"]) if row["job_json"] else {}
            job.update(delivery_id=row["delivery_id"], namespace_id=namespace_id,
                       agent_id=record.agent_id, session_id=record.session_id,
                       request_id=row["request_id"], execution_id=row["execution_id"],
                       output_id=row["output_id"], channel="welink", operation="reconcile",
                       app_id=route.app_id, target_id=route.target_id, idempotency_key=row["idempotency_key"])
            if row["notice_text"] is not None:
                job = _notice_delivery_job(job, row["notice_text"])
            jobs.append(job)
        return jobs

    def save_result_delivery_job(self, namespace_id: str, job: Mapping[str, object]) -> None:
        """Save the resolved public payload before any platform side effect."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT notice_text FROM result_deliveries WHERE namespace_id=? AND delivery_id=?",
                (namespace_id, job["delivery_id"]),
            ).fetchone()
            if row is not None and row["notice_text"] is not None:
                job = _notice_delivery_job(job, row["notice_text"])
            conn.execute(
                "UPDATE result_deliveries SET job_json=?, handoff_done=? "
                "WHERE namespace_id=? AND delivery_id=? AND status='sending'",
                (json.dumps(dict(job), ensure_ascii=False), int(not job.get("mention_targets")),
                 namespace_id, job["delivery_id"]),
            )

    def pending_welink_handoffs(
        self, namespace_id: str, *, after: str = "", limit: int = 20,
    ) -> list[dict[str, object]]:
        """Delivered work remains recoverable until the target request is accepted."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT job_json, external_message_id FROM result_deliveries WHERE namespace_id=? "
                "AND channel='welink' AND status='delivered' AND handoff_done=0 AND job_json IS NOT NULL "
                "AND notice_text IS NULL "
                "AND delivery_id>? ORDER BY delivery_id LIMIT ?",
                (namespace_id, after, limit),
            ).fetchall()
        jobs = []
        for row in rows:
            job = json.loads(row["job_json"])
            if job.get("mention_targets"):
                job["external_message_id"] = row["external_message_id"]
                jobs.append(job)
        return jobs

    def mark_welink_handoff_done(self, namespace_id: str, delivery_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE result_deliveries SET handoff_done=1 WHERE namespace_id=? AND delivery_id=? "
                         "AND status='delivered'", (namespace_id, delivery_id))

    def replace_result_delivery_with_notice(
        self,
        namespace_id: str,
        delivery_id: str,
        *,
        notice_text: str,
        now: datetime,
    ) -> None:
        """Replace public content atomically, keeping candidates and receipt facts.

        Pending sends, unknown receipts and delayed archives/handoffs all use the
        persisted notice. Replacement never makes an unknown row retryable.
        """
        if not isinstance(notice_text, str) or not notice_text.strip():
            raise ValueError("replacement notice must be nonblank text")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM result_deliveries WHERE namespace_id=? AND delivery_id=?",
                (namespace_id, delivery_id),
            ).fetchone()
            if row is None:
                raise PersistentAgentError("result delivery not found", code="NOT_FOUND")
            if row["status"] not in {"pending", "sending", "unknown", "delivered"}:
                raise PersistentAgentError("result delivery cannot be replaced", code="CONFLICT")
            snapshot = (
                json.dumps(_notice_delivery_job(json.loads(row["job_json"]), notice_text), ensure_ascii=False)
                if row["job_json"] else None
            )
            conn.execute(
                "UPDATE result_deliveries SET notice_text=?, file_key=NULL, job_json=?, handoff_done=1, "
                "updated_at=? WHERE namespace_id=? AND delivery_id=?",
                (notice_text, snapshot, now.isoformat(), namespace_id, delivery_id),
            )

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

        Upload keys survive retries unless replaced by a notice. Late upload ACKs
        cannot restore a redacted file.
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
                "file_key = CASE WHEN notice_text IS NULL THEN COALESCE(?, file_key) ELSE NULL END "
                "WHERE namespace_id = ? AND delivery_id = ?",
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
                "LEFT JOIN candidate_outputs c ON c.namespace_id=d.namespace_id AND c.output_id=d.output_id "
                "AND c.request_id=d.request_id AND d.notice_text IS NULL "
                "LEFT JOIN group_delivery_archives a ON a.namespace_id=d.namespace_id AND a.delivery_id=d.delivery_id "
                "WHERE d.namespace_id=? AND d.status='delivered' AND d.channel IN ('feishu','dingtalk') "
                "AND a.delivery_id IS NULL " + position + "ORDER BY d.updated_at,d.delivery_id LIMIT ?", params,
            ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            if job["payload_json"] is None and job["notice_text"] is not None:
                job["payload_json"] = json.dumps({"share_mode": "message", "text": job["notice_text"]})
        return jobs

    def mark_delivery_archived(self, namespace_id: str, delivery_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO group_delivery_archives VALUES (?,?)", (namespace_id, delivery_id))

    def recover_sending_result_deliveries(
        self, namespace_id: str, *, now: datetime, channels: frozenset[str] = frozenset({"feishu"}),
    ) -> int:
        """Fence only this runtime's interrupted sends; never blindly resend unknown outcomes."""
        if not channels:
            return 0
        if not channels <= {"feishu", "dingtalk", "welink"}:
            raise ValueError("unsupported IM delivery channel")
        placeholders = ", ".join("?" for _ in channels)
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE result_deliveries SET status = 'unknown', error_code = 'ACK_UNKNOWN', updated_at = ? "
                f"WHERE namespace_id = ? AND channel IN ({placeholders}) AND status = 'sending'",
                (now.isoformat(), namespace_id, *sorted(channels)),
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
