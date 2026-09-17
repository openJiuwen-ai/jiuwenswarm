# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable mailbox for Agent-to-Agent product Session messages."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


ACTIVE_SESSION_MESSAGE_STATUSES = frozenset(
    {"queued", "running", "waiting_user", "unknown"}
)
TERMINAL_SESSION_MESSAGE_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class SessionMessageStoreError(RuntimeError):
    """Base error raised by the mailbox store."""


class SessionMessageLimitExceeded(SessionMessageStoreError):
    """Mailbox or chain capacity was exceeded."""


class SessionMessageIdempotencyConflict(SessionMessageStoreError):
    """One idempotency key was reused with different arguments."""


@dataclass(frozen=True, slots=True)
class SessionMessageRecord:
    """One durable cross-Session message."""

    sequence: int
    message_id: str
    owner_scope_id: str
    source_session_id: str
    source_title_snapshot: str
    source_request_id: str
    source_tool_call_id: str
    target_session_id: str
    content: str
    chain_id: str
    parent_message_id: str
    hop_count: int
    status: str
    execution_request_id: str
    runtime_run_id: str
    interrupt_request_id: str
    interrupt_source: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    updated_at: float
    last_error_code: str
    last_error: str
    resolved_at: float | None
    resolution: str
    retry_of: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionMessageStore:
    """SQLite-backed Session mailbox with transactional claim and dedup."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _session(self) -> closing[sqlite3.Connection]:
        """Open a connection that commits on success and always closes.

        ``with conn:`` alone only manages the transaction — the connection
        itself stays open until GC. Nesting it inside ``closing`` keeps the
        existing commit/rollback semantics while releasing the fd eagerly.
        """

        return closing(self._connect())

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._session() as conn, conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS session_messages (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT NOT NULL UNIQUE,
                        owner_scope_id TEXT NOT NULL,
                        source_session_id TEXT NOT NULL,
                        source_title_snapshot TEXT NOT NULL DEFAULT '',
                        source_request_id TEXT NOT NULL,
                        source_tool_call_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        target_session_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        chain_id TEXT NOT NULL,
                        parent_message_id TEXT NOT NULL DEFAULT '',
                        hop_count INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        execution_request_id TEXT NOT NULL DEFAULT '',
                        runtime_run_id TEXT NOT NULL DEFAULT '',
                        interrupt_request_id TEXT NOT NULL DEFAULT '',
                        interrupt_source TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        updated_at REAL NOT NULL,
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT '',
                        resolved_at REAL,
                        resolution TEXT NOT NULL DEFAULT '',
                        retry_of TEXT NOT NULL DEFAULT '',
                        CHECK (hop_count >= 1)
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_messages_target_queue
                        ON session_messages(target_session_id, status, sequence);
                    CREATE INDEX IF NOT EXISTS idx_session_messages_owner_created
                        ON session_messages(owner_scope_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_session_messages_chain
                        ON session_messages(chain_id, sequence);
                    """
                )
                columns = set()
                for row in conn.execute(
                    "PRAGMA table_info(session_messages)"
                ).fetchall():
                    columns.add(str(row[1]))
                for name in ("interrupt_request_id", "interrupt_source"):
                    if name not in columns:
                        conn.execute(
                            f"ALTER TABLE session_messages ADD COLUMN {name} "
                            "TEXT NOT NULL DEFAULT ''"
                        )
            self._schema_ready = True

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> SessionMessageRecord | None:
        if row is None:
            return None
        return SessionMessageRecord(
            sequence=int(row["sequence"]),
            message_id=str(row["message_id"]),
            owner_scope_id=str(row["owner_scope_id"]),
            source_session_id=str(row["source_session_id"]),
            source_title_snapshot=str(row["source_title_snapshot"]),
            source_request_id=str(row["source_request_id"]),
            source_tool_call_id=str(row["source_tool_call_id"]),
            target_session_id=str(row["target_session_id"]),
            content=str(row["content"]),
            chain_id=str(row["chain_id"]),
            parent_message_id=str(row["parent_message_id"]),
            hop_count=int(row["hop_count"]),
            status=str(row["status"]),
            execution_request_id=str(row["execution_request_id"]),
            runtime_run_id=str(row["runtime_run_id"]),
            interrupt_request_id=str(row["interrupt_request_id"]),
            interrupt_source=str(row["interrupt_source"]),
            created_at=float(row["created_at"]),
            started_at=(
                float(row["started_at"]) if row["started_at"] is not None else None
            ),
            finished_at=(
                float(row["finished_at"]) if row["finished_at"] is not None else None
            ),
            updated_at=float(row["updated_at"]),
            last_error_code=str(row["last_error_code"]),
            last_error=str(row["last_error"]),
            resolved_at=(
                float(row["resolved_at"]) if row["resolved_at"] is not None else None
            ),
            resolution=str(row["resolution"]),
            retry_of=str(row["retry_of"]),
        )

    @classmethod
    def _rows_to_records(
        cls, rows: Iterable[sqlite3.Row]
    ) -> list[SessionMessageRecord]:
        records: list[SessionMessageRecord] = []
        for row in rows:
            record = cls._row_to_record(row)
            if record is not None:
                records.append(record)
        return records

    def enqueue(
        self,
        *,
        owner_scope_id: str,
        source_session_id: str,
        source_title_snapshot: str,
        source_request_id: str,
        source_tool_call_id: str,
        idempotency_key: str,
        target_session_id: str,
        content: str,
        chain_id: str = "",
        parent_message_id: str = "",
        hop_count: int = 1,
        retry_of: str = "",
        max_pending_per_target: int = 100,
        max_messages_per_chain: int = 16,
    ) -> tuple[SessionMessageRecord, bool]:
        """Commit a message, returning ``(record, created)``."""

        self.ensure_schema()
        now = time.time()
        message_id = f"sm_{uuid.uuid4().hex}"
        effective_chain_id = chain_id or message_id
        with self._session() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_row = conn.execute(
                "SELECT * FROM session_messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_record(existing_row)
                if existing is None:
                    raise SessionMessageStoreError(
                        "idempotent message row could not be decoded"
                    )
                same_request = (
                    existing.owner_scope_id == owner_scope_id
                    and existing.source_session_id == source_session_id
                )
                same_delivery = (
                    existing.target_session_id == target_session_id
                    and existing.content == content
                )
                if not (same_request and same_delivery):
                    raise SessionMessageIdempotencyConflict(
                        "idempotency key was reused with different arguments"
                    )
                return existing, False

            placeholders = ",".join("?" for _ in ACTIVE_SESSION_MESSAGE_STATUSES)
            pending = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM session_messages
                    WHERE target_session_id = ?
                      AND status IN ({placeholders})
                      AND (status != 'unknown' OR resolved_at IS NULL)
                    """,
                    (target_session_id, *sorted(ACTIVE_SESSION_MESSAGE_STATUSES)),
                ).fetchone()[0]
            )
            if pending >= max_pending_per_target:
                raise SessionMessageLimitExceeded("target mailbox is full")

            chain_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE chain_id = ?",
                    (effective_chain_id,),
                ).fetchone()[0]
            )
            if chain_count >= max_messages_per_chain:
                raise SessionMessageLimitExceeded("message chain limit exceeded")

            conn.execute(
                """
                INSERT INTO session_messages (
                    message_id, owner_scope_id, source_session_id,
                    source_title_snapshot, source_request_id,
                    source_tool_call_id, idempotency_key, target_session_id,
                    content, chain_id, parent_message_id, hop_count, status,
                    created_at, updated_at, retry_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    message_id,
                    owner_scope_id,
                    source_session_id,
                    source_title_snapshot,
                    source_request_id,
                    source_tool_call_id,
                    idempotency_key,
                    target_session_id,
                    content,
                    effective_chain_id,
                    parent_message_id,
                    hop_count,
                    now,
                    now,
                    retry_of,
                ),
            )
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        record = self._row_to_record(row)
        if record is None:
            raise SessionMessageStoreError(
                "committed message row could not be decoded"
            )
        return record, True

    def get(self, message_id: str) -> SessionMessageRecord | None:
        self.ensure_schema()
        with self._session() as conn, conn:
            return self._row_to_record(
                conn.execute(
                    "SELECT * FROM session_messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
            )

    def queued_targets(self) -> list[str]:
        self.ensure_schema()
        with self._session() as conn, conn:
            rows = conn.execute(
                """
                SELECT DISTINCT target_session_id FROM session_messages
                WHERE status = 'queued' ORDER BY target_session_id
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def next_queued(self, target_session_id: str) -> SessionMessageRecord | None:
        """Return the FIFO head unless an unresolved unknown blocks the target."""

        self.ensure_schema()
        with self._session() as conn, conn:
            blocker = conn.execute(
                """
                SELECT 1 FROM session_messages
                WHERE target_session_id = ?
                  AND (
                    status IN ('running', 'waiting_user')
                    OR (status = 'unknown' AND resolved_at IS NULL)
                  )
                LIMIT 1
                """,
                (target_session_id,),
            ).fetchone()
            if blocker is not None:
                return None
            row = conn.execute(
                """
                SELECT * FROM session_messages
                WHERE target_session_id = ? AND status = 'queued'
                ORDER BY sequence LIMIT 1
                """,
                (target_session_id,),
            ).fetchone()
        return self._row_to_record(row)

    def waiting_for_target(self, target_session_id: str) -> SessionMessageRecord | None:
        self.ensure_schema()
        with self._session() as conn, conn:
            row = conn.execute(
                """
                SELECT * FROM session_messages
                WHERE target_session_id = ? AND status = 'waiting_user'
                ORDER BY sequence LIMIT 1
                """,
                (target_session_id,),
            ).fetchone()
        return self._row_to_record(row)

    def waiting_for_resume(
        self,
        target_session_id: str,
        interrupt_request_id: str,
        interrupt_source: str,
    ) -> SessionMessageRecord | None:
        """Return the exact waiting item addressed by an interrupt answer."""

        self.ensure_schema()
        with self._session() as conn, conn:
            row = conn.execute(
                """
                SELECT * FROM session_messages
                WHERE target_session_id = ? AND status = 'waiting_user'
                  AND interrupt_request_id = ? AND interrupt_source = ?
                ORDER BY sequence LIMIT 1
                """,
                (target_session_id, interrupt_request_id, interrupt_source),
            ).fetchone()
        return self._row_to_record(row)

    def supersede_waiting_for_target(
        self,
        target_session_id: str,
    ) -> list[SessionMessageRecord]:
        """Fail a target's waiting items whose question the user bypassed.

        A plain (non-resume) user turn on the target means the pending
        cross-session question was abandoned. Such items can never be answered
        through their saved correlation, so move them to ``failed`` to unblock
        the FIFO. Only ``waiting_user`` rows are touched; queued successors
        keep their order.
        """

        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            rows = conn.execute(
                """
                SELECT message_id FROM session_messages
                WHERE target_session_id = ? AND status = 'waiting_user'
                ORDER BY sequence
                """,
                (target_session_id,),
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            if not message_ids:
                return []
            placeholders = ",".join("?" for _ in message_ids)
            conn.execute(
                f"""
                UPDATE session_messages
                SET status = 'failed', finished_at = ?, updated_at = ?,
                    last_error_code = 'SUPERSEDED_BY_USER_TURN',
                    last_error = ?
                WHERE message_id IN ({placeholders}) AND status = 'waiting_user'
                """,
                (
                    now,
                    now,
                    "target user started a new turn before answering "
                    "the cross-session question",
                    *message_ids,
                ),
            )
            updated_rows = conn.execute(
                f"""
                SELECT * FROM session_messages
                WHERE message_id IN ({placeholders}) AND status = 'failed'
                ORDER BY sequence
                """,
                tuple(message_ids),
            ).fetchall()
        return self._rows_to_records(updated_rows)

    def claim(
        self,
        message_id: str,
        execution_request_id: str,
        runtime_run_id: str,
    ) -> SessionMessageRecord | None:
        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            cursor = conn.execute(
                """
                UPDATE session_messages
                SET status = 'running', execution_request_id = ?,
                    runtime_run_id = ?, started_at = ?, updated_at = ?
                WHERE message_id = ? AND status = 'queued'
                """,
                (execution_request_id, runtime_run_id, now, now, message_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_record(row)

    def transition_status(
        self,
        message_id: str,
        status: str,
        *,
        expected_statuses: tuple[str, ...],
        error_code: str = "",
        error: str = "",
    ) -> SessionMessageRecord | None:
        """Atomically update a record only from one of ``expected_statuses``."""

        allowed = ACTIVE_SESSION_MESSAGE_STATUSES | TERMINAL_SESSION_MESSAGE_STATUSES
        if status not in allowed:
            raise ValueError(f"unsupported session message status: {status}")
        if not expected_statuses or any(item not in allowed for item in expected_statuses):
            raise ValueError("expected_statuses must contain supported statuses")
        self.ensure_schema()
        now = time.time()
        finished_at = (
            now if status in TERMINAL_SESSION_MESSAGE_STATUSES | {"unknown"} else None
        )
        placeholders = ",".join("?" for _ in expected_statuses)
        with self._session() as conn, conn:
            cursor = conn.execute(
                f"""
                UPDATE session_messages
                SET status = ?, finished_at = ?, updated_at = ?,
                    last_error_code = ?, last_error = ?
                WHERE message_id = ? AND status IN ({placeholders})
                """,
                (
                    status,
                    finished_at,
                    now,
                    error_code,
                    error,
                    message_id,
                    *expected_statuses,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_record(row)

    def mark_waiting(
        self,
        message_id: str,
        *,
        interrupt_request_id: str,
        interrupt_source: str,
    ) -> SessionMessageRecord | None:
        """Persist the exact HITL identity before exposing it to the client."""

        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            cursor = conn.execute(
                """
                UPDATE session_messages
                SET status = 'waiting_user', updated_at = ?,
                    interrupt_request_id = ?, interrupt_source = ?,
                    finished_at = NULL, last_error_code = '', last_error = ''
                WHERE message_id = ? AND status = 'running'
                """,
                (now, interrupt_request_id, interrupt_source, message_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_record(row)

    def replace_waiting_correlation(
        self,
        message_id: str,
        *,
        expected_interrupt_request_id: str,
        expected_interrupt_source: str,
        interrupt_request_id: str,
        interrupt_source: str,
    ) -> SessionMessageRecord | None:
        """Move a resumed item to its next exact HITL question."""

        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            cursor = conn.execute(
                """
                UPDATE session_messages
                SET updated_at = ?, interrupt_request_id = ?, interrupt_source = ?,
                    last_error_code = '', last_error = ''
                WHERE message_id = ? AND status = 'waiting_user'
                  AND interrupt_request_id = ? AND interrupt_source = ?
                """,
                (
                    now,
                    interrupt_request_id,
                    interrupt_source,
                    message_id,
                    expected_interrupt_request_id,
                    expected_interrupt_source,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_record(row)

    def resolve_unknown(
        self,
        message_id: str,
        *,
        owner_scope_id: str,
        participant_session_id: str,
        resolution: str,
    ) -> SessionMessageRecord | None:
        """Resolve an uncertain outcome without replaying it automatically."""

        if resolution not in {"succeeded", "cancelled"}:
            raise ValueError("unsupported unknown-message resolution")
        self.ensure_schema()
        now = time.time()
        stored_resolution = f"{resolution}_by_user"
        error_code = "" if resolution == "succeeded" else "UNKNOWN_CANCELLED"
        error = "" if resolution == "succeeded" else "uncertain execution cancelled by user"
        with self._session() as conn, conn:
            cursor = conn.execute(
                """
                UPDATE session_messages
                SET status = ?, finished_at = ?, updated_at = ?, resolved_at = ?,
                    resolution = ?, last_error_code = ?, last_error = ?
                WHERE message_id = ? AND owner_scope_id = ?
                  AND (source_session_id = ? OR target_session_id = ?)
                  AND status = 'unknown' AND resolved_at IS NULL
                """,
                (
                    resolution,
                    now,
                    now,
                    now,
                    stored_resolution,
                    error_code,
                    error,
                    message_id,
                    owner_scope_id,
                    participant_session_id,
                    participant_session_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM session_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_record(row)

    def recover_inflight_as_unknown(self) -> int:
        """Conservatively quarantine work whose execution outcome is uncertain."""

        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            cursor = conn.execute(
                """
                UPDATE session_messages
                SET status = 'unknown', finished_at = ?, updated_at = ?,
                    last_error_code = 'HOST_RESTARTED',
                    last_error = 'execution outcome is unknown after host restart'
                WHERE status IN ('running', 'waiting_user')
                """,
                (now, now),
            )
        return int(cursor.rowcount)

    def cancel_pending_for_target(
        self,
        target_session_id: str,
        *,
        error_code: str = "TARGET_DELETED",
        error: str = "target Session was deleted",
    ) -> list[SessionMessageRecord]:
        """Cancel records that cannot continue after target deletion."""

        self.ensure_schema()
        now = time.time()
        with self._session() as conn, conn:
            rows = conn.execute(
                """
                SELECT message_id FROM session_messages
                WHERE target_session_id = ?
                  AND status IN ('queued', 'running', 'waiting_user', 'unknown')
                ORDER BY sequence
                """,
                (target_session_id,),
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            conn.execute(
                """
                UPDATE session_messages
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    last_error_code = ?, last_error = ?,
                    content = '',
                    resolved_at = CASE
                        WHEN status = 'unknown' THEN ? ELSE resolved_at END,
                    resolution = CASE
                        WHEN status = 'unknown' THEN 'target_deleted' ELSE resolution END
                WHERE target_session_id = ?
                  AND status IN ('queued', 'running', 'waiting_user', 'unknown')
                """,
                (now, now, error_code, error, now, target_session_id),
            )
            conn.execute(
                "UPDATE session_messages SET content = '' WHERE target_session_id = ?",
                (target_session_id,),
            )
            if not message_ids:
                return []
            placeholders = ",".join("?" for _ in message_ids)
            updated_rows = conn.execute(
                f"""
                SELECT * FROM session_messages
                WHERE message_id IN ({placeholders})
                ORDER BY sequence
                """,
                tuple(message_ids),
            ).fetchall()
        return self._rows_to_records(updated_rows)

    def pending_counts(self, target_session_ids: list[str]) -> dict[str, int]:
        self.ensure_schema()
        if not target_session_ids:
            return {}
        target_placeholders = ",".join("?" for _ in target_session_ids)
        status_placeholders = ",".join("?" for _ in ACTIVE_SESSION_MESSAGE_STATUSES)
        with self._session() as conn, conn:
            rows = conn.execute(
                f"""
                SELECT target_session_id, COUNT(*) AS count
                FROM session_messages
                WHERE target_session_id IN ({target_placeholders})
                  AND status IN ({status_placeholders})
                  AND (status != 'unknown' OR resolved_at IS NULL)
                GROUP BY target_session_id
                """,
                (*target_session_ids, *sorted(ACTIVE_SESSION_MESSAGE_STATUSES)),
            ).fetchall()
        return {str(row["target_session_id"]): int(row["count"]) for row in rows}

    def blocking_states(self, target_session_ids: list[str]) -> dict[str, str]:
        """Return durable waiting/unknown states for Session list projection."""

        self.ensure_schema()
        if not target_session_ids:
            return {}
        placeholders = ",".join("?" for _ in target_session_ids)
        with self._session() as conn, conn:
            rows = conn.execute(
                f"""
                SELECT target_session_id, status FROM session_messages
                WHERE target_session_id IN ({placeholders})
                  AND (
                    status = 'waiting_user'
                    OR (status = 'unknown' AND resolved_at IS NULL)
                  )
                ORDER BY sequence
                """,
                tuple(target_session_ids),
            ).fetchall()
        states: dict[str, str] = {}
        for row in rows:
            session_id = str(row["target_session_id"])
            status = str(row["status"])
            if status == "unknown" or session_id not in states:
                states[session_id] = status
        return states

    def list_messages(
        self,
        *,
        owner_scope_id: str,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionMessageRecord]:
        self.ensure_schema()
        with self._session() as conn, conn:
            rows = conn.execute(
                """
                SELECT * FROM session_messages
                WHERE owner_scope_id = ?
                  AND (source_session_id = ? OR target_session_id = ?)
                ORDER BY sequence DESC LIMIT ? OFFSET ?
                """,
                (owner_scope_id, session_id, session_id, limit, offset),
            ).fetchall()
        return self._rows_to_records(rows)
