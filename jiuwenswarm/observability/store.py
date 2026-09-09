# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lossless SQLite storage and read queries for Agent OTLP records."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from jiuwenswarm.common.mode_matrix import (
    SINGLE_AGENT_CANONICAL_MODES,
    TEAM_CANONICAL_MODES,
)
from jiuwenswarm.observability.config import (
    DEFAULT_DETAIL_MAX_BYTES,
    session_database_path,
)
from jiuwenswarm.observability.models import (
    CommittedTraceUpdate,
    TraceRecordData,
    WriteBatchResult,
)
from jiuwenswarm.observability.projection import (
    TrajectoryScope,
    project_trajectory_scope,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 3
_BUSY_TIMEOUT_MS = 5000
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_JSON_NESTING_DEPTH = 256
_ABSENT_STORE_EPOCH = "absent"
_TRAJECTORY_MODE_VALUES = tuple(
    sorted(SINGLE_AGENT_CANONICAL_MODES | TEAM_CANONICAL_MODES),
)
_TRAJECTORY_MODE_PLACEHOLDERS = ",".join("?" for _ in _TRAJECTORY_MODE_VALUES)
_ELIGIBLE_TRACES_CTE = f"""
eligible_traces AS (
    SELECT trace_id
    FROM trajectory_current_records
    GROUP BY trace_id
    HAVING SUM(
        CASE
            WHEN agent_mode IS NOT NULL
             AND TRIM(agent_mode) <> ''
             AND LOWER(TRIM(agent_mode)) IN ({_TRAJECTORY_MODE_PLACEHOLDERS})
            THEN 1 ELSE 0
        END
    ) > 0
       AND SUM(
        CASE
            WHEN agent_mode IS NOT NULL
             AND TRIM(agent_mode) <> ''
             AND LOWER(TRIM(agent_mode)) NOT IN ({_TRAJECTORY_MODE_PLACEHOLDERS})
            THEN 1 ELSE 0
        END
    ) = 0
)
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS otlp_span_records (
    ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    session_id TEXT,
    request_id TEXT,
    run_id TEXT,
    agent_mode TEXT,
    start_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'processor',
    created_at INTEGER NOT NULL,
    has_error INTEGER NOT NULL DEFAULT 0,
    raw_json BLOB NOT NULL,
    raw_sha256 TEXT NOT NULL,
    UNIQUE(trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_otlp_records_session_start_ingest
    ON otlp_span_records(session_id, start_time_unix_nano DESC, ingest_seq DESC);
CREATE INDEX IF NOT EXISTS idx_otlp_records_session_ingest
    ON otlp_span_records(session_id, ingest_seq);
CREATE INDEX IF NOT EXISTS idx_otlp_records_session_request_ingest
    ON otlp_span_records(session_id, request_id, ingest_seq);
CREATE INDEX IF NOT EXISTS idx_otlp_records_trace_ingest
    ON otlp_span_records(trace_id, ingest_seq);

CREATE TABLE IF NOT EXISTS otlp_record_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    existing_sha256 TEXT NOT NULL,
    incoming_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_otlp_conflicts_identity
    ON otlp_record_conflicts(trace_id, span_id, created_at);

CREATE TABLE IF NOT EXISTS trajectory_store_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    store_epoch TEXT NOT NULL,
    max_ingest_seq INTEGER NOT NULL,
    max_change_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trajectory_current_records (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    session_id TEXT,
    request_id TEXT,
    run_id TEXT,
    agent_mode TEXT,
    lifecycle TEXT NOT NULL,
    record_revision INTEGER NOT NULL,
    change_seq INTEGER NOT NULL,
    start_time_unix_nano INTEGER NOT NULL,
    observed_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    has_error INTEGER NOT NULL DEFAULT 0,
    raw_json BLOB NOT NULL,
    raw_sha256 TEXT NOT NULL,
    update_kind TEXT NOT NULL,
    PRIMARY KEY(trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_trajectory_current_session_change
    ON trajectory_current_records(session_id, change_seq);
CREATE INDEX IF NOT EXISTS idx_trajectory_current_trace_change
    ON trajectory_current_records(trace_id, change_seq);

CREATE TABLE IF NOT EXISTS trajectory_changes (
    change_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    session_id TEXT,
    request_id TEXT,
    run_id TEXT,
    agent_mode TEXT,
    lifecycle TEXT NOT NULL,
    record_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    start_time_unix_nano INTEGER NOT NULL,
    observed_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    has_error INTEGER NOT NULL DEFAULT 0,
    raw_json BLOB NOT NULL,
    raw_sha256 TEXT NOT NULL,
    update_kind TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trajectory_changes_session_change
    ON trajectory_changes(session_id, change_seq);
CREATE INDEX IF NOT EXISTS idx_trajectory_changes_trace_change
    ON trajectory_changes(trace_id, change_seq);
"""


class TrajectoryCursorError(ValueError):
    """Raised when an opaque trajectory cursor is malformed."""


class TraceRevisionPage(NamedTuple):
    """One bounded page of trace summaries changed after a revision cursor.

    Attributes:
        items: Trace summaries changed within the page window.
        next_cursor: Cursor resuming the same polling pass.
        watermark: Cursor pinned to the bound captured on the first page.
        has_more: Whether the current pass still has pages left.
        reset: Whether the caller's cursor no longer matches the store.
        store_epoch: Epoch the page was read under.
    """

    items: list[dict[str, Any]]
    next_cursor: str
    watermark: str
    has_more: bool
    reset: bool
    store_epoch: str


class TrajectoryStore:
    """Single-threaded SQLite writer that preserves raw record bytes unchanged."""

    def __init__(self, database_path: Path, *, retention_days: int = 7) -> None:
        self.database_path = Path(database_path)
        self.retention_days = max(1, int(retention_days))
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open the writer connection and initialize the idempotent schema."""
        if self._connection is not None:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(_SCHEMA_SQL)
            self._ensure_store_state_columns(connection)
            removed = self._remove_missing_final_current(connection)
            migrated = self._migrate_final_current(connection)
            self._abandon_running_current(connection)
            team_modes_backfilled = self._backfill_inferred_team_modes(connection)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._initialize_store_state(connection)
            if migrated or removed or team_modes_backfilled:
                self._rotate_store_epoch(connection)
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def close(self) -> None:
        """Commit and close the writer connection if it is open."""
        connection = self._connection
        if connection is None:
            return
        try:
            connection.commit()
        finally:
            connection.close()
            self._connection = None

    def write_records(self, records: Sequence[TraceRecordData]) -> WriteBatchResult:
        """Commit one batch and return coalesced session/trace revisions.

        Args:
            records: Immutable records copied from Core before queueing.

        Returns:
            Counts and highest committed revision for each visible trace.
        """
        if not records:
            return WriteBatchResult(inserted=0, conflicts=0, updates=())
        connection = self._require_connection()
        inserted = 0
        conflicts = 0
        changed_trace_ids: set[str] = set()
        incoming_trace_ids = sorted({record.trace_id for record in records})
        try:
            connection.execute("BEGIN IMMEDIATE")
            eligibility_before = self._trace_eligibility(connection, incoming_trace_ids)
            for record in records:
                if record.lifecycle != "final":
                    if self._upsert_current_record(connection, record):
                        inserted += 1
                        changed_trace_ids.add(record.trace_id)
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO otlp_span_records (
                        trace_id,
                        span_id,
                        parent_span_id,
                        session_id,
                        request_id,
                        run_id,
                        agent_mode,
                        start_time_unix_nano,
                        end_time_unix_nano,
                        schema_version,
                        source,
                        created_at,
                        has_error,
                        raw_json,
                        raw_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trace_id, span_id) DO NOTHING
                    """,
                    (
                        record.trace_id,
                        record.span_id,
                        record.parent_span_id,
                        record.session_id,
                        record.request_id,
                        record.run_id,
                        record.agent_mode,
                        record.start_time_unix_nano,
                        record.end_time_unix_nano,
                        record.schema_version,
                        record.source,
                        record.created_at,
                        0,
                        sqlite3.Binary(record.raw_json),
                        record.raw_sha256,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    changed_trace_ids.add(record.trace_id)
                    has_error = _record_has_error(record.raw_json)
                    if has_error:
                        connection.execute(
                            """
                            UPDATE otlp_span_records
                            SET has_error = 1
                            WHERE trace_id = ? AND span_id = ?
                            """,
                            (record.trace_id, record.span_id),
                        )
                    self._upsert_current_record(connection, record, has_error=has_error)
                    continue
                existing = connection.execute(
                    """
                    SELECT raw_sha256
                    FROM otlp_span_records
                    WHERE trace_id = ? AND span_id = ?
                    """,
                    (record.trace_id, record.span_id),
                ).fetchone()
                existing_sha256 = str(existing["raw_sha256"]) if existing is not None else ""
                if existing_sha256 == record.raw_sha256:
                    self._upsert_current_record(connection, record)
                    continue
                conflicts += 1
                connection.execute(
                    """
                    INSERT INTO otlp_record_conflicts (
                        trace_id,
                        span_id,
                        existing_sha256,
                        incoming_sha256,
                        source,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.trace_id,
                        record.span_id,
                        existing_sha256,
                        record.raw_sha256,
                        record.source,
                        record.created_at,
                    ),
                )
                logger.warning(
                    "Trajectory record conflict preserved the first raw record: trace_id=%s span_id=%s",
                    record.trace_id,
                    record.span_id,
                )

            changed_trace_ids.update(self._reconcile_orphans(connection, records))
            eligibility_after = self._trace_eligibility(connection, incoming_trace_ids)
            visible_trace_removed = any(
                eligibility_before[trace_id][0]
                and eligibility_before[trace_id][1]
                and not eligibility_after[trace_id][1]
                for trace_id in incoming_trace_ids
            )
            if visible_trace_removed:
                self._rotate_store_epoch(connection)
            else:
                self._sync_max_ingest_seq(connection)
            updates = self._committed_updates(connection, changed_trace_ids)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return WriteBatchResult(
            inserted=inserted,
            conflicts=conflicts,
            updates=updates,
        )

    @staticmethod
    def _upsert_current_record(
        connection: sqlite3.Connection,
        record: TraceRecordData,
        *,
        has_error: bool | None = None,
    ) -> bool:
        current = connection.execute(
            """
            SELECT lifecycle, record_revision, raw_sha256
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (record.trace_id, record.span_id),
        ).fetchone()
        if current is not None:
            current_lifecycle = str(current["lifecycle"])
            current_revision = int(current["record_revision"])
            if current_lifecycle == "final":
                return False
            if record.lifecycle != "final" and record.record_revision <= current_revision:
                return False
        resolved_has_error = (
            _record_has_error(record.raw_json) if has_error is None else has_error
        )
        cursor = connection.execute(
            """
            INSERT INTO trajectory_changes (
                trace_id, span_id, parent_span_id, session_id, request_id,
                run_id, agent_mode, lifecycle, record_revision, operation,
                start_time_unix_nano, observed_time_unix_nano,
                end_time_unix_nano, schema_version, source, created_at,
                has_error, raw_json, raw_sha256, update_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'upsert', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.trace_id,
                record.span_id,
                record.parent_span_id,
                record.session_id,
                record.request_id,
                record.run_id,
                record.agent_mode,
                record.lifecycle,
                record.record_revision,
                record.start_time_unix_nano,
                record.observed_time_unix_nano,
                record.end_time_unix_nano,
                record.schema_version,
                record.source,
                record.created_at,
                int(resolved_has_error),
                # The change journal is a revision index, not a second payload
                # store. Detail reads load the complete snapshot from
                # trajectory_current_records, while final archives use
                # otlp_span_records. Keeping the full BLOB here multiplied
                # every streaming revision into unbounded write amplification.
                sqlite3.Binary(b""),
                record.raw_sha256,
                record.update_kind,
            ),
        )
        change_seq = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO trajectory_current_records (
                trace_id, span_id, parent_span_id, session_id, request_id,
                run_id, agent_mode, lifecycle, record_revision, change_seq,
                start_time_unix_nano, observed_time_unix_nano,
                end_time_unix_nano, schema_version, source, created_at,
                has_error, raw_json, raw_sha256, update_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, span_id) DO UPDATE SET
                parent_span_id = excluded.parent_span_id,
                session_id = COALESCE(excluded.session_id, trajectory_current_records.session_id),
                request_id = COALESCE(excluded.request_id, trajectory_current_records.request_id),
                run_id = COALESCE(excluded.run_id, trajectory_current_records.run_id),
                agent_mode = COALESCE(excluded.agent_mode, trajectory_current_records.agent_mode),
                lifecycle = excluded.lifecycle,
                record_revision = excluded.record_revision,
                change_seq = excluded.change_seq,
                start_time_unix_nano = excluded.start_time_unix_nano,
                observed_time_unix_nano = excluded.observed_time_unix_nano,
                end_time_unix_nano = excluded.end_time_unix_nano,
                schema_version = excluded.schema_version,
                source = excluded.source,
                created_at = excluded.created_at,
                has_error = excluded.has_error,
                raw_json = excluded.raw_json,
                raw_sha256 = excluded.raw_sha256,
                update_kind = excluded.update_kind
            """,
            (
                record.trace_id,
                record.span_id,
                record.parent_span_id,
                record.session_id,
                record.request_id,
                record.run_id,
                record.agent_mode,
                record.lifecycle,
                record.record_revision,
                change_seq,
                record.start_time_unix_nano,
                record.observed_time_unix_nano,
                record.end_time_unix_nano,
                record.schema_version,
                record.source,
                record.created_at,
                int(resolved_has_error),
                sqlite3.Binary(record.raw_json),
                record.raw_sha256,
                record.update_kind,
            ),
        )
        return True

    @staticmethod
    def _migrate_final_current(connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            """
            SELECT records.*
            FROM otlp_span_records AS records
            LEFT JOIN trajectory_current_records AS current
              ON current.trace_id = records.trace_id AND current.span_id = records.span_id
            WHERE current.trace_id IS NULL
            ORDER BY records.ingest_seq ASC
            """
        ).fetchall()
        for row in rows:
            record = TraceRecordData(
                raw_json=bytes(row["raw_json"]),
                raw_sha256=str(row["raw_sha256"]),
                trace_id=str(row["trace_id"]),
                span_id=str(row["span_id"]),
                parent_span_id=row["parent_span_id"],
                start_time_unix_nano=int(row["start_time_unix_nano"]),
                end_time_unix_nano=int(row["end_time_unix_nano"]),
                session_id=row["session_id"],
                request_id=row["request_id"],
                run_id=row["run_id"],
                agent_mode=row["agent_mode"],
                schema_version=str(row["schema_version"]),
                source=str(row["source"]),
                created_at=int(row["created_at"]),
                lifecycle="final",
                record_revision=1,
                observed_time_unix_nano=int(row["end_time_unix_nano"]),
                update_kind="completed",
            )
            TrajectoryStore._upsert_current_record(
                connection,
                record,
                has_error=bool(row["has_error"]),
            )
        return len(rows)

    @staticmethod
    def _remove_missing_final_current(connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            """
            SELECT current.trace_id, current.span_id
            FROM trajectory_current_records AS current
            LEFT JOIN otlp_span_records AS records
              ON records.trace_id = current.trace_id AND records.span_id = current.span_id
            WHERE current.lifecycle = 'final' AND records.trace_id IS NULL
            """
        ).fetchall()
        for row in rows:
            identity = (str(row["trace_id"]), str(row["span_id"]))
            connection.execute(
                "DELETE FROM trajectory_current_records WHERE trace_id = ? AND span_id = ?",
                identity,
            )
            connection.execute(
                "DELETE FROM trajectory_changes WHERE trace_id = ? AND span_id = ?",
                identity,
            )
        return len(rows)

    @staticmethod
    def _abandon_running_current(connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            """
            SELECT *
            FROM trajectory_current_records
            WHERE lifecycle = 'running'
            ORDER BY change_seq ASC
            """
        ).fetchall()
        observed_time = time.time_ns()
        created_at = int(time.time())
        for row in rows:
            cursor = connection.execute(
                """
                INSERT INTO trajectory_changes (
                    trace_id, span_id, parent_span_id, session_id, request_id,
                    run_id, agent_mode, lifecycle, record_revision, operation,
                    start_time_unix_nano, observed_time_unix_nano,
                    end_time_unix_nano, schema_version, source, created_at,
                    has_error, raw_json, raw_sha256, update_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'abandoned', ?, 'upsert', ?, ?, 0, ?, ?, ?, ?, ?, ?, 'recovered')
                """,
                (
                    row["trace_id"],
                    row["span_id"],
                    row["parent_span_id"],
                    row["session_id"],
                    row["request_id"],
                    row["run_id"],
                    row["agent_mode"],
                    row["record_revision"],
                    row["start_time_unix_nano"],
                    observed_time,
                    row["schema_version"],
                    row["source"],
                    created_at,
                    row["has_error"],
                    sqlite3.Binary(b""),
                    row["raw_sha256"],
                ),
            )
            connection.execute(
                """
                UPDATE trajectory_current_records
                SET lifecycle = 'abandoned',
                    change_seq = ?,
                    observed_time_unix_nano = ?,
                    created_at = ?,
                    update_kind = 'recovered'
                WHERE trace_id = ? AND span_id = ?
                """,
                (
                    int(cursor.lastrowid),
                    observed_time,
                    created_at,
                    row["trace_id"],
                    row["span_id"],
                ),
            )
        return len(rows)

    @staticmethod
    def _backfill_inferred_team_modes(connection: sqlite3.Connection) -> int:
        """Repair mode-less Team traces produced before the Team routing fix.

        Raw OTLP remains immutable. Only the derived routing column is filled,
        and only when an authoritative Team scope exists and the trace has no
        conflicting non-Team mode.
        """
        rows = connection.execute(
            """
            SELECT trace_id, raw_json
            FROM trajectory_current_records
            WHERE agent_mode IS NULL OR TRIM(agent_mode) = ''
            ORDER BY change_seq ASC
            """
        ).fetchall()
        inferred_trace_ids: set[str] = set()
        for row in rows:
            trace_id = str(row["trace_id"])
            if trace_id in inferred_trace_ids:
                continue
            try:
                payload = json.loads(bytes(row["raw_json"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            scope = project_trajectory_scope(payload)
            if scope.team_id is not None or scope.team_name is not None:
                inferred_trace_ids.add(trace_id)

        repaired = 0
        allowed_team_modes = tuple(sorted(TEAM_CANONICAL_MODES))
        mode_placeholders = ",".join("?" for _ in allowed_team_modes)
        for trace_id in sorted(inferred_trace_ids):
            conflict = connection.execute(
                f"""
                SELECT 1
                FROM trajectory_current_records
                WHERE trace_id = ?
                  AND agent_mode IS NOT NULL
                  AND TRIM(agent_mode) <> ''
                  AND LOWER(TRIM(agent_mode)) NOT IN ({mode_placeholders})
                LIMIT 1
                """,
                (trace_id, *allowed_team_modes),
            ).fetchone()
            if conflict is not None:
                logger.warning(
                    "Trajectory Team mode backfill skipped conflicting trace: trace_id=%s",
                    trace_id,
                )
                continue
            for table in (
                "otlp_span_records",
                "trajectory_current_records",
                "trajectory_changes",
            ):
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET agent_mode = 'team'
                    WHERE trace_id = ?
                      AND (agent_mode IS NULL OR TRIM(agent_mode) = '')
                    """,
                    (trace_id,),
                )
            repaired += 1
        return repaired

    def delete_expired(self, *, now: int | None = None) -> int:
        """Delete records older than the configured retention window."""
        connection = self._require_connection()
        cutoff = int(now if now is not None else time.time()) - self.retention_days * 86400
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM otlp_span_records WHERE created_at < ?",
                (cutoff,),
            )
            current_cursor = connection.execute(
                "DELETE FROM trajectory_current_records WHERE created_at < ?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM trajectory_changes WHERE created_at < ?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM otlp_record_conflicts WHERE created_at < ?",
                (cutoff,),
            )
            if cursor.rowcount > 0 or current_cursor.rowcount > 0:
                self._rotate_store_epoch(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return max(0, int(cursor.rowcount))

    def fetch_raw(self, trace_id: str, span_id: str) -> bytes | None:
        """Return exact stored bytes for writer-side diagnostics and tests."""
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT raw_json
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (trace_id, span_id),
        ).fetchone()
        return bytes(row["raw_json"]) if row is not None else None

    def fetch_raw_sha256(self, trace_id: str, span_id: str) -> str | None:
        """Return the hash persisted beside one raw record."""
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT raw_sha256
            FROM trajectory_current_records
            WHERE trace_id = ? AND span_id = ?
            """,
            (trace_id, span_id),
        ).fetchone()
        return str(row["raw_sha256"]) if row is not None else None

    def count_conflicts(self) -> int:
        """Return the persisted conflict diagnostic count."""
        connection = self._require_connection()
        row = connection.execute("SELECT COUNT(*) AS count FROM otlp_record_conflicts").fetchone()
        return int(row["count"]) if row is not None else 0

    def fetch_store_epoch(self) -> str:
        """Return the current persistent epoch for diagnostics and tests."""
        connection = self._require_connection()
        row = connection.execute(
            "SELECT store_epoch FROM trajectory_store_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Trajectory store state is missing")
        return str(row["store_epoch"])

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("TrajectoryStore is not initialized")
        return connection

    @staticmethod
    def _initialize_store_state(connection: sqlite3.Connection) -> None:
        current_max = _current_max_ingest_seq(connection)
        current_change_max = _current_max_change_seq(connection)
        row = connection.execute(
            """
            SELECT store_epoch, max_ingest_seq, max_change_seq
            FROM trajectory_store_state
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO trajectory_store_state (
                    singleton,
                    store_epoch,
                    max_ingest_seq,
                    max_change_seq
                ) VALUES (1, ?, ?, ?)
                """,
                (_new_store_epoch(), current_max, current_change_max),
            )
            return
        try:
            stored_epoch = str(row["store_epoch"])
            stored_max = int(row["max_ingest_seq"])
            stored_change_max = int(row["max_change_seq"])
        except (TypeError, ValueError, OverflowError):
            stored_epoch = ""
            stored_max = -1
            stored_change_max = -1
        stored_state_invalid = not stored_epoch or stored_max < 0 or stored_change_max < 0
        watermark_regressed = (
            current_max < stored_max or current_change_max < stored_change_max
        )
        if stored_state_invalid or watermark_regressed:
            TrajectoryStore._rotate_store_epoch(connection)
            return
        if current_max > stored_max or current_change_max > stored_change_max:
            connection.execute(
                """
                UPDATE trajectory_store_state
                SET max_ingest_seq = ?, max_change_seq = ?
                WHERE singleton = 1
                """,
                (current_max, current_change_max),
            )

    @staticmethod
    def _ensure_store_state_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(trajectory_store_state)").fetchall()
        }
        if "max_change_seq" not in columns:
            connection.execute(
                "ALTER TABLE trajectory_store_state ADD COLUMN max_change_seq INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _sync_max_ingest_seq(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE trajectory_store_state
            SET max_ingest_seq = ?, max_change_seq = ?
            WHERE singleton = 1
            """,
            (_current_max_ingest_seq(connection), _current_max_change_seq(connection)),
        )

    @staticmethod
    def _rotate_store_epoch(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE trajectory_store_state
            SET store_epoch = ?, max_ingest_seq = ?, max_change_seq = ?
            WHERE singleton = 1
            """,
            (
                _new_store_epoch(),
                _current_max_ingest_seq(connection),
                _current_max_change_seq(connection),
            ),
        )

    @staticmethod
    def _trace_eligibility(
        connection: sqlite3.Connection,
        trace_ids: Sequence[str],
    ) -> dict[str, tuple[bool, bool]]:
        states = {trace_id: (False, False) for trace_id in trace_ids}
        if not trace_ids:
            return states
        placeholders = ",".join("?" for _ in trace_ids)
        rows = connection.execute(
            f"""
            SELECT trace_id,
                   COUNT(*) AS record_count,
                   SUM(
                       CASE
                           WHEN agent_mode IS NOT NULL
                            AND TRIM(agent_mode) <> ''
                            AND LOWER(TRIM(agent_mode)) IN (
                                {_TRAJECTORY_MODE_PLACEHOLDERS}
                            )
                           THEN 1 ELSE 0
                       END
                   ) AS known_count,
                   SUM(
                       CASE
                           WHEN agent_mode IS NOT NULL
                            AND TRIM(agent_mode) <> ''
                            AND LOWER(TRIM(agent_mode)) NOT IN (
                                {_TRAJECTORY_MODE_PLACEHOLDERS}
                            )
                           THEN 1 ELSE 0
                       END
                   ) AS rejected_count
            FROM otlp_span_records
            WHERE trace_id IN ({placeholders})
            GROUP BY trace_id
            """,
            (
                *_TRAJECTORY_MODE_VALUES,
                *_TRAJECTORY_MODE_VALUES,
                *trace_ids,
            ),
        ).fetchall()
        for row in rows:
            trace_id = str(row["trace_id"])
            record_count = int(row["record_count"])
            known_count = int(row["known_count"] or 0)
            rejected_count = int(row["rejected_count"] or 0)
            states[trace_id] = (
                record_count > 0,
                known_count > 0 and rejected_count == 0,
            )
        return states

    @staticmethod
    def _reconcile_orphans(
        connection: sqlite3.Connection,
        records: Sequence[TraceRecordData],
    ) -> set[str]:
        changed_trace_ids: set[str] = set()
        for trace_id in sorted({record.trace_id for record in records}):
            rows = connection.execute(
                """
                SELECT session_id, request_id, run_id, agent_mode
                FROM otlp_span_records
                WHERE trace_id = ? AND session_id IS NOT NULL
                ORDER BY ingest_seq ASC
                """,
                (trace_id,),
            ).fetchall()
            session_ids = {str(row["session_id"]) for row in rows}
            if not session_ids:
                continue
            if len(session_ids) != 1:
                logger.warning(
                    "Trajectory orphan reconciliation skipped ambiguous session hints: trace_id=%s",
                    trace_id,
                )
                continue
            session_id = next(iter(session_ids))
            request_id = _unique_text_hint(rows, "request_id")
            run_id = _unique_text_hint(rows, "run_id")
            agent_mode = _unique_text_hint(rows, "agent_mode")
            cursor = connection.execute(
                """
                UPDATE otlp_span_records
                SET session_id = COALESCE(session_id, ?),
                    request_id = COALESCE(request_id, ?),
                    run_id = COALESCE(run_id, ?),
                    agent_mode = COALESCE(agent_mode, ?)
                WHERE trace_id = ?
                  AND (session_id IS NULL OR session_id = ?)
                  AND (
                      session_id IS NULL
                      OR (request_id IS NULL AND ? IS NOT NULL)
                      OR (run_id IS NULL AND ? IS NOT NULL)
                      OR (agent_mode IS NULL AND ? IS NOT NULL)
                  )
                """,
                (
                    session_id,
                    request_id,
                    run_id,
                    agent_mode,
                    trace_id,
                    session_id,
                    request_id,
                    run_id,
                    agent_mode,
                ),
            )
            if cursor.rowcount > 0:
                connection.execute(
                    """
                    UPDATE trajectory_current_records
                    SET session_id = COALESCE(session_id, ?),
                        request_id = COALESCE(request_id, ?),
                        run_id = COALESCE(run_id, ?),
                        agent_mode = COALESCE(agent_mode, ?)
                    WHERE trace_id = ? AND (session_id IS NULL OR session_id = ?)
                    """,
                    (session_id, request_id, run_id, agent_mode, trace_id, session_id),
                )
                connection.execute(
                    """
                    UPDATE trajectory_changes
                    SET session_id = COALESCE(session_id, ?),
                        request_id = COALESCE(request_id, ?),
                        run_id = COALESCE(run_id, ?),
                        agent_mode = COALESCE(agent_mode, ?)
                    WHERE trace_id = ? AND (session_id IS NULL OR session_id = ?)
                    """,
                    (session_id, request_id, run_id, agent_mode, trace_id, session_id),
                )
                changed_trace_ids.add(trace_id)
        return changed_trace_ids

    @staticmethod
    def _committed_updates(
        connection: sqlite3.Connection,
        changed_trace_ids: set[str],
    ) -> tuple[CommittedTraceUpdate, ...]:
        updates: list[CommittedTraceUpdate] = []
        epoch_row = connection.execute(
            "SELECT store_epoch FROM trajectory_store_state WHERE singleton = 1"
        ).fetchone()
        store_epoch = str(epoch_row["store_epoch"]) if epoch_row is not None else None
        for trace_id in sorted(changed_trace_ids):
            rows = connection.execute(
                """
                SELECT session_id, MAX(ingest_seq) AS revision
                FROM (
                    SELECT trace_id, session_id, change_seq AS ingest_seq
                    FROM trajectory_current_records
                )
                WHERE trace_id = ? AND session_id IS NOT NULL
                GROUP BY session_id
                """,
                (trace_id,),
            ).fetchall()
            for row in rows:
                lifecycle_row = connection.execute(
                    """
                    SELECT lifecycle
                    FROM trajectory_current_records
                    WHERE trace_id = ? AND session_id = ?
                    ORDER BY change_seq DESC
                    LIMIT 1
                    """,
                    (trace_id, str(row["session_id"])),
                ).fetchone()
                updates.append(
                    CommittedTraceUpdate(
                        session_id=str(row["session_id"]),
                        trace_id=trace_id,
                        revision=int(row["revision"]),
                        store_epoch=store_epoch,
                        lifecycle=(
                            str(lifecycle_row["lifecycle"])
                            if lifecycle_row is not None
                            else "final"
                        ),
                    )
                )
        return tuple(updates)


class AsyncTrajectoryReader:
    """Gateway-side read-only view over trajectory SQLite databases."""

    def __init__(self, database_path: Path, *, session_scoped: bool = False) -> None:
        self.database_path = Path(database_path)
        self.session_scoped = session_scoped
        self._usage_locks: dict[str, asyncio.Lock] = {}
        self._usage_cache: dict[str, dict[str, Any]] = {}

    async def get_session_request_usage(
        self,
        session_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Return session-complete cumulative usage partitioned by execution subject."""
        usage_lock = self._usage_locks.setdefault(session_id, asyncio.Lock())
        async with usage_lock:
            connection = await self._connect(session_id)
            if connection is None:
                return [], _ABSENT_STORE_EPOCH
            try:
                await connection.execute("BEGIN")
                store_epoch = await _read_store_epoch(connection)
                watermark = await _session_revision_watermark(connection, session_id)
                cache = self._usage_cache.get(session_id)
                cache_valid = (
                    cache is not None
                    and cache["store_epoch"] == store_epoch
                    and int(cache["watermark"]) <= watermark
                )
                after_revision = int(cache["watermark"]) if cache_valid else 0
                facts = dict(cache["facts"]) if cache_valid else {}
                async with connection.execute(
                    """
                    SELECT trace_id,
                           start_time_unix_nano,
                           change_seq,
                           raw_json
                    FROM trajectory_current_records
                    WHERE session_id = ? AND change_seq > ?
                    ORDER BY change_seq ASC
                    """,
                    (session_id, after_revision),
                ) as statement:
                    rows = await statement.fetchall()
                for row in rows:
                    fact = _request_usage_fact(
                        bytes(row["raw_json"]),
                        trace_id=str(row["trace_id"]),
                        start_time_unix_nano=int(row["start_time_unix_nano"]),
                    )
                    if fact is None:
                        continue
                    facts[(fact["trace_id"], fact["inference_id"])] = fact
                self._usage_cache[session_id] = {
                    "store_epoch": store_epoch,
                    "watermark": watermark,
                    "facts": facts,
                }
            finally:
                await connection.rollback()
                await connection.close()
        return _cumulative_request_usage(tuple(facts.values())), store_epoch

    async def list_traces(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List trace summaries in stable newest-first order."""
        items, next_cursor, _revision_cursor, _store_epoch = (
            await self.list_traces_with_revision_cursor(
                session_id,
                limit=limit,
                cursor=cursor,
            )
        )
        return items, next_cursor

    async def get_session_archive_records(
        self,
        session_id: str,
    ) -> tuple[list[dict[str, Any]], str, int]:
        """Read every current record for one session from one SQLite snapshot."""
        connection = await self._connect(session_id)
        if connection is None:
            return [], _ABSENT_STORE_EPOCH, 0
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            revision = await _session_revision_watermark(connection, session_id)
            query = f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT records.trace_id,
                       records.span_id,
                       records.parent_span_id,
                       records.session_id,
                       records.request_id,
                       records.run_id,
                       records.agent_mode,
                       records.lifecycle,
                       records.record_revision,
                       records.change_seq,
                       records.start_time_unix_nano,
                       records.observed_time_unix_nano,
                       records.end_time_unix_nano,
                       records.schema_version,
                       records.source,
                       records.created_at,
                       records.raw_json,
                       records.raw_sha256,
                       records.update_kind
                FROM trajectory_current_records AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ?
                ORDER BY records.start_time_unix_nano ASC,
                         records.trace_id ASC,
                         records.span_id ASC
            """
            params: tuple[Any, ...] = (
                *_trajectory_scope_params(),
                session_id,
            )
            async with connection.execute(query, params) as statement:
                rows = await statement.fetchall()
        finally:
            await connection.rollback()
            await connection.close()
        return [_archive_record_from_row(row) for row in rows], store_epoch, revision

    async def list_traces_with_revision_cursor(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, str, str]:
        """List summaries and a polling baseline from one SQLite snapshot."""
        cursor_value = decode_trace_cursor(cursor) if cursor else None
        connection = await self._connect(session_id)
        if connection is None:
            store_epoch = _ABSENT_STORE_EPOCH
            if cursor_value is not None:
                cursor_session_id, cursor_epoch, cursor_ingest_seq, _trace_id = cursor_value
                if (
                    cursor_session_id != session_id
                    or cursor_epoch != store_epoch
                    or cursor_ingest_seq > 0
                ):
                    raise TrajectoryCursorError("trajectory list cursor is out of scope")
            return (
                [],
                None,
                encode_revision_cursor(session_id, store_epoch, 0),
                store_epoch,
            )
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            revision_ingest_seq = await _session_revision_watermark(connection, session_id)
            if cursor_value is None:
                query = f"""
                    WITH {_ELIGIBLE_TRACES_CTE}
                    SELECT records.trace_id,
                           MIN(records.change_seq) AS list_ingest_seq,
                           MAX(records.change_seq) AS revision,
                           MIN(records.start_time_unix_nano) AS start_time_unix_nano,
                           MAX(records.end_time_unix_nano) AS end_time_unix_nano,
                           COUNT(*) AS span_count,
                           MAX(records.request_id) AS request_id,
                           MAX(records.run_id) AS run_id,
                           MAX(records.agent_mode) AS agent_mode,
                           MAX(records.has_error) AS has_error
                    FROM trajectory_current_records AS records
                    INNER JOIN eligible_traces
                        ON eligible_traces.trace_id = records.trace_id
                    WHERE records.session_id = ?
                    GROUP BY records.trace_id
                    ORDER BY MIN(records.change_seq) DESC, records.trace_id DESC
                    LIMIT ?
                """
                params: tuple[Any, ...] = (
                    *_trajectory_scope_params(),
                    session_id,
                    limit + 1,
                )
            else:
                (
                    cursor_session_id,
                    cursor_epoch,
                    cursor_ingest_seq,
                    cursor_trace_id,
                ) = cursor_value
                if (
                    cursor_session_id != session_id
                    or cursor_epoch != store_epoch
                    or cursor_ingest_seq > revision_ingest_seq
                ):
                    raise TrajectoryCursorError("trajectory list cursor is out of scope")
                query = f"""
                    WITH {_ELIGIBLE_TRACES_CTE}
                    SELECT records.trace_id,
                           MIN(records.change_seq) AS list_ingest_seq,
                           MAX(records.change_seq) AS revision,
                           MIN(records.start_time_unix_nano) AS start_time_unix_nano,
                           MAX(records.end_time_unix_nano) AS end_time_unix_nano,
                           COUNT(*) AS span_count,
                           MAX(records.request_id) AS request_id,
                           MAX(records.run_id) AS run_id,
                           MAX(records.agent_mode) AS agent_mode,
                           MAX(records.has_error) AS has_error
                    FROM trajectory_current_records AS records
                    INNER JOIN eligible_traces
                        ON eligible_traces.trace_id = records.trace_id
                    WHERE records.session_id = ?
                    GROUP BY records.trace_id
                    HAVING MIN(records.change_seq) < ?
                        OR (MIN(records.change_seq) = ? AND records.trace_id < ?)
                    ORDER BY MIN(records.change_seq) DESC, records.trace_id DESC
                    LIMIT ?
                """
                params = (
                    *_trajectory_scope_params(),
                    session_id,
                    cursor_ingest_seq,
                    cursor_ingest_seq,
                    cursor_trace_id,
                    limit + 1,
                )
            async with connection.execute(query, params) as statement:
                rows = await statement.fetchall()
        finally:
            await connection.rollback()
            await connection.close()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [_trace_summary_from_row(row) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = encode_trace_cursor(
                session_id,
                store_epoch,
                int(last["list_ingest_seq"]),
                str(last["trace_id"]),
            )
        return (
            items,
            next_cursor,
            encode_revision_cursor(session_id, store_epoch, revision_ingest_seq),
            store_epoch,
        )

    async def list_trace_revisions(
        self,
        session_id: str,
        *,
        after_revision: str,
        limit: int,
    ) -> TraceRevisionPage:
        """List trace summaries changed after an opaque revision cursor.

        One polling pass is bounded by the watermark captured on its first page.
        A continuation cursor carries that bound so concurrent commits are left
        for the next pass instead of extending the current pagination window.
        """
        (
            cursor_session_id,
            cursor_epoch,
            after_ingest_seq,
            through_ingest_seq,
        ) = decode_revision_cursor(after_revision)
        connection = await self._connect(session_id)
        if connection is None:
            store_epoch = _ABSENT_STORE_EPOCH
            stable_cursor = encode_revision_cursor(session_id, store_epoch, 0)
            reset = (
                cursor_session_id != session_id
                or cursor_epoch != store_epoch
                or after_ingest_seq > 0
                or (through_ingest_seq is not None and through_ingest_seq > 0)
            )
            return TraceRevisionPage(
                items=[],
                next_cursor=stable_cursor,
                watermark=stable_cursor,
                has_more=False,
                reset=reset,
                store_epoch=store_epoch,
            )
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            current_watermark = await _session_revision_watermark(connection, session_id)
            reset = (
                cursor_session_id != session_id
                or cursor_epoch != store_epoch
                or after_ingest_seq > current_watermark
                or (
                    through_ingest_seq is not None
                    and through_ingest_seq > current_watermark
                )
            )
            if reset:
                stable_cursor = encode_revision_cursor(
                    session_id,
                    store_epoch,
                    current_watermark,
                )
                return TraceRevisionPage(
                    items=[],
                    next_cursor=stable_cursor,
                    watermark=stable_cursor,
                    has_more=False,
                    reset=True,
                    store_epoch=store_epoch,
                )
            if through_ingest_seq is None:
                through_ingest_seq = current_watermark
            query = f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT records.change_seq AS ingest_seq,
                       records.trace_id
                FROM trajectory_changes AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ?
                  AND records.change_seq > ?
                  AND records.change_seq <= ?
                ORDER BY records.change_seq ASC
                LIMIT ?
            """
            params: tuple[Any, ...] = (
                *_trajectory_scope_params(),
                session_id,
                after_ingest_seq,
                through_ingest_seq,
                limit + 1,
            )
            async with connection.execute(query, params) as statement:
                change_rows = await statement.fetchall()
            has_more = len(change_rows) > limit
            visible_change_rows = change_rows[:limit]
            changed_trace_ids = list(
                dict.fromkeys(str(row["trace_id"]) for row in visible_change_rows)
            )
            summaries = await _trace_summaries_by_id(
                connection,
                session_id,
                changed_trace_ids,
                through_ingest_seq,
            )
        finally:
            await connection.rollback()
            await connection.close()

        summary_by_trace_id = {str(item["trace_id"]): item for item in summaries}
        items = [
            summary_by_trace_id[trace_id]
            for trace_id in changed_trace_ids
            if trace_id in summary_by_trace_id
        ]
        next_ingest_seq = through_ingest_seq
        if has_more and visible_change_rows:
            next_ingest_seq = int(visible_change_rows[-1]["ingest_seq"])
        watermark = encode_revision_cursor(
            session_id,
            store_epoch,
            through_ingest_seq,
        )
        next_cursor = encode_revision_cursor(
            session_id,
            store_epoch,
            next_ingest_seq,
            through_ingest_seq=through_ingest_seq if has_more else None,
        )
        return TraceRevisionPage(
            items=items,
            next_cursor=next_cursor,
            watermark=watermark,
            has_more=has_more,
            reset=False,
            store_epoch=store_epoch,
        )

    async def get_trace_records(
        self,
        session_id: str,
        trace_id: str,
        *,
        since_revision: int,
        limit: int,
        max_bytes: int = DEFAULT_DETAIL_MAX_BYTES,
    ) -> dict[str, Any] | None:
        """Read a complete or incremental page for one trace."""
        connection = await self._connect(session_id)
        if connection is None:
            return None
        try:
            await connection.execute("BEGIN")
            aggregate = await _fetch_one(
                connection,
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT MIN(records.change_seq) AS first_revision,
                       MAX(records.change_seq) AS current_revision
                FROM trajectory_changes AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ? AND records.trace_id = ?
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                    trace_id,
                ),
            )
            if aggregate is None or aggregate["current_revision"] is None:
                return None
            first_revision = int(aggregate["first_revision"])
            current_revision = int(aggregate["current_revision"])
            reset = since_revision > current_revision or (
                since_revision > 0 and since_revision < first_revision
            )
            effective_since = 0 if reset else since_revision
            # Detail is a coalesced current-state delta, not a replay of every
            # journal revision. Every current record is a complete upsert
            # snapshot. Any future operation that removes one identity without
            # rotating store_epoch must add a durable tombstone before this
            # query can support it safely.
            async with connection.execute(
                """
                SELECT change_seq AS ingest_seq,
                       trace_id,
                       span_id,
                       record_revision,
                       lifecycle,
                       'upsert' AS operation,
                       observed_time_unix_nano,
                       LENGTH(raw_json) AS raw_size_bytes
                FROM trajectory_current_records
                WHERE session_id = ?
                  AND trace_id = ?
                  AND change_seq > ?
                ORDER BY change_seq ASC
                LIMIT ?
                """,
                (session_id, trace_id, effective_since, limit + 1),
            ) as statement:
                metadata_rows = await statement.fetchall()

            selected_metadata: list[tuple[aiosqlite.Row, bool]] = []
            projected_raw_bytes = 0
            byte_budget = max(1, int(max_bytes))
            for row in metadata_rows:
                if len(selected_metadata) >= limit:
                    break
                raw_size = max(0, int(row["raw_size_bytes"] or 0))
                if not selected_metadata and raw_size > byte_budget:
                    selected_metadata.append((row, True))
                    break
                if projected_raw_bytes + raw_size > byte_budget:
                    break
                selected_metadata.append((row, False))
                projected_raw_bytes += raw_size

            raw_rows: list[aiosqlite.Row] = []
            fetchable_metadata = [
                row for row, projection_omitted in selected_metadata if not projection_omitted
            ]
            if fetchable_metadata:
                last_fetch_revision = int(fetchable_metadata[-1]["ingest_seq"])
                async with connection.execute(
                    """
                    SELECT change_seq AS ingest_seq,
                           trace_id,
                           span_id,
                           record_revision,
                           lifecycle,
                           'upsert' AS operation,
                           observed_time_unix_nano,
                           LENGTH(raw_json) AS raw_size_bytes,
                           raw_json
                    FROM trajectory_current_records
                    WHERE session_id = ?
                      AND trace_id = ?
                      AND change_seq > ?
                      AND change_seq <= ?
                    ORDER BY change_seq ASC
                    """,
                    (
                        session_id,
                        trace_id,
                        effective_since,
                        last_fetch_revision,
                    ),
                ) as statement:
                    raw_rows = await statement.fetchall()
        finally:
            await connection.rollback()
            await connection.close()
        raw_by_revision = {int(row["ingest_seq"]): row for row in raw_rows}
        records: list[dict[str, Any]] = []
        for row, projection_omitted in selected_metadata:
            ingest_seq = int(row["ingest_seq"])
            if projection_omitted:
                records.append(_omitted_detail_record_from_row(row))
            else:
                records.append(_detail_record_from_row(raw_by_revision[ingest_seq]))
        has_more = len(metadata_rows) > len(selected_metadata)
        next_since_revision = effective_since
        if selected_metadata:
            next_since_revision = int(selected_metadata[-1][0]["ingest_seq"])
        return {
            "revision": current_revision,
            "reset": reset,
            "records": records,
            "has_more": has_more,
            "next_since_revision": next_since_revision,
            "projected_raw_bytes": projected_raw_bytes,
            "max_projected_raw_bytes": byte_budget,
        }

    async def get_raw_record(
        self,
        session_id: str,
        trace_id: str,
        span_id: str,
    ) -> bytes | None:
        """Return exact raw bytes when the identity belongs to the session."""
        connection = await self._connect(session_id)
        if connection is None:
            return None
        try:
            row = await _fetch_one(
                connection,
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT records.raw_json
                FROM otlp_span_records AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ?
                  AND records.trace_id = ?
                  AND records.span_id = ?
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                    trace_id,
                    span_id,
                ),
            )
        finally:
            await connection.close()
        return bytes(row["raw_json"]) if row is not None else None

    async def _connect(self, session_id: str) -> aiosqlite.Connection | None:
        database_path = self.database_path
        if self.session_scoped:
            database_path = session_database_path(database_path, session_id)
        if not database_path.is_file():
            return None
        connection = await aiosqlite.connect(
            f"{database_path.resolve().as_uri()}?mode=ro",
            timeout=_BUSY_TIMEOUT_MS / 1000,
            uri=True,
        )
        connection.row_factory = aiosqlite.Row
        await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        await connection.execute("PRAGMA query_only=ON")
        return connection


async def _fetch_one(
    connection: aiosqlite.Connection,
    query: str,
    params: tuple[Any, ...],
) -> aiosqlite.Row | None:
    async with connection.execute(query, params) as statement:
        return await statement.fetchone()


async def _read_store_epoch(connection: aiosqlite.Connection) -> str:
    table = await _fetch_one(
        connection,
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'trajectory_store_state'
        """,
        (),
    )
    if table is None:
        return _ABSENT_STORE_EPOCH
    row = await _fetch_one(
        connection,
        """
        SELECT store_epoch
        FROM trajectory_store_state
        WHERE singleton = 1
        """,
        (),
    )
    if row is None:
        return _ABSENT_STORE_EPOCH
    store_epoch = str(row["store_epoch"] or "")
    return store_epoch if store_epoch else _ABSENT_STORE_EPOCH


def _current_max_ingest_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(ingest_seq), 0) AS max_ingest_seq FROM otlp_span_records"
    ).fetchone()
    return int(row["max_ingest_seq"]) if row is not None else 0


def _current_max_change_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(change_seq), 0) AS max_change_seq FROM trajectory_changes"
    ).fetchone()
    return int(row["max_change_seq"]) if row is not None else 0


def _new_store_epoch() -> str:
    return uuid.uuid4().hex


def _unique_text_hint(rows: Sequence[sqlite3.Row], column: str) -> str | None:
    values = {
        str(row[column])
        for row in rows
        if row[column] is not None and str(row[column]).strip()
    }
    if len(values) != 1:
        return None
    return next(iter(values))


def _trajectory_scope_params() -> tuple[Any, ...]:
    """Return parameters for the trace-level single-Agent eligibility CTE."""
    return (
        *_TRAJECTORY_MODE_VALUES,
        *_TRAJECTORY_MODE_VALUES,
    )


async def _session_revision_watermark(
    connection: aiosqlite.Connection,
    session_id: str,
) -> int:
    row = await _fetch_one(
        connection,
        f"""
        WITH {_ELIGIBLE_TRACES_CTE}
        SELECT COALESCE(MAX(records.change_seq), 0) AS revision_ingest_seq
        FROM trajectory_current_records AS records
        INNER JOIN eligible_traces
            ON eligible_traces.trace_id = records.trace_id
        WHERE records.session_id = ?
        """,
        (
            *_trajectory_scope_params(),
            session_id,
        ),
    )
    return int(row["revision_ingest_seq"]) if row is not None else 0


async def _trace_summaries_by_id(
    connection: aiosqlite.Connection,
    session_id: str,
    trace_ids: list[str],
    through_ingest_seq: int,
) -> list[dict[str, Any]]:
    if not trace_ids:
        return []
    trace_placeholders = ",".join("?" for _ in trace_ids)
    query = f"""
        WITH {_ELIGIBLE_TRACES_CTE},
        latest_records AS (
            SELECT records.*
            FROM trajectory_changes AS records
            WHERE records.change_seq <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM trajectory_changes AS newer
                  WHERE newer.trace_id = records.trace_id
                    AND newer.span_id = records.span_id
                    AND newer.change_seq <= ?
                    AND newer.change_seq > records.change_seq
              )
        )
        SELECT records.trace_id,
               MIN(records.change_seq) AS list_ingest_seq,
               MAX(records.change_seq) AS revision,
               MIN(records.start_time_unix_nano) AS start_time_unix_nano,
               MAX(records.end_time_unix_nano) AS end_time_unix_nano,
               COUNT(*) AS span_count,
               MAX(records.request_id) AS request_id,
               MAX(records.run_id) AS run_id,
               MAX(records.agent_mode) AS agent_mode,
               MAX(records.has_error) AS has_error
        FROM latest_records AS records
        INNER JOIN eligible_traces
            ON eligible_traces.trace_id = records.trace_id
        WHERE records.session_id = ?
          AND records.trace_id IN ({trace_placeholders})
        GROUP BY records.trace_id
    """
    params: tuple[Any, ...] = (
        *_trajectory_scope_params(),
        through_ingest_seq,
        through_ingest_seq,
        session_id,
        *trace_ids,
    )
    async with connection.execute(query, params) as statement:
        rows = await statement.fetchall()
    return [_trace_summary_from_row(row) for row in rows]


def encode_revision_cursor(
    session_id: str,
    store_epoch: str,
    after_ingest_seq: int,
    *,
    through_ingest_seq: int | None = None,
) -> str:
    """Encode a stable polling cursor without exposing the SQLite sequence."""
    session_value = _cursor_text(session_id)
    epoch_value = _cursor_text(store_epoch)
    after_value = _cursor_sequence(after_ingest_seq)
    payload = {
        "v": 2,
        "s": session_value,
        "e": epoch_value,
        "a": str(after_value),
    }
    if through_ingest_seq is not None:
        through_value = _cursor_sequence(through_ingest_seq)
        if through_value < after_value:
            raise TrajectoryCursorError("invalid trajectory revision cursor")
        payload["u"] = str(through_value)
    return _encode_cursor_payload(payload)


def decode_revision_cursor(cursor: str) -> tuple[str, str, int, int | None]:
    """Decode and validate an opaque polling cursor."""
    try:
        payload = _decode_cursor_payload(cursor)
        expected_fields = {"v", "s", "e", "a"}
        if "u" in payload:
            expected_fields.add("u")
        if set(payload) != expected_fields or type(payload.get("v")) is not int:
            raise TrajectoryCursorError("invalid trajectory revision cursor")
        if payload["v"] != 2:
            raise TrajectoryCursorError("invalid trajectory revision cursor")
        session_id = _cursor_text(payload["s"])
        store_epoch = _cursor_text(payload["e"])
        raw_after = payload["a"]
        raw_through = payload.get("u")
        after_ingest_seq = _decode_cursor_sequence(raw_after)
        through_ingest_seq = (
            _decode_cursor_sequence(raw_through) if raw_through is not None else None
        )
    except TrajectoryCursorError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TrajectoryCursorError("invalid trajectory revision cursor") from exc
    if through_ingest_seq is not None and through_ingest_seq < after_ingest_seq:
        raise TrajectoryCursorError("invalid trajectory revision cursor")
    return session_id, store_epoch, after_ingest_seq, through_ingest_seq


def encode_trace_cursor(
    session_id: str,
    store_epoch: str,
    first_ingest_seq: int,
    trace_id: str,
) -> str:
    """Encode a stable opaque list cursor."""
    payload = {
        "v": 3,
        "s": _cursor_text(session_id),
        "e": _cursor_text(store_epoch),
        "i": str(_cursor_sequence(first_ingest_seq)),
        "t": _cursor_text(trace_id),
    }
    return _encode_cursor_payload(payload)


def decode_trace_cursor(cursor: str) -> tuple[str, str, int, str]:
    """Decode and validate a list cursor without exposing SQL details."""
    try:
        payload = _decode_cursor_payload(cursor)
        if set(payload) != {"v", "s", "e", "i", "t"}:
            raise TrajectoryCursorError("invalid trajectory cursor")
        if type(payload.get("v")) is not int or payload["v"] != 3:
            raise TrajectoryCursorError("invalid trajectory cursor")
        session_id = _cursor_text(payload["s"])
        store_epoch = _cursor_text(payload["e"])
        first_ingest_seq = _decode_cursor_sequence(payload["i"])
        trace_id = _cursor_text(payload["t"])
    except TrajectoryCursorError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TrajectoryCursorError("invalid trajectory cursor") from exc
    return session_id, store_epoch, first_ingest_seq, trace_id


def _encode_cursor_payload(payload: dict[str, Any]) -> str:
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")
    if len(cursor) > 512:
        raise TrajectoryCursorError("trajectory cursor is too large")
    return cursor


def _decode_cursor_payload(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise TrajectoryCursorError("invalid trajectory cursor")
    if cursor != cursor.strip() or len(cursor) % 4 == 1:
        raise TrajectoryCursorError("invalid trajectory cursor")
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in cursor
    ):
        raise TrajectoryCursorError("invalid trajectory cursor")
    padding = "=" * (-len(cursor) % 4)
    try:
        encoded = cursor.encode("ascii")
        decoded = base64.b64decode(
            encoded + padding.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded:
            raise TrajectoryCursorError("invalid trajectory cursor")
        payload = json.loads(decoded, object_pairs_hook=_unique_cursor_object)
    except TrajectoryCursorError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise TrajectoryCursorError("invalid trajectory cursor") from exc
    if not isinstance(payload, dict):
        raise TrajectoryCursorError("invalid trajectory cursor")
    return payload


def _unique_cursor_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise TrajectoryCursorError("invalid trajectory cursor")
        payload[key] = value
    return payload


def _cursor_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TrajectoryCursorError("invalid trajectory cursor")
    return value


def _cursor_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrajectoryCursorError("invalid trajectory cursor sequence")
    if value < 0 or value > _MAX_SQLITE_INTEGER:
        raise TrajectoryCursorError("invalid trajectory cursor sequence")
    return value


def _decode_cursor_sequence(value: object) -> int:
    if not isinstance(value, str) or not value:
        raise TrajectoryCursorError("invalid trajectory cursor sequence")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TrajectoryCursorError("invalid trajectory cursor sequence") from exc
    if value != str(parsed):
        raise TrajectoryCursorError("invalid trajectory cursor sequence")
    return _cursor_sequence(parsed)


def _record_has_error(raw_json: bytes) -> bool:
    try:
        payload = _strict_otlp_payload(raw_json)
    except Exception:
        return False
    resource_spans = payload.get("resourceSpans")
    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            continue
        scope_spans = resource_span.get("scopeSpans")
        if not isinstance(scope_spans, list):
            continue
        for scope_span in scope_spans:
            if not isinstance(scope_span, dict):
                continue
            spans = scope_span.get("spans")
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                status = span.get("status")
                if not isinstance(status, dict):
                    continue
                code = status.get("code")
                if code == 2 or str(code or "").strip().upper() in {
                    "2",
                    "ERROR",
                    "STATUS_CODE_ERROR",
                }:
                    return True
    return False


def _otlp_attribute_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
    ):
        if key in value:
            return value[key]
    return None


def _request_usage_fact(
    raw_json: bytes,
    *,
    trace_id: str,
    start_time_unix_nano: int,
) -> dict[str, Any] | None:
    try:
        payload = _strict_otlp_payload(raw_json)
    except Exception:
        return None
    spans = []
    for resource_span in payload.get("resourceSpans", []):
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans", []):
            if isinstance(scope_span, dict):
                spans.extend(scope_span.get("spans", []))
    if len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    if spans[0].get("name") != "llm.call":
        return None
    attributes = {
        str(attribute.get("key")): _otlp_attribute_value(attribute.get("value"))
        for attribute in spans[0].get("attributes", [])
        if isinstance(attribute, dict) and isinstance(attribute.get("key"), str)
    }
    inference_id = str(attributes.get("openjiuwen.inference.id") or "").strip()
    if not inference_id:
        return None
    subject_id = str(attributes.get("openjiuwen.execution.subject.id") or "main").strip()
    usage_keys = {
        "input": ("gen_ai.usage.input_tokens",),
        "cacheRead": ("gen_ai.usage.cache_read.input_tokens",),
        "cacheWrite": ("gen_ai.usage.cache_creation.input_tokens",),
        "output": ("gen_ai.usage.output_tokens",),
        "reasoning": ("gen_ai.usage.reasoning.output_tokens",),
    }
    usage: dict[str, int] = {}
    for output_key, attribute_keys in usage_keys.items():
        raw_value = next(
            (
                attributes[key]
                for key in attribute_keys
                if attributes.get(key) is not None
            ),
            None,
        )
        try:
            value = int(raw_value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= value <= _MAX_SQLITE_INTEGER:
            usage[output_key] = value
    input_tokens = usage.get("input")
    output_tokens = usage.get("output")
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        if total_tokens <= _MAX_SQLITE_INTEGER:
            usage["total"] = total_tokens
    return {
        "trace_id": trace_id,
        "inference_id": inference_id,
        "subject_id": subject_id or "main",
        "start_time_unix_nano": start_time_unix_nano,
        "usage": usage,
    }


def _cumulative_request_usage(
    facts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    cumulative_by_subject: dict[str, dict[str, int]] = {}
    result: list[dict[str, Any]] = []
    for fact in sorted(
        facts,
        key=lambda item: (
            str(item["subject_id"]),
            int(item["start_time_unix_nano"]),
            str(item["trace_id"]),
            str(item["inference_id"]),
        ),
    ):
        subject_id = str(fact["subject_id"])
        cumulative = cumulative_by_subject.setdefault(subject_id, {})
        for key, value in dict(fact["usage"]).items():
            cumulative[key] = cumulative.get(key, 0) + int(value)
        result.append({
            **fact,
            "start_time_unix_nano": str(fact["start_time_unix_nano"]),
            "cumulative_usage": dict(cumulative),
        })
    return result


def _trace_summary_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "trace_id": str(row["trace_id"]),
        "revision": int(row["revision"]),
        "start_time_unix_nano": int(row["start_time_unix_nano"]),
        "end_time_unix_nano": int(row["end_time_unix_nano"]),
        "span_count": int(row["span_count"]),
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "agent_mode": row["agent_mode"],
        "has_error": bool(row["has_error"]),
    }


def _detail_record_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    raw_json = bytes(row["raw_json"])
    try:
        otlp = _strict_otlp_payload(raw_json)
    except (RecursionError, TypeError, ValueError, OverflowError):
        otlp = None
    return {
        "ingest_seq": int(row["ingest_seq"]),
        "change_seq": int(row["ingest_seq"]),
        "record_id": f'{row["trace_id"]}:{row["span_id"]}',
        "trace_id": str(row["trace_id"]),
        "span_id": str(row["span_id"]),
        "record_revision": int(row["record_revision"]),
        "lifecycle": str(row["lifecycle"]),
        "operation": str(row["operation"]),
        "observed_time_unix_nano": str(row["observed_time_unix_nano"]),
        "raw_size_bytes": int(row["raw_size_bytes"]),
        "otlp": otlp,
        "raw_valid": otlp is not None,
    }


def _omitted_detail_record_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Describe a record that exceeds the projection budget without loading it."""
    return {
        "ingest_seq": int(row["ingest_seq"]),
        "change_seq": int(row["ingest_seq"]),
        "record_id": f'{row["trace_id"]}:{row["span_id"]}',
        "trace_id": str(row["trace_id"]),
        "span_id": str(row["span_id"]),
        "record_revision": int(row["record_revision"]),
        "lifecycle": str(row["lifecycle"]),
        "operation": str(row["operation"]),
        "observed_time_unix_nano": str(row["observed_time_unix_nano"]),
        "raw_size_bytes": int(row["raw_size_bytes"]),
        "otlp": None,
        "raw_valid": None,
        "projection_omitted": "record_too_large",
    }


def _archive_record_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Build one lossless, version-independent archive current record."""
    raw_json = bytes(row["raw_json"])
    try:
        otlp = _strict_otlp_payload(raw_json)
    except (RecursionError, TypeError, ValueError, OverflowError):
        otlp = None
    return {
        "record_id": f'{row["trace_id"]}:{row["span_id"]}',
        "trace_id": str(row["trace_id"]),
        "span_id": str(row["span_id"]),
        "parent_span_id": row["parent_span_id"],
        "record_revision": int(row["record_revision"]),
        "lifecycle": str(row["lifecycle"]),
        "operation": "upsert",
        "change_seq": str(row["change_seq"]),
        "start_time_unix_nano": str(row["start_time_unix_nano"]),
        "observed_time_unix_nano": str(row["observed_time_unix_nano"]),
        "end_time_unix_nano": str(row["end_time_unix_nano"]),
        "session_id": row["session_id"],
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "agent_mode": row["agent_mode"],
        "schema_version": str(row["schema_version"]),
        "source": str(row["source"]),
        "created_at": int(row["created_at"]),
        "update_kind": str(row["update_kind"]),
        "raw_sha256": str(row["raw_sha256"]),
        "raw_json_base64": base64.b64encode(raw_json).decode("ascii"),
        "otlp": otlp,
        "raw_valid": otlp is not None,
    }


def _strict_otlp_payload(raw_json: bytes) -> dict[str, Any]:
    """Parse strict finite JSON and validate the minimum OTLP envelope shape."""

    _validate_json_nesting(raw_json)

    def _reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    payload = json.loads(
        raw_json,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(payload, dict):
        raise ValueError("OTLP record must be a JSON object")
    if not isinstance(payload.get("resourceSpans"), list):
        raise ValueError("OTLP record resourceSpans must be an array")
    return payload


def _validate_json_nesting(raw_json: bytes) -> None:
    """Reject excessive JSON nesting without decoding or recursive traversal."""
    depth = 0
    in_string = False
    escaped = False
    for character in raw_json:
        if in_string:
            if escaped:
                escaped = False
            elif character == 0x5C:
                escaped = True
            elif character == 0x22:
                in_string = False
            continue
        if character == 0x22:
            in_string = True
            continue
        if character in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON nesting depth exceeds projection limit")
        elif character in (0x5D, 0x7D):
            depth -= 1


__all__ = [
    "AsyncTrajectoryReader",
    "TrajectoryCursorError",
    "TrajectoryStore",
    "decode_revision_cursor",
    "decode_trace_cursor",
    "encode_revision_cursor",
    "encode_trace_cursor",
]
