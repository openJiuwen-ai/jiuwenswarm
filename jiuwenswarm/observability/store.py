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
import zlib
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
    StreamFrameData,
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
# Serialized name of the OTLP span status code, used to skip parsing a payload
# that cannot carry an error status. See ``_record_has_error``.
_STATUS_CODE_KEY = b'"code"'
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
    execution_subject_id TEXT NOT NULL DEFAULT 'main',
    execution_subject_display_name TEXT,
    execution_subject_kind TEXT,
    execution_subject_parent_id TEXT,
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
    execution_subject_id TEXT NOT NULL DEFAULT 'main',
    execution_subject_display_name TEXT,
    execution_subject_kind TEXT,
    execution_subject_parent_id TEXT,
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
    raw_size_bytes INTEGER NOT NULL DEFAULT 0,
    raw_sha256 TEXT NOT NULL,
    update_kind TEXT NOT NULL,
    PRIMARY KEY(trace_id, span_id)
);

CREATE INDEX IF NOT EXISTS idx_trajectory_current_session_change
    ON trajectory_current_records(session_id, change_seq);
-- One execution subject's records in commit order. This is the chain a reader
-- follows end to end, so it is the index the detail read is built on.
CREATE INDEX IF NOT EXISTS idx_trajectory_current_subject_change
    ON trajectory_current_records(session_id, execution_subject_id, change_seq);
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

-- The span a run of frames came from, named once instead of on every frame.
-- One streaming turn emits hundreds of frames from a single span, and a
-- 32-character trace id plus a 16-character span id on each of them cost
-- twenty times what those frames actually say. Here they cost one row.
CREATE TABLE IF NOT EXISTS trajectory_frame_spans (
    span_ref INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_subject_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    UNIQUE (trace_id, span_id)
);

-- One model-stream frame. Frames are append-only and small: a frame states
-- one increment of an answer, never the whole of it, so a streaming turn
-- costs what it actually produced instead of its length squared.
--
-- What a frame says is text, arguments_delta and a tool's identity. Its own
-- identity is span_ref and sequence, and nothing else about where it came
-- from is repeated here.
CREATE TABLE IF NOT EXISTS trajectory_stream_frames (
    frame_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    span_ref INTEGER NOT NULL REFERENCES trajectory_frame_spans(span_ref),
    sequence INTEGER NOT NULL,
    -- A code for one of the kinds a model stream can state, never the word.
    kind INTEGER NOT NULL,
    text TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    arguments_delta TEXT,
    -- When the frame was produced. No separate write timestamp: a frame is
    -- queued and committed within milliseconds of being produced, and the
    -- only thing that reads it -- retention -- measures in days.
    timestamp_unix_nano INTEGER NOT NULL
);

-- No session column and no session index either: a writer opens one file per
-- session, so the session is a property of the file, not of each of its
-- 33,000 rows. Catching up walks the file in commit order, which frame_seq
-- already is -- it is the rowid, so that walk is a primary-key scan.
--
-- Replaying one answer, and discarding the frames of a span that turned out
-- to be incomplete, both address frames by the span that produced them.
CREATE INDEX IF NOT EXISTS idx_trajectory_frames_span_ref
    ON trajectory_stream_frames(span_ref, sequence);

-- One piece of content, stored once however many records state it. The GenAI
-- convention has every model call restate its whole input; this is where that
-- repetition stops. created_at is refreshed on every reference, so content a
-- live conversation keeps restating never ages out from under it.
CREATE TABLE IF NOT EXISTS trajectory_blobs (
    blob_hash  TEXT PRIMARY KEY,
    content    BLOB NOT NULL,
    byte_size  INTEGER NOT NULL,
    -- When this content was first referenced. A reader resuming from a
    -- revision already holds everything first seen at or before it, so this
    -- is what lets one response carry only what is new to that reader.
    -- Unlike created_at it is never refreshed: it states first sight, not
    -- last use.
    first_change_seq INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

-- One prefix of a sequence, addressed by the content of that prefix:
--     seq_hash = H(prev_hash || blob_hash)
-- Equal seq_hash means every element is equal, in order, so a reader knows
-- nothing changed without fetching anything. Sequences sharing a prefix share
-- these rows, which is what makes a growing conversation cost its increment.
CREATE TABLE IF NOT EXISTS trajectory_sequences (
    seq_hash   TEXT PRIMARY KEY,
    prev_hash  TEXT,
    blob_hash  TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- Walking a chain back to its root follows prev_hash.
CREATE INDEX IF NOT EXISTS idx_trajectory_sequences_prev
    ON trajectory_sequences(prev_hash);
"""

# What a model stream can say is a closed set, so storage names each kind by a
# code rather than by a word spelled out on every one of a turn's hundreds of
# frames. A kind outside this set is a bug in whoever produced it, and fails
# loudly here rather than being stored as something no reader can render.
_FRAME_KIND_CODES: dict[str, int] = {
    "text-delta": 1,
    "reasoning-delta": 2,
    "tool-call-delta": 3,
    "usage": 4,
}
_FRAME_KIND_NAMES: dict[int, str] = {code: kind for kind, code in _FRAME_KIND_CODES.items()}

# A final span's payload already lives in otlp_span_records, so
# trajectory_current_records stores it only while the span is still running and
# leaves an empty BLOB once it ends. Reads resolve the two sources through this
# join. NULLIF keeps rows written before the column existed working unchanged:
# they still carry their own payload, so the fallback never applies to them and
# no backfill is needed.
_CURRENT_ARCHIVE_JOIN = """
    LEFT JOIN otlp_span_records AS archive
        ON archive.trace_id = current.trace_id
       AND archive.span_id = current.span_id
"""
_CURRENT_RAW_JSON = "COALESCE(NULLIF(current.raw_json, X''), archive.raw_json)"
# Pre-migration rows default to 0 here, so fall back to measuring the payload
# they still hold.
_CURRENT_RAW_SIZE = "COALESCE(NULLIF(current.raw_size_bytes, 0), LENGTH(current.raw_json))"

# OTLP JSON repeats its key names on every span, event and attribute, so it
# compresses several-fold. Level 3 sits at the knee of the curve for this data:
# measured against real payloads it reaches 3.7x for 0.65 ms per record, where
# level 6 spends 1.35 ms to reach 4.1x. Decompression costs 0.07 ms either way.
# SQLite's default bound-variable limit is 999; stay well inside it when
# fetching the elements one page of records refers to.
# The reference format Agent Core writes in place of a restated attribute.
_SEQUENCE_REFERENCE_PREFIX = "@oj-seq"
_SEQUENCE_REFERENCE_VERSION = "1"
_SEQUENCE_FETCH_CHUNK = 400
_PAYLOAD_COMPRESSION_LEVEL = 3
# Every uncompressed payload is a JSON object, so its first byte distinguishes
# it from a zlib stream without a version column or a migration.
_JSON_OBJECT_START = 0x7B


def _encode_payload(raw_json: bytes) -> bytes:
    """Compress one payload for storage."""
    return zlib.compress(raw_json, _PAYLOAD_COMPRESSION_LEVEL)


def _decode_payload(stored: bytes | None) -> bytes:
    """Return the original payload, whether or not it was stored compressed.

    Rows written before compression begin with ``{`` and are handed back
    untouched, so an existing database keeps working without a migration.
    """
    if not stored:
        return b""
    raw = bytes(stored)
    if raw[0] == _JSON_OBJECT_START:
        return raw
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


class TrajectoryCursorError(ValueError):
    """Raised when an opaque trajectory cursor is malformed."""


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
            self._drop_superseded_frame_table(connection)
            connection.executescript(_SCHEMA_SQL)
            self._ensure_store_state_columns(connection)
            self._ensure_current_record_columns(connection)
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

    def write_records(
        self,
        records: Sequence[TraceRecordData],
        frames: Sequence[StreamFrameData] = (),
    ) -> WriteBatchResult:
        """Commit one batch and return coalesced session/trace revisions.

        Records and frames share one transaction on purpose: they carry two
        independent watermarks, and committing them separately would let a
        reader observe one advance without the other and read a state that
        never existed.

        Args:
            records: Immutable records copied from Core before queueing.
            frames: Model-stream frames to append in the same transaction.

        Returns:
            Counts and highest committed revision for each visible trace.
        """
        if not records and not frames:
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
                        execution_subject_id,
                        execution_subject_display_name,
                        execution_subject_kind,
                        execution_subject_parent_id,
                        start_time_unix_nano,
                        end_time_unix_nano,
                        schema_version,
                        source,
                        created_at,
                        has_error,
                        raw_json,
                        raw_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        record.execution_subject_id,
                        record.execution_subject_display_name,
                        record.execution_subject_kind,
                        record.execution_subject_parent_id,
                        record.start_time_unix_nano,
                        record.end_time_unix_nano,
                        record.schema_version,
                        record.source,
                        record.created_at,
                        0,
                        # raw_sha256 stays the digest of the uncompressed bytes,
                        # so conflict detection is unaffected by the encoding.
                        sqlite3.Binary(_encode_payload(record.raw_json)),
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

            self._store_addressed_sequences(connection, records)
            frame_watermarks = self._append_stream_frames(connection, frames)
            # A span whose record did not change in this batch can still have
            # produced frames, and a reader learns about those only if that
            # trace is reported as changed.
            changed_trace_ids.update(trace_id for _session, trace_id in frame_watermarks)
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
            updates = self._committed_updates(
                connection,
                changed_trace_ids,
                frame_watermarks,
            )
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
    def _append_stream_frames(
        connection: sqlite3.Connection,
        frames: Sequence[StreamFrameData],
    ) -> dict[tuple[str, str], int]:
        """Append every frame and report the highest seq per session and trace.

        Frames are inserted, never merged: each states an increment that no
        later frame repeats. The rowid of the last insert for a trace is its
        watermark, because the sequence is monotonic within a transaction.

        Args:
            connection: The open write transaction.
            frames: Frames to append, in the order they were produced.

        Returns:
            Highest committed ``frame_seq`` keyed by session and trace.
        """
        watermarks: dict[tuple[str, str], int] = {}
        span_refs: dict[tuple[str, str], int] = {}
        for frame in frames:
            identity = (frame.trace_id, frame.span_id)
            span_ref = span_refs.get(identity)
            if span_ref is None:
                span_ref = TrajectoryStore._resolve_frame_span(connection, frame)
                span_refs[identity] = span_ref
            cursor = connection.execute(
                """
                INSERT INTO trajectory_stream_frames (
                    span_ref, sequence, kind, text, tool_call_id,
                    tool_name, arguments_delta, timestamp_unix_nano
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_ref,
                    frame.sequence,
                    _FRAME_KIND_CODES[frame.kind],
                    frame.text,
                    frame.tool_call_id,
                    frame.tool_name,
                    frame.arguments_delta,
                    frame.timestamp_unix_nano,
                ),
            )
            watermarks[(frame.session_id, frame.trace_id)] = int(cursor.lastrowid)
        return watermarks

    @staticmethod
    def _resolve_frame_span(
        connection: sqlite3.Connection,
        frame: StreamFrameData,
    ) -> int:
        """Name the span a frame came from, registering it the first time.

        No cache spans transactions: the batch a writer flushes almost always
        carries one span, so the dictionary the caller keeps for that batch
        already collapses this to one round trip per span per flush. Nothing
        is lost by not caching further, because a span is registered by its
        own identity -- registering it again finds what is already there.

        Args:
            connection: The open write transaction.
            frame: Any frame of the span to name.

        Returns:
            The integer this database names that span by.
        """
        identity = (frame.trace_id, frame.span_id)
        connection.execute(
            """
            INSERT INTO trajectory_frame_spans (
                execution_subject_id, trace_id, span_id
            ) VALUES (?, ?, ?)
            ON CONFLICT (trace_id, span_id) DO NOTHING
            """,
            (frame.execution_subject_id, *identity),
        )
        row = connection.execute(
            "SELECT span_ref FROM trajectory_frame_spans WHERE trace_id = ? AND span_id = ?",
            identity,
        ).fetchone()
        return int(row["span_ref"])

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
        # A final span is already archived in otlp_span_records under the same
        # identity, so storing the payload again here doubled the database for
        # no recoverable information. Running spans have no archive row yet and
        # keep theirs. The size is recorded either way, because the detail
        # reader budgets pages by it before it fetches any payload.
        is_final = record.lifecycle == "final"
        stored_raw_json = b"" if is_final else _encode_payload(record.raw_json)
        connection.execute(
            """
            INSERT INTO trajectory_current_records (
                trace_id, span_id, parent_span_id, session_id, request_id,
                run_id, agent_mode, execution_subject_id,
                execution_subject_display_name, execution_subject_kind,
                execution_subject_parent_id, lifecycle, record_revision, change_seq,
                start_time_unix_nano, observed_time_unix_nano,
                end_time_unix_nano, schema_version, source, created_at,
                has_error, raw_json, raw_size_bytes, raw_sha256, update_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, span_id) DO UPDATE SET
                parent_span_id = excluded.parent_span_id,
                session_id = COALESCE(excluded.session_id, trajectory_current_records.session_id),
                request_id = COALESCE(excluded.request_id, trajectory_current_records.request_id),
                run_id = COALESCE(excluded.run_id, trajectory_current_records.run_id),
                agent_mode = COALESCE(excluded.agent_mode, trajectory_current_records.agent_mode),
                execution_subject_id = excluded.execution_subject_id,
                execution_subject_display_name = COALESCE(
                    excluded.execution_subject_display_name,
                    trajectory_current_records.execution_subject_display_name
                ),
                execution_subject_kind = COALESCE(
                    excluded.execution_subject_kind,
                    trajectory_current_records.execution_subject_kind
                ),
                execution_subject_parent_id = COALESCE(
                    excluded.execution_subject_parent_id,
                    trajectory_current_records.execution_subject_parent_id
                ),
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
                raw_size_bytes = excluded.raw_size_bytes,
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
                record.execution_subject_id,
                record.execution_subject_display_name,
                record.execution_subject_kind,
                record.execution_subject_parent_id,
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
                sqlite3.Binary(stored_raw_json),
                record.logical_size_bytes or len(record.raw_json),
                record.raw_sha256,
                record.update_kind,
            ),
        )
        return True

    @staticmethod
    def _store_addressed_sequences(
        connection: sqlite3.Connection,
        records: Sequence[TraceRecordData],
    ) -> None:
        """Persist the content every record references, once per distinct piece.

        Both writes are idempotent by construction: a hash names its own
        content, so re-inserting is a no-op on the data and a refresh of when
        that content was last needed.

        Args:
            connection: The open write transaction.
            records: Records whose references must resolve afterwards.
        """
        blobs: dict[str, tuple[bytes, int]] = {}
        nodes: dict[str, tuple[str | None, str, int, int]] = {}
        for record in records:
            for sequence in record.sequences:
                for blob_hash, content in sequence.blobs.items():
                    blobs[blob_hash] = (content, record.created_at)
                for node in sequence.nodes:
                    nodes[node.seq_hash] = (
                        node.prev_hash,
                        node.blob_hash,
                        node.depth,
                        record.created_at,
                    )
        if blobs:
            # The batch's own watermark. Stamping the highest committed change
            # of this batch keeps first_change_seq at or above the revision of
            # every record that referenced the content here, so a reader
            # resuming from an earlier revision is never told it already has
            # something it does not.
            row = connection.execute(
                "SELECT COALESCE(MAX(change_seq), 0) AS seq FROM trajectory_changes"
            ).fetchone()
            batch_change_seq = int(row["seq"]) if row is not None else 0
            connection.executemany(
                """
                INSERT INTO trajectory_blobs (
                    blob_hash, content, byte_size, first_change_seq, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(blob_hash) DO UPDATE SET created_at = excluded.created_at
                """,
                [
                    (
                        blob_hash,
                        sqlite3.Binary(_encode_payload(content)),
                        len(content),
                        batch_change_seq,
                        created_at,
                    )
                    for blob_hash, (content, created_at) in blobs.items()
                ],
            )
        if nodes:
            connection.executemany(
                """
                INSERT INTO trajectory_sequences (
                    seq_hash, prev_hash, blob_hash, depth, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(seq_hash) DO UPDATE SET created_at = excluded.created_at
                """,
                [
                    (seq_hash, prev_hash, blob_hash, depth, created_at)
                    for seq_hash, (prev_hash, blob_hash, depth, created_at) in nodes.items()
                ],
            )

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
                raw_json=_decode_payload(row["raw_json"]),
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
                execution_subject_id=str(row["execution_subject_id"] or "main"),
                execution_subject_display_name=row["execution_subject_display_name"],
                execution_subject_kind=row["execution_subject_kind"],
                execution_subject_parent_id=row["execution_subject_parent_id"],
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
            # The span this frame stream described is gone, so its frames
            # describe nothing any reader can reach. The name goes with them:
            # nothing else refers to a span once its frames are gone.
            connection.execute(
                """
                DELETE FROM trajectory_stream_frames
                WHERE span_ref IN (
                    SELECT span_ref FROM trajectory_frame_spans
                    WHERE trace_id = ? AND span_id = ?
                )
                """,
                identity,
            )
            connection.execute(
                "DELETE FROM trajectory_frame_spans WHERE trace_id = ? AND span_id = ?",
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
            f"""
            SELECT current.trace_id AS trace_id,
                   {_CURRENT_RAW_JSON} AS raw_json
            FROM trajectory_current_records AS current
            {_CURRENT_ARCHIVE_JOIN}
            WHERE current.agent_mode IS NULL OR TRIM(current.agent_mode) = ''
            ORDER BY current.change_seq ASC
            """
        ).fetchall()
        inferred_trace_ids: set[str] = set()
        for row in rows:
            trace_id = str(row["trace_id"])
            if trace_id in inferred_trace_ids:
                continue
            try:
                payload = json.loads(_decode_payload(row["raw_json"]))
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
            # Frames are kept past their span's completion so an answer can be
            # replayed, which makes them the one append-only table that grows
            # with how much the models say. Retention has to reach them, and
            # ages them by when the model said it -- the only timestamp a
            # frame carries.
            connection.execute(
                "DELETE FROM trajectory_stream_frames WHERE timestamp_unix_nano < ?",
                (cutoff * 1_000_000_000,),
            )
            # A span is named so its frames can point at it, so its name is
            # worth nothing once retention has taken the last of them.
            connection.execute(
                """
                DELETE FROM trajectory_frame_spans
                WHERE NOT EXISTS (
                    SELECT 1 FROM trajectory_stream_frames AS frames
                    WHERE frames.span_ref = trajectory_frame_spans.span_ref
                )
                """
            )
            # Addressed content ages by when it was last referenced, not when
            # it first appeared: a tool definition restated all session long
            # keeps being refreshed, and so outlives the records that named it
            # only at the start.
            connection.execute(
                "DELETE FROM trajectory_sequences WHERE created_at < ?",
                (cutoff,),
            )
            connection.execute(
                "DELETE FROM trajectory_blobs WHERE created_at < ?",
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
            f"""
            SELECT {_CURRENT_RAW_JSON} AS raw_json
            FROM trajectory_current_records AS current
            {_CURRENT_ARCHIVE_JOIN}
            WHERE current.trace_id = ? AND current.span_id = ?
            """,
            (trace_id, span_id),
        ).fetchone()
        if row is None or row["raw_json"] is None:
            return None
        return _decode_payload(row["raw_json"])

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
    def _ensure_current_record_columns(connection: sqlite3.Connection) -> None:
        """Add the payload-size column an older database predates.

        Existing rows keep the 0 default and their own payload, which
        ``_CURRENT_RAW_SIZE`` measures directly, so no backfill is required.
        """
        rows = connection.execute("PRAGMA table_info(trajectory_current_records)").fetchall()
        columns = {str(row["name"]) for row in rows}
        if "raw_size_bytes" not in columns:
            connection.execute(
                "ALTER TABLE trajectory_current_records"
                " ADD COLUMN raw_size_bytes INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _drop_superseded_frame_table(connection: sqlite3.Connection) -> None:
        """Discard a frame table that names a span on every one of its rows.

        Frames are the one thing here with a short life: retention clears them
        within days, and a reader that loses them resumes from the records,
        which are untouched. Carrying the old shape forward would mean moving
        every row of it to buy back something that expires on its own.

        A reader holding a watermark from the discarded table sees one beyond
        what the file now has and starts over, which is the same path it takes
        for any rebuilt database.

        This runs before the schema script, which cannot create an index on a
        column the old table does not have.
        """
        rows = connection.execute("PRAGMA table_info(trajectory_stream_frames)").fetchall()
        columns = {str(row["name"]) for row in rows}
        if "trace_id" in columns:
            connection.execute("DROP TABLE trajectory_stream_frames")

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
        frame_watermarks: dict[tuple[str, str], int] | None = None,
    ) -> tuple[CommittedTraceUpdate, ...]:
        frame_seqs = frame_watermarks or {}
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
                session_id = str(row["session_id"])
                updates.append(
                    CommittedTraceUpdate(
                        session_id=session_id,
                        trace_id=trace_id,
                        revision=int(row["revision"]),
                        store_epoch=store_epoch,
                        lifecycle=(
                            str(lifecycle_row["lifecycle"])
                            if lifecycle_row is not None
                            else "final"
                        ),
                        frame_seq=frame_seqs.get((session_id, trace_id), 0),
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
                    f"""
                    SELECT current.trace_id AS trace_id,
                           current.start_time_unix_nano AS start_time_unix_nano,
                           current.change_seq AS change_seq,
                           {_CURRENT_RAW_JSON} AS raw_json
                    FROM trajectory_current_records AS current
                    {_CURRENT_ARCHIVE_JOIN}
                    WHERE current.session_id = ? AND current.change_seq > ?
                    ORDER BY current.change_seq ASC
                    """,
                    (session_id, after_revision),
                ) as statement:
                    rows = await statement.fetchall()
                for row in rows:
                    if row["raw_json"] is None:
                        continue
                    fact = _request_usage_fact(
                        _decode_payload(row["raw_json"]),
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

    async def get_session_archive_records(
        self,
        session_id: str,
        *,
        rehydrate: bool = True,
    ) -> tuple[list[dict[str, Any]], str, int, dict[str, Any]]:
        """Read every current record for one session from one SQLite snapshot.

        Args:
            session_id: Session to export.
            rehydrate: Put referenced content back into each record, so the
                export is valid OTLP for a reader that knows nothing of this
                store. False exports references plus the dictionaries that
                resolve them: far smaller, still self-contained, but only
                readable by a tool that understands the addressing.

        Returns:
            The records, the store epoch, the session revision, and the
            resolution dictionaries -- empty when *rehydrate* is true, because
            the records then state their content directly.
        """
        connection = await self._connect(session_id)
        if connection is None:
            return [], _ABSENT_STORE_EPOCH, 0, {"sequences": {}, "blobs": {}}
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            revision = await _session_revision_watermark(connection, session_id)
            query = f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT current.trace_id AS trace_id,
                       current.span_id AS span_id,
                       current.parent_span_id AS parent_span_id,
                       current.session_id AS session_id,
                       current.request_id AS request_id,
                       current.run_id AS run_id,
                       current.agent_mode AS agent_mode,
                       current.lifecycle AS lifecycle,
                       current.record_revision AS record_revision,
                       current.change_seq AS change_seq,
                       current.start_time_unix_nano AS start_time_unix_nano,
                       current.observed_time_unix_nano AS observed_time_unix_nano,
                       current.end_time_unix_nano AS end_time_unix_nano,
                       current.schema_version AS schema_version,
                       current.source AS source,
                       current.created_at AS created_at,
                       {_CURRENT_RAW_JSON} AS raw_json,
                       current.raw_sha256 AS raw_sha256,
                       current.update_kind AS update_kind
                FROM trajectory_current_records AS current
                {_CURRENT_ARCHIVE_JOIN}
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = current.trace_id
                WHERE current.session_id = ?
                ORDER BY current.start_time_unix_nano ASC,
                         current.trace_id ASC,
                         current.span_id ASC
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
        records = [_archive_record_from_row(row) for row in rows]
        head_hashes: set[str] = set()
        for record in records:
            for reference in (record.get("sequences") or {}).values():
                if reference.get("hash"):
                    head_hashes.add(str(reference["hash"]))
        heads = sorted(head_hashes)
        empty: dict[str, Any] = {"sequences": {}, "blobs": {}}
        if not heads:
            return records, store_epoch, revision, empty
        resolved = await self.resolve_sequences(session_id, heads, since_revision=0)
        if resolved is None:
            resolved = empty
        if not rehydrate:
            return records, store_epoch, revision, resolved
        chains = {key: list(value) for key, value in resolved["sequences"].items()}
        blobs = dict(resolved["blobs"])
        rebuilt: list[dict[str, Any]] = []
        for record in records:
            raw = base64.b64decode(record["raw_json_base64"])
            restored = _rehydrate_payload(raw, chains, blobs)
            if restored is raw:
                rebuilt.append(record)
                continue
            entry = dict(record)
            entry["raw_json_base64"] = base64.b64encode(restored).decode("ascii")
            try:
                entry["otlp"] = _strict_otlp_payload(restored)
                entry["raw_valid"] = True
            except (RecursionError, TypeError, ValueError, OverflowError):
                entry["otlp"] = None
                entry["raw_valid"] = False
            entry.pop("sequences", None)
            rebuilt.append(entry)
        return rebuilt, store_epoch, revision, empty

    async def list_subjects(
        self,
        session_id: str,
        *,
        after_revision: int = 0,
    ) -> tuple[list[dict[str, Any]], str, int]:
        """Summarize every execution subject that owns a chain in one session.

        Args:
            session_id: Session to summarize.
            after_revision: Return only subjects that changed past this
                change_seq, which is how a poller asks for what is new.

        Returns:
            The subject summaries, the store epoch, and the session watermark.
        """
        connection = await self._connect(session_id)
        if connection is None:
            return [], _ABSENT_STORE_EPOCH, 0
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            watermark = await _session_revision_watermark(connection, session_id)
            async with connection.execute(
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT records.execution_subject_id AS subject_id,
                       MAX(records.execution_subject_display_name) AS display_name,
                       MAX(records.execution_subject_kind) AS kind,
                       MAX(records.execution_subject_parent_id) AS parent_id,
                       COUNT(*) AS record_count,
                       COUNT(DISTINCT records.trace_id) AS trace_count,
                       MIN(records.start_time_unix_nano) AS first_start_time_unix_nano,
                       MAX(records.observed_time_unix_nano) AS last_observed_time_unix_nano,
                       MIN(records.change_seq) AS first_revision,
                       MAX(records.change_seq) AS revision,
                       SUM(records.has_error) AS error_count,
                       SUM(CASE WHEN records.lifecycle = 'running' THEN 1 ELSE 0 END) AS running_count
                FROM trajectory_current_records AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ?
                GROUP BY records.execution_subject_id
                HAVING MAX(records.change_seq) > ?
                ORDER BY MIN(records.start_time_unix_nano) ASC,
                         records.execution_subject_id ASC
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                    max(0, int(after_revision)),
                ),
            ) as statement:
                rows = await statement.fetchall()
        finally:
            await connection.rollback()
            await connection.close()
        return [_subject_summary_from_row(row) for row in rows], store_epoch, watermark

    async def get_subject_records(
        self,
        session_id: str,
        subject_id: str,
        *,
        since_revision: int,
        limit: int,
        max_bytes: int = DEFAULT_DETAIL_MAX_BYTES,
    ) -> dict[str, Any] | None:
        """Read one page of an execution subject's chain, in commit order.

        The chain is keyed by (session, subject) because that is what Agent
        Core commits against and what the viewer replays. Paging advances
        along it rather than across it, so the window a page ends on is the
        base the next page's first delta applies to.

        Args:
            session_id: Session owning the chain.
            subject_id: Execution subject owning the chain.
            since_revision: Highest change_seq the caller already holds.
            limit: Maximum records in this page.
            max_bytes: Payload budget for this page.

        Returns:
            One page of the chain, or None when the subject has no records.
        """
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
                FROM trajectory_current_records AS records
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = records.trace_id
                WHERE records.session_id = ? AND records.execution_subject_id = ?
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                    subject_id,
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
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT current.change_seq AS ingest_seq,
                       current.trace_id AS trace_id,
                       current.span_id AS span_id,
                       current.record_revision AS record_revision,
                       current.lifecycle AS lifecycle,
                       'upsert' AS operation,
                       current.observed_time_unix_nano AS observed_time_unix_nano,
                       {_CURRENT_RAW_SIZE} AS raw_size_bytes
                FROM trajectory_current_records AS current
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = current.trace_id
                WHERE current.session_id = ?
                  AND current.execution_subject_id = ?
                  AND current.change_seq > ?
                ORDER BY current.change_seq ASC
                LIMIT ?
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                    subject_id,
                    effective_since,
                    limit + 1,
                ),
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
                    f"""
                    WITH {_ELIGIBLE_TRACES_CTE}
                    SELECT current.change_seq AS ingest_seq,
                           current.trace_id AS trace_id,
                           current.span_id AS span_id,
                           current.record_revision AS record_revision,
                           current.lifecycle AS lifecycle,
                           'upsert' AS operation,
                           current.observed_time_unix_nano AS observed_time_unix_nano,
                           {_CURRENT_RAW_SIZE} AS raw_size_bytes,
                           {_CURRENT_RAW_JSON} AS raw_json
                    FROM trajectory_current_records AS current
                    {_CURRENT_ARCHIVE_JOIN}
                    INNER JOIN eligible_traces
                        ON eligible_traces.trace_id = current.trace_id
                    WHERE current.session_id = ?
                      AND current.execution_subject_id = ?
                      AND current.change_seq > ?
                      AND current.change_seq <= ?
                    ORDER BY current.change_seq ASC
                    """,
                    (
                        *_trajectory_scope_params(),
                        session_id,
                        subject_id,
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

    async def resolve_sequences(
        self,
        session_id: str,
        seq_hashes: Sequence[str],
        *,
        since_revision: int = 0,
    ) -> dict[str, Any] | None:
        """Resolve chains into their elements, and the content a reader lacks.

        The walk is one recursive query over every requested chain at once,
        and it deduplicates as it goes: several spans of one conversation
        share a prefix, so that prefix is visited once however many of them
        asked for it.

        Args:
            session_id: Session owning the chains.
            seq_hashes: Chain heads to resolve.
            since_revision: Revision the reader already holds. Content first
                seen at or before it is assumed present and is not resent;
                a reader that lost its cache asks for it by hash instead.

        Returns:
            ``sequences`` mapping each head to its element hashes in order,
            and ``blobs`` mapping hash to content for what the reader lacks.
        """
        wanted = [h for h in dict.fromkeys(seq_hashes) if h]
        if not wanted:
            return {"sequences": {}, "blobs": {}}
        connection = await self._connect(session_id)
        if connection is None:
            return None
        try:
            placeholders = ",".join("?" for _ in wanted)
            async with connection.execute(
                f"""
                WITH RECURSIVE reachable(seq_hash, prev_hash, blob_hash, depth) AS (
                    SELECT seq_hash, prev_hash, blob_hash, depth
                    FROM trajectory_sequences
                    WHERE seq_hash IN ({placeholders})
                    UNION
                    SELECT s.seq_hash, s.prev_hash, s.blob_hash, s.depth
                    FROM trajectory_sequences AS s
                    JOIN reachable AS r ON s.seq_hash = r.prev_hash
                )
                SELECT seq_hash, prev_hash, blob_hash, depth FROM reachable
                """,
                tuple(wanted),
            ) as statement:
                rows = await statement.fetchall()
            nodes = {
                str(row["seq_hash"]): (
                    None if row["prev_hash"] is None else str(row["prev_hash"]),
                    str(row["blob_hash"]),
                )
                for row in rows
            }
            sequences: dict[str, list[str]] = {}
            needed: list[str] = []
            for head in wanted:
                elements: list[str] = []
                cursor = head
                # Walking in memory costs one dictionary lookup per element,
                # and a chain that lost an ancestor simply stops early rather
                # than looping.
                while cursor is not None and cursor in nodes:
                    previous, blob_hash = nodes[cursor]
                    elements.append(blob_hash)
                    cursor = previous
                elements.reverse()
                if elements:
                    sequences[head] = elements
                    needed.extend(elements)
            blobs: dict[str, str] = {}
            unique_needed = list(dict.fromkeys(needed))
            for start in range(0, len(unique_needed), _SEQUENCE_FETCH_CHUNK):
                chunk = unique_needed[start:start + _SEQUENCE_FETCH_CHUNK]
                marks = ",".join("?" for _ in chunk)
                async with connection.execute(
                    f"""
                    SELECT blob_hash, content, first_change_seq
                    FROM trajectory_blobs
                    WHERE blob_hash IN ({marks}) AND first_change_seq > ?
                    """,
                    (*chunk, max(0, int(since_revision))),
                ) as statement:
                    for row in await statement.fetchall():
                        blobs[str(row["blob_hash"])] = _decode_payload(
                            row["content"]
                        ).decode("utf-8", "replace")
        finally:
            await connection.close()
        return {"sequences": sequences, "blobs": blobs}

    async def get_stream_frames(
        self,
        session_id: str,
        *,
        since_frame_seq: int,
        limit: int,
    ) -> dict[str, Any] | None:
        """Read one page of a session's stream frames, in commit order.

        This is how a reader that fell behind catches up. Frames are additive,
        so a reader resumes from the last one it holds and replays forward
        rather than waiting for the answer to finish.

        Frames are filtered through the same trace eligibility as records: a
        frame belongs to a span, and a span the reader may not see must not
        leak its content through this path. The session needs no filter of its
        own -- it selects the file, and every frame in that file is its own.

        Args:
            session_id: Session to read frames for; it resolves the database.
            since_frame_seq: Highest frame_seq the caller already holds.
            limit: Maximum frames in this page.

        Returns:
            One page of frames, or None when the session has no database.
        """
        connection = await self._connect(session_id)
        if connection is None:
            return None
        try:
            await connection.execute("BEGIN")
            aggregate = await _fetch_one(
                connection,
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT MIN(frames.frame_seq) AS first_frame_seq,
                       MAX(frames.frame_seq) AS current_frame_seq
                FROM trajectory_stream_frames AS frames
                INNER JOIN trajectory_frame_spans AS spans
                    ON spans.span_ref = frames.span_ref
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = spans.trace_id
                """,
                _trajectory_scope_params(),
            )
            if aggregate is None or aggregate["current_frame_seq"] is None:
                return {
                    "frame_seq": 0,
                    "reset": False,
                    "frames": [],
                    "has_more": False,
                    "next_since_frame_seq": since_frame_seq,
                }
            first_frame_seq = int(aggregate["first_frame_seq"])
            current_frame_seq = int(aggregate["current_frame_seq"])
            # Ahead of the store means the database was rebuilt; behind its
            # first frame means retention removed what the reader wanted next.
            # Either way the reader has to start over rather than resume into
            # a gap it cannot see.
            reset = since_frame_seq > current_frame_seq or (
                since_frame_seq > 0 and since_frame_seq < first_frame_seq - 1
            )
            effective_since = 0 if reset else since_frame_seq
            async with connection.execute(
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT frames.frame_seq AS frame_seq,
                       spans.trace_id AS trace_id,
                       spans.span_id AS span_id,
                       spans.execution_subject_id AS execution_subject_id,
                       frames.sequence AS sequence,
                       frames.kind AS kind,
                       frames.text AS text,
                       frames.tool_call_id AS tool_call_id,
                       frames.tool_name AS tool_name,
                       frames.arguments_delta AS arguments_delta,
                       frames.timestamp_unix_nano AS timestamp_unix_nano
                FROM trajectory_stream_frames AS frames
                INNER JOIN trajectory_frame_spans AS spans
                    ON spans.span_ref = frames.span_ref
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = spans.trace_id
                WHERE frames.frame_seq > ?
                ORDER BY frames.frame_seq ASC
                LIMIT ?
                """,
                (
                    *_trajectory_scope_params(),
                    effective_since,
                    limit + 1,
                ),
            ) as statement:
                rows = await statement.fetchall()
        finally:
            await connection.rollback()
            await connection.close()
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_since_frame_seq = effective_since
        if selected:
            next_since_frame_seq = int(selected[-1]["frame_seq"])
        return {
            "frame_seq": current_frame_seq,
            "reset": reset,
            "frames": [_stream_frame_from_row(row) for row in selected],
            "has_more": has_more,
            "next_since_frame_seq": next_since_frame_seq,
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
        return _decode_payload(row["raw_json"]) if row is not None else None

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
    # A span status reaches OTLP JSON as {"status":{"code":...}}, and an
    # unset status carries no code at all, so a payload without the key cannot
    # describe an error. This scan is C-level, whereas the parse it guards is
    # dominated by a byte-wise nesting check costing roughly fifteen times the
    # JSON decode. The writer runs this once per record, which made it the
    # single largest cost of a batch. A key present for any other reason only
    # falls through to the parse below, which still decides the answer.
    if _STATUS_CODE_KEY not in raw_json:
        return False
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
    attributes = {
        str(attribute.get("key")): _otlp_attribute_value(attribute.get("value"))
        for attribute in spans[0].get("attributes", [])
        if isinstance(attribute, dict) and isinstance(attribute.get("key"), str)
    }
    if attributes.get("gen_ai.operation.name") not in {"chat", "generate_content", "text_completion"}:
        return None
    inference_id = str(attributes.get("openjiuwen.inference.id") or "").strip()
    if not inference_id:
        return None
    subject_id = str(attributes.get("openjiuwen.execution.subject.id") or "main").strip()
    usage_keys = {
        "input": ("gen_ai.usage.input_tokens",),
        "cacheRead": ("gen_ai.usage.cache_read.input_tokens",),
        "cacheWrite": ("gen_ai.usage.cache_write.input_tokens",),
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


def _parse_sequence_reference(value: object) -> tuple[str, int] | None:
    """Return the chain a stored attribute names, or None for a plain value."""
    if not isinstance(value, str) or not value.startswith(_SEQUENCE_REFERENCE_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 4 or parts[1] != _SEQUENCE_REFERENCE_VERSION:
        return None
    try:
        depth = int(parts[3])
    except ValueError:
        return None
    if not parts[2] or depth < 0:
        return None
    return parts[2], depth


def _rebuilt_sequence_value(elements: list[str], blobs: dict[str, str]) -> str | None:
    """Return the attribute value a chain states, or None if an element is gone.

    Always an array: a chain is built only from one, so it rebuilds into one
    at any depth. Nothing here inspects the count.
    """
    parts: list[str] = []
    for element in elements:
        content = blobs.get(element)
        if content is None:
            return None
        parts.append(content)
    if not parts:
        return None
    return "[" + ",".join(parts) + "]"


def _rehydrate_payload(
    raw_json: bytes,
    chains: dict[str, list[str]],
    blobs: dict[str, str],
) -> bytes:
    """Put the content back where a stored record kept only a reference.

    An export has to be valid OTLP on its own: it is read by tools that know
    nothing of this store's addressing, and a reference they cannot resolve is
    worse than the bytes it saved.

    Args:
        raw_json: The stored payload, carrying references.
        chains: Element hashes of each chain, in order.
        blobs: Element content by hash.

    Returns:
        The payload with every resolvable reference replaced, or the original
        bytes when nothing needed replacing. A reference whose content is gone
        is left as it is rather than dropping the span that carries it.
    """
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return raw_json
    if not isinstance(payload, dict):
        return raw_json
    changed = False
    for resource_span in payload.get("resourceSpans") or ():
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans") or ():
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or ():
                if not isinstance(span, dict):
                    continue
                for attribute in span.get("attributes") or ():
                    if not isinstance(attribute, dict):
                        continue
                    value = attribute.get("value")
                    if not isinstance(value, dict):
                        continue
                    parsed = _parse_sequence_reference(value.get("stringValue"))
                    if parsed is None:
                        continue
                    elements = chains.get(parsed[0])
                    if elements is None:
                        continue
                    rebuilt = _rebuilt_sequence_value(elements, blobs)
                    if rebuilt is None:
                        continue
                    value["stringValue"] = rebuilt
                    changed = True
    if not changed:
        return raw_json
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _record_sequence_references(otlp: Any) -> dict[str, dict[str, Any]]:
    """Return the chains one record refers to, keyed by attribute.

    A reader compares these hashes against what it already holds: an equal
    hash means the attribute did not change, so nothing about it needs to be
    fetched, parsed or projected again.
    """
    references: dict[str, dict[str, Any]] = {}
    if not isinstance(otlp, dict):
        return references
    for resource_span in otlp.get("resourceSpans") or ():
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans") or ():
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or ():
                if not isinstance(span, dict):
                    continue
                for attribute in span.get("attributes") or ():
                    if not isinstance(attribute, dict):
                        continue
                    value = attribute.get("value")
                    if not isinstance(value, dict):
                        continue
                    parsed = _parse_sequence_reference(value.get("stringValue"))
                    if parsed is None:
                        continue
                    references[str(attribute.get("key") or "")] = {
                        "hash": parsed[0],
                        "depth": parsed[1],
                    }
    return references


def _detail_record_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    raw_json = _decode_payload(row["raw_json"])
    try:
        otlp = _strict_otlp_payload(raw_json)
    except (RecursionError, TypeError, ValueError, OverflowError):
        otlp = None
    references = _record_sequence_references(otlp)
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
        **({} if not references else {"sequences": references}),
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


def _stream_frame_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Shape one stream frame row for the wire.

    Text fields are passed through untouched: a frame's text is an increment
    of an answer, and its spaces are content rather than formatting.
    """
    frame: dict[str, Any] = {
        "frame_seq": int(row["frame_seq"]),
        "trace_id": str(row["trace_id"]),
        "span_id": str(row["span_id"]),
        "subject_id": str(row["execution_subject_id"]),
        "sequence": int(row["sequence"]),
        "kind": _FRAME_KIND_NAMES[int(row["kind"])],
        "timestamp_unix_nano": int(row["timestamp_unix_nano"]),
    }
    for column in ("text", "tool_call_id", "tool_name", "arguments_delta"):
        value = row[column]
        if value is not None:
            frame[column] = str(value)
    return frame


def _subject_summary_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Describe one execution subject's chain without loading any payload."""
    return {
        "subject_id": str(row["subject_id"]),
        "display_name": row["display_name"],
        "kind": row["kind"],
        "parent_id": row["parent_id"],
        "record_count": int(row["record_count"]),
        "trace_count": int(row["trace_count"]),
        "first_start_time_unix_nano": str(row["first_start_time_unix_nano"]),
        "last_observed_time_unix_nano": str(row["last_observed_time_unix_nano"]),
        "first_revision": int(row["first_revision"]),
        "revision": int(row["revision"]),
        "has_error": int(row["error_count"] or 0) > 0,
        "running": int(row["running_count"] or 0) > 0,
    }


def _archive_record_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Build one lossless, version-independent archive current record."""
    raw_json = _decode_payload(row["raw_json"])
    try:
        otlp = _strict_otlp_payload(raw_json)
    except (RecursionError, TypeError, ValueError, OverflowError):
        otlp = None
    references = _record_sequence_references(otlp)
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
        **({} if not references else {"sequences": references}),
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
]
