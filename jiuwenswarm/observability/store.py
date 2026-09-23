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
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from openjiuwen.extensions.observability.content_addressing import (
    build_sequence,
    parse_sequence_reference,
    rebuild_value,
)

from jiuwenswarm.common.mode_matrix import (
    SINGLE_AGENT_CANONICAL_MODES,
    TEAM_CANONICAL_MODES,
)
from jiuwenswarm.observability.config import (
    DEFAULT_DETAIL_MAX_BYTES,
    DEFAULT_DISCARD_FINAL_SPAN_FRAMES,
    database_files,
    session_database_path,
)
from jiuwenswarm.observability.models import (
    CommittedTraceUpdate,
    StreamFrameData,
    TraceRecordData,
    WriteBatchResult,
)
from jiuwenswarm.observability.otlp_payload import (
    MAX_SAFE_INTEGER,
    int64_attribute_value,
    parse_otlp_payload,
    strict_otlp_payload,
)
from jiuwenswarm.observability.retention import (
    RetentionCheckpoint,
    RetentionRow,
    advance_checkpoint,
    checkpoint_sequence_heads,
    plan_session_retention,
    record_view_facts,
)

logger = logging.getLogger(__name__)

# A database written under any other version is discarded, not migrated:
# trajectories are diagnostic data with a retention window of days, and every
# migration kept here was code that outlived the data it existed for.
_SCHEMA_VERSION = 6
_BUSY_TIMEOUT_MS = 5000
# Serialized name of the OTLP span status code, used to skip parsing a payload
# that cannot carry an error status. See ``_record_has_error``.
_STATUS_CODE_KEY = b'"code"'
_ABSENT_STORE_EPOCH = "absent"
# Archive line contract. Version 3 is a JSONL stream of content-addressed
# lines in commit order; see ``AsyncTrajectoryReader.iter_session_archive_lines``.
TRAJECTORY_ARCHIVE_FORMAT = "openjiuwen.trajectory.archive"
TRAJECTORY_ARCHIVE_VERSION = 3
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
    -- What retention groups and pages this record by, read from its payload
    -- once when it is written: the subject the viewer lists it under, whether
    -- the viewer projects it at all, and the turn it states.
    view_subject_id TEXT NOT NULL,
    view_subject_kind TEXT NOT NULL,
    view_subject_session_id TEXT,
    view_projected INTEGER NOT NULL DEFAULT 0,
    turn_id TEXT,
    turn_number INTEGER,
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
--
-- A frame lives only as long as the answer it stands in for is unfinished. The
-- terminal record of its span states that answer in full, so by default the
-- frames go as that record is committed; ``discard_final_span_frames: false``
-- keeps them for the lifetime of the turn page instead, which is what
-- replaying a finished answer frame by frame would need.
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
-- Replaying one answer, discarding the frames a terminal record supersedes,
-- and discarding those of a span that turned out to be incomplete all address
-- frames by the span that produced them.
CREATE INDEX IF NOT EXISTS idx_trajectory_frames_span_ref
    ON trajectory_stream_frames(span_ref, sequence);

-- One piece of content, stored once however many records state it. The GenAI
-- convention has every model call restate its whole input; this is where that
-- repetition stops. Retention keeps content while trajectory_sequence_refs
-- still reaches it, whenever it was last restated.
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

-- Which chains each record and each retention checkpoint refers to. Content
-- is kept for as long as something still refers to it, so retention reclaims
-- what nothing reaches any more instead of what has merely not been restated
-- for a while.
CREATE TABLE IF NOT EXISTS trajectory_sequence_refs (
    owner_kind TEXT NOT NULL,
    owner_id   TEXT NOT NULL,
    seq_hash   TEXT NOT NULL,
    PRIMARY KEY (owner_kind, owner_id, seq_hash)
);

-- The derived state retention left behind for one execution subject of one
-- session, so the turns that remain render as they did before older ones
-- were removed. The contract of state_json is documented in
-- jiuwenswarm/observability/retention.py.
CREATE TABLE IF NOT EXISTS trajectory_retention_checkpoints (
    session_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    boundary_turn_id TEXT,
    boundary_change_seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, subject_id)
);
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
# join: the empty BLOB is what marks a row whose payload is in the archive.
_CURRENT_ARCHIVE_JOIN = """
    LEFT JOIN otlp_span_records AS archive
        ON archive.trace_id = current.trace_id
       AND archive.span_id = current.span_id
"""
_CURRENT_RAW_JSON = "COALESCE(NULLIF(current.raw_json, X''), archive.raw_json)"

# OTLP JSON repeats its key names on every span, event and attribute, so it
# compresses several-fold. Level 3 sits at the knee of the curve for this data:
# measured against real payloads it reaches 3.7x for 0.65 ms per record, where
# level 6 spends 1.35 ms to reach 4.1x. Decompression costs 0.07 ms either way.
# SQLite's default bound-variable limit is 999; stay well inside it when
# fetching the elements one page of records refers to.
_SEQUENCE_FETCH_CHUNK = 400
_PAYLOAD_COMPRESSION_LEVEL = 3
# Records read per cursor batch while exporting an archive. The content a batch
# refers to is resolved in one round of queries, so this bounds both the rows
# and the chain nodes held at once.
_ARCHIVE_RECORD_BATCH = 500
# Every chain node reachable from a set of heads, walking prev_hash back to the
# root. ``placeholders`` is filled with one bound variable per head.
_REACHABLE_NODES_SQL = """
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
"""
# Owners in trajectory_sequence_refs.
_RECORD_OWNER_KIND = "record"
_CHECKPOINT_OWNER_KIND = "checkpoint"
# Attribute key a checkpoint's message lists are addressed under. The key names
# no attribute; it only labels the sequence while it is being built.
_CHECKPOINT_MESSAGES_KEY = "openjiuwen.retention.messages"


def _encode_payload(raw_json: bytes) -> bytes:
    """Compress one payload for storage."""
    return zlib.compress(raw_json, _PAYLOAD_COMPRESSION_LEVEL)


def _decode_payload(stored: bytes | None) -> bytes:
    """Return the original payload of one stored, compressed BLOB.

    An empty BLOB is the marker of a final row whose payload lives in the
    archive table, and decodes to nothing.
    """
    if not stored:
        return b""
    return zlib.decompress(bytes(stored))


class TrajectoryStore:
    """Single-threaded SQLite writer that preserves raw record bytes unchanged."""

    def __init__(
        self,
        database_path: Path,
        *,
        retention_days: int = 7,
        discard_final_span_frames: bool = DEFAULT_DISCARD_FINAL_SPAN_FRAMES,
    ) -> None:
        self.database_path = Path(database_path)
        self.retention_days = max(1, int(retention_days))
        self.discard_final_span_frames = bool(discard_final_span_frames)
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open the writer connection and initialize the idempotent schema."""
        if self._connection is not None:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._open_writer_connection()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, _SCHEMA_VERSION):
                connection.close()
                self._discard_incompatible_database(version)
                connection = self._open_writer_connection()
            connection.execute("PRAGMA foreign_keys=ON")
            # Retention frees whole turns at a time, and only an incremental
            # vacuum hands those pages back to the file system. The mode can be
            # chosen only before the first table exists, so it precedes WAL
            # and the schema; on an existing database it changes nothing.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(_SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._initialize_store_state(connection)
            self._abandon_running_current(connection)
            connection.commit()
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def _open_writer_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=_BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return connection

    def _discard_incompatible_database(self, version: int) -> None:
        """Delete a database written under another schema version.

        The file and its WAL sidecars go together: a WAL left behind would be
        replayed into the empty database that replaces them.
        """
        logger.warning(
            "Trajectory database schema %s is not %s; discarding it: path=%s",
            version,
            _SCHEMA_VERSION,
            self.database_path,
        )
        for path in database_files(self.database_path):
            path.unlink(missing_ok=True)

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
            # Append first, then discard: one flush window can carry both the
            # last frames of a span and the record that ends it.
            self._discard_final_span_frames(connection, records)
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

    def _append_stream_frames(
        self,
        connection: sqlite3.Connection,
        frames: Sequence[StreamFrameData],
    ) -> dict[tuple[str, str], int]:
        """Append every frame and report the highest seq per session and trace.

        Frames are inserted, never merged: each states an increment that no
        later frame repeats. The rowid of the last insert for a trace is its
        watermark, because the sequence is monotonic within a transaction.

        Frames and records reach the writer through queues of their own, so a
        span's record can be committed before the last of its frames arrives.
        Those late frames are dropped rather than stored: their span already
        states its complete output, and no reader consults the frames of a span
        that ended. Without this they would be stored with nothing left to
        delete them -- ``_discard_final_span_frames`` runs as a record lands,
        and that record has already landed.

        Args:
            connection: The open write transaction.
            frames: Frames to append, in the order they were produced.

        Returns:
            Highest committed ``frame_seq`` keyed by session and trace.
        """
        watermarks: dict[tuple[str, str], int] = {}
        # A span maps to its ref, or to None once it is known to have ended.
        span_refs: dict[tuple[str, str], int | None] = {}
        for frame in frames:
            identity = (frame.trace_id, frame.span_id)
            if identity in span_refs:
                span_ref = span_refs[identity]
            else:
                span_ref = self._resolve_frame_span(connection, frame)
                span_refs[identity] = span_ref
            if span_ref is None:
                continue
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

    def _resolve_frame_span(
        self,
        connection: sqlite3.Connection,
        frame: StreamFrameData,
    ) -> int | None:
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
            The integer this database names that span by, or None when the span
            already has a terminal record and its frames are to be dropped.
        """
        identity = (frame.trace_id, frame.span_id)
        if self.discard_final_span_frames and _has_final_record(connection, *identity):
            return None
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

    def _discard_final_span_frames(
        self,
        connection: sqlite3.Connection,
        records: Sequence[TraceRecordData],
    ) -> None:
        """Drop the frames of every span this batch brought to a terminal state.

        A frame is a stand-in for an answer still being written. The record of
        a finished span states that answer in full, so from the moment it is
        committed the frames of that span are read by nothing: the reader drops
        its own copy of them, the detail read never consults them, and an
        archive excludes them. Keeping them would leave the largest table in
        the database holding only content no code path reaches.
        ``_delete_orphan_frames`` does not reach them either -- it ages out the
        frames of spans that never produced a record, and these have one.

        This runs after the batch's own frames are appended, so a flush window
        that carries both a span's last frames and its record still discards
        them. The trace's frame watermark is left as appended: it reports the
        revision frames were last committed at, and a reader that comes back
        for frames now gone is told to reset, which costs it the frame state of
        a span whose record already supersedes it.

        Args:
            connection: The open write transaction.
            records: Records committed in this batch, terminal or not.
        """
        if not self.discard_final_span_frames:
            return
        finished = {
            (record.trace_id, record.span_id)
            for record in records
            if record.lifecycle == "final"
        }
        if not finished:
            return
        # Deleting by span_ref keeps the frame delete on the index the frames
        # are clustered by, and resolving the ref first means a span that never
        # streamed costs one lookup instead of a scan.
        refs: list[tuple[int]] = []
        for identity in finished:
            rows = connection.execute(
                "SELECT span_ref FROM trajectory_frame_spans WHERE trace_id = ? AND span_id = ?",
                identity,
            )
            refs.extend((int(row["span_ref"]),) for row in rows)
        if not refs:
            return
        connection.executemany("DELETE FROM trajectory_stream_frames WHERE span_ref = ?", refs)
        # The span was named only so its frames could point at it. Foreign keys
        # are on, so this must follow the frames it owns.
        connection.executemany("DELETE FROM trajectory_frame_spans WHERE span_ref = ?", refs)

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
        change_seq = _next_change_seq(connection)
        # A final span is already archived in otlp_span_records under the same
        # identity, so storing the payload again here doubled the database for
        # no recoverable information. Running spans have no archive row yet and
        # keep theirs. The size is recorded either way, because the detail
        # reader budgets pages by it before it fetches any payload.
        is_final = record.lifecycle == "final"
        stored_raw_json = b"" if is_final else _encode_payload(record.raw_json)
        view_facts = record_view_facts(record.raw_json, record.trace_id, record.span_id)
        connection.execute(
            """
            INSERT INTO trajectory_current_records (
                trace_id, span_id, parent_span_id, session_id, request_id,
                run_id, agent_mode, execution_subject_id,
                execution_subject_display_name, execution_subject_kind,
                execution_subject_parent_id, lifecycle, record_revision, change_seq,
                start_time_unix_nano, observed_time_unix_nano,
                end_time_unix_nano, schema_version, source, created_at,
                has_error, raw_json, raw_size_bytes, raw_sha256, update_kind,
                view_subject_id, view_subject_kind, view_subject_session_id,
                view_projected, turn_id, turn_number
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
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
                update_kind = excluded.update_kind,
                view_subject_id = excluded.view_subject_id,
                view_subject_kind = excluded.view_subject_kind,
                view_subject_session_id = excluded.view_subject_session_id,
                view_projected = excluded.view_projected,
                turn_id = excluded.turn_id,
                turn_number = excluded.turn_number
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
                view_facts.subject_id,
                view_facts.subject_kind,
                view_facts.subject_session_id,
                int(view_facts.projected),
                view_facts.turn_id,
                view_facts.turn_number,
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
        references: set[tuple[str, str]] = set()
        for record in records:
            for sequence in record.sequences:
                references.add((_record_owner_id(record.trace_id, record.span_id), sequence.seq_hash))
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
            batch_change_seq = _stored_max_change_seq(connection)
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
        if references:
            # A running record restates its references on every revision and
            # may drop one it had; the stale reference only keeps content a
            # little longer, until the record itself is retired.
            connection.executemany(
                """
                INSERT INTO trajectory_sequence_refs (owner_kind, owner_id, seq_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(owner_kind, owner_id, seq_hash) DO NOTHING
                """,
                [
                    (_RECORD_OWNER_KIND, owner_id, seq_hash)
                    for owner_id, seq_hash in sorted(references)
                ],
            )

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
                    _next_change_seq(connection),
                    observed_time,
                    created_at,
                    row["trace_id"],
                    row["span_id"],
                ),
            )
        return len(rows)

    def delete_expired(self, *, now: int | None = None) -> int:
        """Retire the oldest expired turn pages of every session, as whole pages.

        One transaction removes the pages ``retention.plan_session_retention``
        chooses, folds what they leave behind into each subject's checkpoint,
        and then reclaims whatever content no remaining record or checkpoint
        reaches. Readers see the removal as a new store epoch and rebuild from
        the checkpoints. The file shrinks by an incremental vacuum afterwards.

        Args:
            now: Unix second to measure the retention window from.

        Returns:
            The number of records removed.
        """
        connection = self._require_connection()
        cutoff = int(now if now is not None else time.time()) - self.retention_days * 86400
        try:
            connection.execute("BEGIN IMMEDIATE")
            removed = self._retire_expired_pages(connection, cutoff)
            if removed > 0:
                self._collect_unreachable_content(connection)
                self._rotate_store_epoch(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if removed > 0:
            connection.execute("PRAGMA incremental_vacuum").fetchall()
        return removed

    def _retire_expired_pages(self, connection: sqlite3.Connection, cutoff: int) -> int:
        """Remove expired turn pages and advance the checkpoints they leave.

        Args:
            connection: The open write transaction.
            cutoff: Unix second before which a record's last write has expired.

        Returns:
            The number of records removed.
        """
        # A page expires only once every record on it has, so a store without
        # a single expired record has nothing to plan.
        expired = connection.execute(
            "SELECT 1 FROM trajectory_current_records WHERE created_at < ? LIMIT 1",
            (cutoff,),
        ).fetchone()
        if expired is None:
            self._delete_orphan_frames(connection, cutoff)
            return 0
        rows_by_session: dict[str | None, list[RetentionRow]] = {}
        for row in connection.execute(
            """
            SELECT session_id, trace_id, span_id, parent_span_id, agent_mode,
                   view_subject_id, view_subject_kind, view_subject_session_id, view_projected,
                   turn_id, turn_number, start_time_unix_nano,
                   observed_time_unix_nano, lifecycle, created_at, change_seq
            FROM trajectory_current_records
            ORDER BY change_seq ASC
            """
        ):
            rows_by_session.setdefault(row["session_id"], []).append(_retention_row(row))
        resolver = _SequenceValueResolver(connection)
        removed = 0
        for session_id, session_rows in rows_by_session.items():
            if all(row.created_at >= cutoff for row in session_rows):
                continue
            plan = plan_session_retention(
                session_id,
                session_rows,
                _checkpoint_trace_turn_ids(connection, session_id),
                cutoff=cutoff,
                trajectory_modes=frozenset(_TRAJECTORY_MODE_VALUES),
            )
            if not plan.deleted_rows:
                continue
            # Only a session that loses pages pays for reading its checkpoints
            # back, message lists and all.
            checkpoints = self._load_checkpoints(connection, session_id, resolver)
            payloads = _retired_payloads(connection, plan.deleted_rows)
            updated: dict[str, RetentionCheckpoint] = {}
            for subject_id, retention in plan.groups.items():
                updated[subject_id] = advance_checkpoint(
                    checkpoints.get(subject_id),
                    retention,
                    payloads,
                    resolver.value,
                )
            for subject_id, usage in _retired_usage(plan.deleted_rows, payloads).items():
                checkpoint = updated.get(subject_id) or checkpoints.get(subject_id)
                if checkpoint is None:
                    checkpoint = RetentionCheckpoint(subject_id=subject_id)
                checkpoint.add_usage(usage)
                updated[subject_id] = checkpoint
            self._delete_retired_records(connection, plan.deleted_rows)
            self._write_checkpoints(connection, session_id, updated.values())
            removed += len(plan.deleted_rows)
        self._delete_orphan_frames(connection, cutoff)
        return removed

    @staticmethod
    def _load_checkpoints(
        connection: sqlite3.Connection,
        session_id: str | None,
        resolver: _SequenceValueResolver,
    ) -> dict[str, RetentionCheckpoint]:
        """Read one session's checkpoints, with their message lists resolved."""
        checkpoints: dict[str, RetentionCheckpoint] = {}
        for row in connection.execute(
            """
            SELECT subject_id, boundary_turn_id, boundary_change_seq, state_json
            FROM trajectory_retention_checkpoints
            WHERE session_id = ?
            """,
            (_checkpoint_session_key(session_id),),
        ):
            subject_id = str(row["subject_id"])
            checkpoints[subject_id] = RetentionCheckpoint.from_state(
                subject_id,
                json.loads(row["state_json"]),
                resolver.messages,
                boundary_turn_key=row["boundary_turn_id"],
                boundary_change_seq=int(row["boundary_change_seq"]),
            )
        return checkpoints

    @staticmethod
    def _delete_retired_records(
        connection: sqlite3.Connection,
        rows: Sequence[RetentionRow],
    ) -> None:
        """Remove retired records with their archive copies, conflicts, frames and references."""
        connection.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS retention_retired (
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                PRIMARY KEY (trace_id, span_id)
            )
            """
        )
        connection.execute("DELETE FROM temp.retention_retired")
        connection.executemany(
            "INSERT OR IGNORE INTO temp.retention_retired (trace_id, span_id) VALUES (?, ?)",
            [row.identity for row in rows],
        )
        retired = "(trace_id, span_id) IN (SELECT trace_id, span_id FROM temp.retention_retired)"
        for table in (
            "trajectory_current_records",
            "otlp_span_records",
            "otlp_record_conflicts",
        ):
            connection.execute(f"DELETE FROM {table} WHERE {retired}")
        connection.execute(
            f"""
            DELETE FROM trajectory_stream_frames
            WHERE span_ref IN (SELECT span_ref FROM trajectory_frame_spans WHERE {retired})
            """
        )
        connection.execute(f"DELETE FROM trajectory_frame_spans WHERE {retired}")
        connection.executemany(
            "DELETE FROM trajectory_sequence_refs WHERE owner_kind = ? AND owner_id = ?",
            [(_RECORD_OWNER_KIND, _record_owner_id(*row.identity)) for row in rows],
        )
        connection.execute("DELETE FROM temp.retention_retired")

    @staticmethod
    def _delete_orphan_frames(connection: sqlite3.Connection, cutoff: int) -> None:
        """Age out frames of spans that never produced a record.

        Frames of a span with a record are released by that record -- as it is
        committed, or with its turn page where the store is configured to keep
        them. A stream cut off before its span ended leaves frames no record
        will ever release, and those still age by when the model produced them.
        """
        connection.execute(
            """
            DELETE FROM trajectory_stream_frames
            WHERE timestamp_unix_nano < ?
              AND span_ref IN (
                  SELECT spans.span_ref FROM trajectory_frame_spans AS spans
                  WHERE NOT EXISTS (
                      SELECT 1 FROM trajectory_current_records AS current
                      WHERE current.trace_id = spans.trace_id
                        AND current.span_id = spans.span_id
                  )
              )
            """,
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

    @staticmethod
    def _write_checkpoints(
        connection: sqlite3.Connection,
        session_id: str | None,
        checkpoints: Iterable[RetentionCheckpoint],
    ) -> None:
        """Persist checkpoints, their message lists as content and their references."""
        session_key = _checkpoint_session_key(session_id)
        updated_at = int(time.time())
        for checkpoint in checkpoints:
            state = checkpoint.to_state(
                lambda messages: _store_message_sequence(connection, messages, updated_at),
            )
            connection.execute(
                """
                INSERT INTO trajectory_retention_checkpoints (
                    session_id, subject_id, boundary_turn_id,
                    boundary_change_seq, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, subject_id) DO UPDATE SET
                    boundary_turn_id = excluded.boundary_turn_id,
                    boundary_change_seq = excluded.boundary_change_seq,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_key,
                    checkpoint.subject_id,
                    checkpoint.boundary_turn_key,
                    checkpoint.boundary_change_seq,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    updated_at,
                ),
            )
            owner_id = _checkpoint_owner_id(session_key, checkpoint.subject_id)
            connection.execute(
                "DELETE FROM trajectory_sequence_refs WHERE owner_kind = ? AND owner_id = ?",
                (_CHECKPOINT_OWNER_KIND, owner_id),
            )
            connection.executemany(
                """
                INSERT INTO trajectory_sequence_refs (owner_kind, owner_id, seq_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(owner_kind, owner_id, seq_hash) DO NOTHING
                """,
                [
                    (_CHECKPOINT_OWNER_KIND, owner_id, seq_hash)
                    for seq_hash in checkpoint_sequence_heads(state)
                ],
            )

    @staticmethod
    def _collect_unreachable_content(connection: sqlite3.Connection) -> None:
        """Delete chain nodes and elements no record or checkpoint reaches."""
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS retention_reachable (seq_hash TEXT PRIMARY KEY)"
        )
        connection.execute("DELETE FROM temp.retention_reachable")
        connection.execute(
            """
            WITH RECURSIVE reachable(seq_hash) AS (
                SELECT seq_hash FROM trajectory_sequence_refs
                UNION
                SELECT sequences.prev_hash
                FROM trajectory_sequences AS sequences
                JOIN reachable ON sequences.seq_hash = reachable.seq_hash
                WHERE sequences.prev_hash IS NOT NULL
            )
            INSERT INTO temp.retention_reachable (seq_hash)
            SELECT seq_hash FROM reachable
            """
        )
        connection.execute(
            """
            DELETE FROM trajectory_sequences
            WHERE seq_hash NOT IN (SELECT seq_hash FROM temp.retention_reachable)
            """
        )
        connection.execute(
            """
            DELETE FROM trajectory_blobs
            WHERE blob_hash NOT IN (SELECT blob_hash FROM trajectory_sequences)
            """
        )
        connection.execute("DELETE FROM temp.retention_reachable")

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
        """Create the state row, or rotate the epoch if it no longer holds.

        ``max_change_seq`` is the revision counter itself, not a summary of
        some table, so it is only checked against what it must bound: no
        stored record may carry a later revision than the counter handed out.
        """
        current_max = _current_max_ingest_seq(connection)
        highest_record_change = _highest_record_change_seq(connection)
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
                (_new_store_epoch(), current_max, highest_record_change),
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
        stored_state_invalid = (
            not stored_epoch
            or stored_max < 0
            or stored_change_max < highest_record_change
        )
        if stored_state_invalid or current_max < stored_max:
            connection.execute(
                """
                UPDATE trajectory_store_state
                SET store_epoch = ?, max_ingest_seq = ?, max_change_seq = ?
                WHERE singleton = 1
                """,
                (
                    _new_store_epoch(),
                    current_max,
                    max(stored_change_max, highest_record_change),
                ),
            )
            return
        if current_max > stored_max:
            TrajectoryStore._sync_max_ingest_seq(connection)

    @staticmethod
    def _sync_max_ingest_seq(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE trajectory_store_state SET max_ingest_seq = ? WHERE singleton = 1",
            (_current_max_ingest_seq(connection),),
        )

    @staticmethod
    def _rotate_store_epoch(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE trajectory_store_state
            SET store_epoch = ?, max_ingest_seq = ?
            WHERE singleton = 1
            """,
            (_new_store_epoch(), _current_max_ingest_seq(connection)),
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
                retired = {
                    str(row["subject_id"]): dict(row["state"].get("usage") or {})
                    for row in await _read_checkpoint_rows(connection, session_id)
                }
            finally:
                await connection.rollback()
                await connection.close()
        return _cumulative_request_usage(tuple(facts.values()), retired), store_epoch

    async def get_retention_checkpoints(self, session_id: str) -> dict[str, Any] | None:
        """Return the checkpoints retention left for one session, with their content.

        Every chain a checkpoint refers to is resolved in the same snapshot and
        all of its content is included, so a reader seeds itself before it
        loads a single record, whatever it already holds.

        Args:
            session_id: Session to read.

        Returns:
            ``store_epoch``, ``checkpoints`` (subject, boundary and ``state``
            per row, see ``jiuwenswarm.observability.retention``), and the
            ``sequences`` and ``blobs`` they refer to; None when the session
            has no database.
        """
        connection = await self._connect(session_id)
        if connection is None:
            return None
        try:
            await connection.execute("BEGIN")
            store_epoch = await _read_store_epoch(connection)
            rows = await _read_checkpoint_rows(connection, session_id)
            heads = list(dict.fromkeys(
                head for row in rows for head in checkpoint_sequence_heads(row["state"])
            ))
            nodes = await _fetch_sequence_nodes(connection, heads)
            sequences: dict[str, list[str]] = {}
            for head in heads:
                elements = _chain_elements(head, nodes)
                if elements:
                    sequences[head] = elements
            blobs = await _fetch_blob_texts(
                connection,
                list(dict.fromkeys(element for elements in sequences.values() for element in elements)),
            )
        finally:
            await connection.rollback()
            await connection.close()
        return {
            "store_epoch": store_epoch,
            "checkpoints": [_checkpoint_response(row) for row in rows],
            "sequences": sequences,
            "blobs": blobs,
        }

    async def iter_session_archive_lines(
        self,
        session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield one session's archive as content-addressed lines, in commit order.

        The whole walk reads one SQLite snapshot, so the lines describe a
        single consistent state however long the export takes. Records are
        read in change_seq order through one cursor a batch at a time, and the
        content they refer to is resolved per batch: a ``blob`` or
        ``sequence`` line is emitted just before the first line that needs it,
        parents before children. What is held in memory across batches is only
        the set of hashes already emitted, so any prefix of the lines can be
        replayed on its own. Stream frames are not part of an archive.

        Args:
            session_id: Session to export.

        Yields:
            ``header`` first, then one ``checkpoint`` line per retention
            checkpoint, then ``record`` lines, each ``blob``/``sequence`` line
            just before the first line that needs it, then one ``usage`` line
            per request, and ``end`` last.
        """
        header: dict[str, Any] = {
            "type": "header",
            "format": TRAJECTORY_ARCHIVE_FORMAT,
            "archive_version": TRAJECTORY_ARCHIVE_VERSION,
            "session_id": session_id,
            "exported_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "store_epoch": _ABSENT_STORE_EPOCH,
            "revision": "0",
            "stream_frames": False,
        }
        connection = await self._connect(session_id)
        if connection is None:
            yield header
            yield {"type": "end", "records": 0, "lines": 1}
            return
        line_count = 0
        record_count = 0
        emitted_sequences: set[str] = set()
        emitted_blobs: set[str] = set()
        facts: dict[tuple[str, str], dict[str, Any]] = {}
        retired_usage: dict[str, dict[str, int]] = {}
        try:
            await connection.execute("BEGIN")
            header["store_epoch"] = await _read_store_epoch(connection)
            header["revision"] = str(
                await _session_revision_watermark(connection, session_id)
            )
            yield header
            line_count += 1
            checkpoint_rows = await _read_checkpoint_rows(connection, session_id)
            retired_usage = {
                str(row["subject_id"]): dict(row["state"].get("usage") or {})
                for row in checkpoint_rows
            }
            for row in checkpoint_rows:
                checkpoint_heads = checkpoint_sequence_heads(row["state"])
                nodes = await _fetch_sequence_nodes(
                    connection,
                    [head for head in checkpoint_heads if head not in emitted_sequences],
                )
                blobs = await _fetch_blob_texts(
                    connection,
                    sorted({blob_hash for _, blob_hash, _ in nodes.values()} - emitted_blobs),
                )
                for line in _archive_content_lines(
                    checkpoint_heads,
                    nodes,
                    blobs,
                    emitted_sequences=emitted_sequences,
                    emitted_blobs=emitted_blobs,
                ):
                    yield line
                    line_count += 1
                yield {"type": "checkpoint", **_checkpoint_response(row)}
                line_count += 1
            async with connection.execute(
                f"""
                WITH {_ELIGIBLE_TRACES_CTE}
                SELECT current.trace_id AS trace_id,
                       current.span_id AS span_id,
                       current.parent_span_id AS parent_span_id,
                       current.session_id AS session_id,
                       current.request_id AS request_id,
                       current.run_id AS run_id,
                       current.agent_mode AS agent_mode,
                       current.execution_subject_id AS execution_subject_id,
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
                       current.raw_size_bytes AS raw_size_bytes,
                       current.raw_sha256 AS raw_sha256,
                       current.update_kind AS update_kind
                FROM trajectory_current_records AS current
                {_CURRENT_ARCHIVE_JOIN}
                INNER JOIN eligible_traces
                    ON eligible_traces.trace_id = current.trace_id
                WHERE current.session_id = ?
                ORDER BY current.change_seq ASC
                """,
                (
                    *_trajectory_scope_params(),
                    session_id,
                ),
            ) as statement:
                while True:
                    rows = await statement.fetchmany(_ARCHIVE_RECORD_BATCH)
                    if not rows:
                        break
                    decoded: list[tuple[aiosqlite.Row, bytes, dict[str, Any] | None]] = []
                    for row in rows:
                        raw_json = _decode_payload(row["raw_json"])
                        try:
                            otlp: dict[str, Any] | None = strict_otlp_payload(raw_json)
                        except (RecursionError, TypeError, ValueError, OverflowError):
                            otlp = None
                        decoded.append((row, raw_json, otlp))
                    references = [_record_sequence_references(otlp) for _, _, otlp in decoded]
                    pending_heads: set[str] = set()
                    for record_references in references:
                        pending_heads.update(_reference_heads(record_references))
                    pending_heads -= emitted_sequences
                    nodes = await _fetch_sequence_nodes(connection, sorted(pending_heads))
                    pending_blobs = {blob_hash for _, blob_hash, _ in nodes.values()}
                    pending_blobs -= emitted_blobs
                    blobs = await _fetch_blob_texts(connection, sorted(pending_blobs))
                    for (row, raw_json, otlp), record_references in zip(decoded, references):
                        for line in _archive_content_lines(
                            _reference_heads(record_references),
                            nodes,
                            blobs,
                            emitted_sequences=emitted_sequences,
                            emitted_blobs=emitted_blobs,
                        ):
                            yield line
                            line_count += 1
                        yield _archive_record_line(row, raw_json, otlp, record_references)
                        line_count += 1
                        record_count += 1
                        if otlp is None:
                            continue
                        fact = _request_usage_fact_from_payload(
                            otlp,
                            trace_id=str(row["trace_id"]),
                            start_time_unix_nano=int(row["start_time_unix_nano"]),
                        )
                        if fact is not None:
                            facts[(fact["trace_id"], fact["inference_id"])] = fact
        finally:
            await connection.rollback()
            await connection.close()
        for item in _cumulative_request_usage(tuple(facts.values()), retired_usage):
            yield {"type": "usage", **item}
            line_count += 1
        yield {"type": "end", "records": record_count, "lines": line_count}

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
                       current.raw_size_bytes AS raw_size_bytes
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
                           current.raw_size_bytes AS raw_size_bytes,
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
            nodes = await _fetch_sequence_nodes(connection, wanted)
            sequences: dict[str, list[str]] = {}
            needed: list[str] = []
            for head in wanted:
                elements = _chain_elements(head, nodes)
                if elements:
                    sequences[head] = elements
                    needed.extend(elements)
            blobs = await _fetch_blob_texts(
                connection,
                list(dict.fromkeys(needed)),
                since_revision=since_revision,
            )
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
            # first frame means what the reader wanted next is gone -- retired
            # by retention, or released by the terminal record of the span that
            # produced it. Either way the reader has to start over rather than
            # resume into a gap it cannot see. Starting over costs it only the
            # frames of spans whose own records now state more than they did.
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
        version_row = await _fetch_one(connection, "PRAGMA user_version", ())
        if version_row is None or int(version_row[0]) != _SCHEMA_VERSION:
            # A database from another schema version is discarded when its
            # writer next opens it; until then it reads as absent, never as
            # rows this reader would misinterpret.
            await connection.close()
            return None
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


async def _fetch_sequence_nodes(
    connection: aiosqlite.Connection,
    heads: Sequence[str],
) -> dict[str, tuple[str | None, str, int]]:
    """Fetch every chain node reachable from the given heads.

    The walk is one recursive query per chunk of heads, and it deduplicates as
    it goes: several spans of one conversation share a prefix, so that prefix
    is visited once however many of them asked for it.

    Args:
        connection: Open reader connection.
        heads: Chain heads to walk back from.

    Returns:
        ``(prev_hash, blob_hash, depth)`` by ``seq_hash`` for every node found.
        A chain that lost an ancestor simply contributes fewer nodes.
    """
    nodes: dict[str, tuple[str | None, str, int]] = {}
    for start in range(0, len(heads), _SEQUENCE_FETCH_CHUNK):
        chunk = heads[start:start + _SEQUENCE_FETCH_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        async with connection.execute(
            _REACHABLE_NODES_SQL.format(placeholders=placeholders),
            tuple(chunk),
        ) as statement:
            for row in await statement.fetchall():
                nodes[str(row["seq_hash"])] = (
                    None if row["prev_hash"] is None else str(row["prev_hash"]),
                    str(row["blob_hash"]),
                    int(row["depth"]),
                )
    return nodes


def _chain_elements(
    head: str,
    nodes: Mapping[str, tuple[str | None, str, int]],
) -> list[str]:
    """Return the element hashes of one chain, root first, from fetched nodes."""
    elements: list[str] = []
    cursor: str | None = head
    # Walking in memory costs one dictionary lookup per element, and a chain
    # that lost an ancestor simply stops early rather than looping.
    while cursor is not None and cursor in nodes:
        previous, blob_hash, _depth = nodes[cursor]
        elements.append(blob_hash)
        cursor = previous
    elements.reverse()
    return elements


async def _read_checkpoint_rows(
    connection: aiosqlite.Connection,
    session_id: str,
) -> list[dict[str, Any]]:
    """Read one session's retention checkpoints, their state parsed."""
    async with connection.execute(
        """
        SELECT subject_id, boundary_turn_id, boundary_change_seq, state_json, updated_at
        FROM trajectory_retention_checkpoints
        WHERE session_id = ?
        ORDER BY subject_id ASC
        """,
        (_checkpoint_session_key(session_id),),
    ) as statement:
        rows = await statement.fetchall()
    return [
        {
            "subject_id": str(row["subject_id"]),
            "boundary_turn_id": row["boundary_turn_id"],
            "boundary_change_seq": int(row["boundary_change_seq"]),
            "updated_at": int(row["updated_at"]),
            "state": json.loads(row["state_json"]),
        }
        for row in rows
    ]


def _checkpoint_response(row: Mapping[str, Any]) -> dict[str, Any]:
    """Shape one checkpoint row for the wire and for an archive line."""
    return {
        "subject_id": row["subject_id"],
        "boundary_turn_id": row["boundary_turn_id"],
        "boundary_change_seq": str(row["boundary_change_seq"]),
        "state": row["state"],
    }


async def _fetch_blob_texts(
    connection: aiosqlite.Connection,
    blob_hashes: Sequence[str],
    *,
    since_revision: int = 0,
) -> dict[str, str]:
    """Fetch element content by hash, in chunks the bound-variable limit allows.

    Args:
        connection: Open reader connection.
        blob_hashes: Distinct element hashes to fetch.
        since_revision: Content first seen at or before this revision is
            assumed held by the reader and is skipped.

    Returns:
        Decoded content by hash for every element found.
    """
    blobs: dict[str, str] = {}
    for start in range(0, len(blob_hashes), _SEQUENCE_FETCH_CHUNK):
        chunk = blob_hashes[start:start + _SEQUENCE_FETCH_CHUNK]
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
    return blobs


def _current_max_ingest_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(ingest_seq), 0) AS max_ingest_seq FROM otlp_span_records"
    ).fetchone()
    return int(row["max_ingest_seq"]) if row is not None else 0


def _highest_record_change_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(change_seq), 0) AS change_seq FROM trajectory_current_records"
    ).fetchone()
    return int(row["change_seq"]) if row is not None else 0


def _stored_max_change_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT max_change_seq FROM trajectory_store_state WHERE singleton = 1"
    ).fetchone()
    return int(row["max_change_seq"]) if row is not None else 0


def _has_final_record(
    connection: sqlite3.Connection,
    trace_id: str,
    span_id: str,
) -> bool:
    """Report whether one span already holds a terminal record.

    Args:
        connection: The open write transaction.
        trace_id: Trace the span belongs to.
        span_id: Span to test.

    Returns:
        True when the span's record states its complete output.
    """
    row = connection.execute(
        """
        SELECT 1
        FROM trajectory_current_records
        WHERE trace_id = ? AND span_id = ? AND lifecycle = 'final'
        """,
        (trace_id, span_id),
    ).fetchone()
    return row is not None


def _next_change_seq(connection: sqlite3.Connection) -> int:
    """Hand out the next revision inside the caller's write transaction.

    Revisions are what readers resume from, so they only ever grow -- also
    across retention, which deletes records but never rewinds this counter.
    The bump and the read are two statements rather than ``UPDATE ... RETURNING``
    because ``RETURNING`` needs SQLite 3.35+, newer than some supported Python
    builds link against. The caller's write transaction keeps them atomic.
    """
    cursor = connection.execute(
        """
        UPDATE trajectory_store_state
        SET max_change_seq = max_change_seq + 1
        WHERE singleton = 1
        """
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Trajectory store state is missing")
    return _stored_max_change_seq(connection)


def _new_store_epoch() -> str:
    return uuid.uuid4().hex


def _record_owner_id(trace_id: str, span_id: str) -> str:
    """Name a record as the owner of the chains it refers to."""
    return f"{trace_id}:{span_id}"


def _checkpoint_session_key(session_id: str | None) -> str:
    """Key a checkpoint by session; records without a session share the empty key."""
    return session_id or ""


def _checkpoint_owner_id(session_key: str, subject_id: str) -> str:
    """Name a checkpoint as the owner of the chains it refers to."""
    return json.dumps([session_key, subject_id], ensure_ascii=False, separators=(",", ":"))


def _checkpoint_trace_turn_ids(
    connection: sqlite3.Connection,
    session_id: str | None,
) -> dict[str, dict[str, list[str]]]:
    """Read the turn ids each checkpoint of a session recorded, by subject and trace."""
    turn_ids: dict[str, dict[str, list[str]]] = {}
    for row in connection.execute(
        "SELECT subject_id, state_json FROM trajectory_retention_checkpoints WHERE session_id = ?",
        (_checkpoint_session_key(session_id),),
    ):
        turns = json.loads(row["state_json"]).get("turns") or {}
        turn_ids[str(row["subject_id"])] = dict(turns.get("trace_turn_ids") or {})
    return turn_ids


def _retention_row(row: sqlite3.Row) -> RetentionRow:
    return RetentionRow(
        session_id=row["session_id"],
        trace_id=str(row["trace_id"]),
        span_id=str(row["span_id"]),
        parent_span_id=row["parent_span_id"],
        agent_mode=row["agent_mode"],
        subject_id=str(row["view_subject_id"]),
        subject_kind=str(row["view_subject_kind"]),
        subject_session_id=row["view_subject_session_id"],
        projected=bool(row["view_projected"]),
        turn_id=row["turn_id"],
        turn_number=None if row["turn_number"] is None else int(row["turn_number"]),
        start_time_unix_nano=int(row["start_time_unix_nano"]),
        observed_time_unix_nano=int(row["observed_time_unix_nano"]),
        lifecycle=str(row["lifecycle"]),
        created_at=int(row["created_at"]),
        change_seq=int(row["change_seq"]),
    )


def _retired_payloads(
    connection: sqlite3.Connection,
    rows: Sequence[RetentionRow],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the stored OTLP payload of each record about to be retired."""
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for start in range(0, len(rows), _SEQUENCE_FETCH_CHUNK // 2):
        chunk = rows[start:start + _SEQUENCE_FETCH_CHUNK // 2]
        clauses = " OR ".join("(current.trace_id = ? AND current.span_id = ?)" for _ in chunk)
        parameters = [value for row in chunk for value in row.identity]
        for stored in connection.execute(
            f"""
            SELECT current.trace_id AS trace_id,
                   current.span_id AS span_id,
                   {_CURRENT_RAW_JSON} AS raw_json
            FROM trajectory_current_records AS current
            {_CURRENT_ARCHIVE_JOIN}
            WHERE {clauses}
            """,
            parameters,
        ):
            if stored["raw_json"] is None:
                continue
            payload = parse_otlp_payload(_decode_payload(stored["raw_json"]))
            if payload is not None:
                payloads[(str(stored["trace_id"]), str(stored["span_id"]))] = payload
    return payloads


def _retired_usage(
    rows: Sequence[RetentionRow],
    payloads: Mapping[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Sum the token usage of retired requests per usage subject."""
    facts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        payload = payloads.get(row.identity)
        if payload is None:
            continue
        fact = _request_usage_fact_from_payload(
            payload,
            trace_id=row.trace_id,
            start_time_unix_nano=row.start_time_unix_nano,
        )
        if fact is not None:
            facts[(fact["trace_id"], fact["inference_id"])] = fact
    totals: dict[str, dict[str, int]] = {}
    for fact in facts.values():
        subject_totals = totals.setdefault(str(fact["subject_id"]), {})
        for key, value in dict(fact["usage"]).items():
            subject_totals[key] = subject_totals.get(key, 0) + int(value)
    return totals


def _store_message_sequence(
    connection: sqlite3.Connection,
    messages: list[dict[str, Any]],
    created_at: int,
) -> dict[str, Any]:
    """Store one message list as a content-addressed chain and return its reference.

    The chain shares nodes and elements with every other list holding the same
    messages, so a checkpoint costs what its windows add.
    """
    sequence = build_sequence(
        _CHECKPOINT_MESSAGES_KEY,
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
    )
    if sequence is None:
        return {"hash": None, "depth": 0}
    first_change_seq = _stored_max_change_seq(connection)
    connection.executemany(
        """
        INSERT INTO trajectory_blobs (
            blob_hash, content, byte_size, first_change_seq, created_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(blob_hash) DO NOTHING
        """,
        [
            (
                blob_hash,
                sqlite3.Binary(_encode_payload(content)),
                len(content),
                first_change_seq,
                created_at,
            )
            for blob_hash, content in sequence.blobs.items()
        ],
    )
    connection.executemany(
        """
        INSERT INTO trajectory_sequences (
            seq_hash, prev_hash, blob_hash, depth, created_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(seq_hash) DO NOTHING
        """,
        [
            (node.seq_hash, node.prev_hash, node.blob_hash, node.depth, created_at)
            for node in sequence.nodes
        ],
    )
    return {"hash": sequence.seq_hash, "depth": sequence.depth}


class _SequenceValueResolver:
    """Rebuild chain values inside the writer transaction, each chain once."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._values: dict[str, str | None] = {}

    def value(self, seq_hash: str) -> str | None:
        """Return the value a chain states, or None when it cannot be rebuilt."""
        if seq_hash in self._values:
            return self._values[seq_hash]
        nodes: dict[str, tuple[str | None, str]] = {}
        for row in self._connection.execute(_REACHABLE_NODES_SQL.format(placeholders="?"), (seq_hash,)):
            nodes[str(row["seq_hash"])] = (row["prev_hash"], str(row["blob_hash"]))
        elements: list[str] = []
        cursor: str | None = seq_hash
        while cursor is not None and cursor in nodes:
            previous, blob_hash = nodes[cursor]
            elements.append(blob_hash)
            cursor = previous
        elements.reverse()
        contents: dict[str, str] = {}
        distinct = list(dict.fromkeys(elements))
        for start in range(0, len(distinct), _SEQUENCE_FETCH_CHUNK):
            chunk = distinct[start:start + _SEQUENCE_FETCH_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for row in self._connection.execute(
                f"SELECT blob_hash, content FROM trajectory_blobs WHERE blob_hash IN ({marks})",
                chunk,
            ):
                contents[str(row["blob_hash"])] = _decode_payload(row["content"]).decode("utf-8", "replace")
        resolved = None
        if elements and all(element in contents for element in elements):
            resolved = rebuild_value([contents[element] for element in elements])
        self._values[seq_hash] = resolved
        return resolved

    def messages(self, reference: Any) -> list[dict[str, Any]] | None:
        """Return the message list a checkpoint reference states, or None."""
        if not isinstance(reference, dict):
            return None
        seq_hash = reference.get("hash")
        if seq_hash is None:
            return [] if reference.get("depth") == 0 else None
        value = self.value(str(seq_hash))
        if value is None:
            return None
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else None


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
        payload = strict_otlp_payload(raw_json)
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
        payload = strict_otlp_payload(raw_json)
    except Exception:
        return None
    return _request_usage_fact_from_payload(
        payload,
        trace_id=trace_id,
        start_time_unix_nano=start_time_unix_nano,
    )


def _request_usage_fact_from_payload(
    payload: dict[str, Any],
    *,
    trace_id: str,
    start_time_unix_nano: int,
) -> dict[str, Any] | None:
    """Extract one inference's usage from an already parsed OTLP payload."""
    spans = []
    for resource_span in payload.get("resourceSpans", []):
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans", []):
            if isinstance(scope_span, dict):
                spans.extend(scope_span.get("spans", []))
    if len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    attribute_entries = {
        str(attribute.get("key")): attribute.get("value")
        for attribute in spans[0].get("attributes", [])
        if isinstance(attribute, dict) and isinstance(attribute.get("key"), str)
    }
    attributes = {
        key: _otlp_attribute_value(value) for key, value in attribute_entries.items()
    }
    if attributes.get("gen_ai.operation.name") not in {"chat", "generate_content", "text_completion"}:
        return None
    # The viewer joins cumulative usage to a request by the inference id it
    # reads, which is the exact string value, so it is keyed the same way.
    inference_value = attribute_entries.get("openjiuwen.inference.id")
    inference_id = inference_value.get("stringValue") if isinstance(inference_value, dict) else None
    if not isinstance(inference_id, str) or not inference_id.strip():
        return None
    subject_id = str(attributes.get("openjiuwen.execution.subject.id") or "main").strip()
    usage_keys = {
        "input": "gen_ai.usage.input_tokens",
        "cacheRead": "gen_ai.usage.cache_read.input_tokens",
        "cacheWrite": "gen_ai.usage.cache_write.input_tokens",
        "output": "gen_ai.usage.output_tokens",
        "reasoning": "gen_ai.usage.reasoning.output_tokens",
    }
    # Token counts are read as the viewer reads a request's own usage: the
    # int64 arm only, as a non-negative integer it can represent exactly.
    usage: dict[str, int] = {}
    for output_key, attribute_key in usage_keys.items():
        value = int64_attribute_value(attribute_entries.get(attribute_key))
        if value is not None and 0 <= value <= MAX_SAFE_INTEGER:
            usage[output_key] = value
    input_tokens = usage.get("input")
    output_tokens = usage.get("output")
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        if total_tokens <= MAX_SAFE_INTEGER:
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
    retired: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    """Accumulate usage per subject in request order.

    Args:
        facts: One usage fact per current request.
        retired: Usage of each subject's retired requests, which its
            cumulative figures continue from.

    Returns:
        Each fact with its cumulative usage.
    """
    cumulative_by_subject: dict[str, dict[str, int]] = {
        subject_id: dict(usage) for subject_id, usage in retired.items()
    }
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
                    parsed = parse_sequence_reference(value.get("stringValue"))
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
        otlp = strict_otlp_payload(raw_json)
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


def _reference_heads(references: dict[str, dict[str, Any]]) -> list[str]:
    """Return the chain heads one record's references name, in attribute order."""
    return [
        str(reference["hash"])
        for reference in references.values()
        if reference.get("hash")
    ]


def _archive_content_lines(
    heads: Sequence[str],
    nodes: dict[str, tuple[str | None, str, int]],
    blobs: dict[str, str],
    *,
    emitted_sequences: set[str],
    emitted_blobs: set[str],
) -> list[dict[str, Any]]:
    """Define the content a record is about to reference and no line has yet.

    Each chain is walked back only to the first node already emitted, then
    stated root first: a ``sequence`` line follows the ``blob`` line of its
    element and the ``sequence`` line of its parent, so a reader resolves
    every hash as soon as it reads it. A node or element the store no longer
    holds is skipped; the reference that needed it stays unresolved, exactly
    as it would for a live reader.

    Args:
        heads: Chain heads the next record refers to.
        nodes: Chain nodes fetched for the current batch, by hash.
        blobs: Element content fetched for the current batch, by hash.
        emitted_sequences: Chain hashes already written; updated in place.
        emitted_blobs: Element hashes already written; updated in place.

    Returns:
        The ``blob`` and ``sequence`` lines to write before the record.
    """
    lines: list[dict[str, Any]] = []
    for head in heads:
        path: list[str] = []
        cursor = head
        while cursor is not None and cursor not in emitted_sequences and cursor in nodes:
            path.append(cursor)
            cursor = nodes[cursor][0]
        for seq_hash in reversed(path):
            previous, blob_hash, depth = nodes[seq_hash]
            if blob_hash not in emitted_blobs and blob_hash in blobs:
                emitted_blobs.add(blob_hash)
                lines.append({"type": "blob", "hash": blob_hash, "text": blobs[blob_hash]})
            emitted_sequences.add(seq_hash)
            lines.append({
                "type": "sequence",
                "hash": seq_hash,
                "prev": previous,
                "blob": blob_hash,
                "depth": depth,
            })
    return lines


def _archive_record_line(
    row: aiosqlite.Row,
    raw_json: bytes,
    otlp: dict[str, Any] | None,
    references: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one lossless, version-independent archive record line.

    The payload is carried once, as the stored bytes: UTF-8 text when it
    decodes, base64 otherwise. A reader parses it itself, so the line does not
    restate it a second time as parsed OTLP.

    Args:
        row: Current-record row, joined with its archived payload.
        raw_json: The decoded payload bytes of *row*.
        otlp: *raw_json* parsed as strict OTLP, or None when it is not.
        references: The chains the payload refers to, by attribute key.

    Returns:
        The ``record`` line.
    """
    line: dict[str, Any] = {
        "type": "record",
        "record_id": f'{row["trace_id"]}:{row["span_id"]}',
        "trace_id": str(row["trace_id"]),
        "span_id": str(row["span_id"]),
        "parent_span_id": row["parent_span_id"],
        "subject_id": str(row["execution_subject_id"]),
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
        "raw_size_bytes": int(row["raw_size_bytes"]),
        "raw_valid": otlp is not None,
    }
    try:
        line["raw_json"] = raw_json.decode("utf-8")
    except UnicodeDecodeError:
        line["raw_json_base64"] = base64.b64encode(raw_json).decode("ascii")
    if references:
        line["sequences"] = references
    return line


__all__ = [
    "TRAJECTORY_ARCHIVE_FORMAT",
    "TRAJECTORY_ARCHIVE_VERSION",
    "AsyncTrajectoryReader",
    "TrajectoryStore",
]
