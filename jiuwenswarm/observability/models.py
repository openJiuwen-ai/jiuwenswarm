# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Typed records used by the Swarm trajectory persistence boundary."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Protocol


class OtlpSpanRecordLike(Protocol):
    """Structural contract implemented by the Agent Core OTLP processor record."""

    raw_json: bytes
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int
    session_id: str | None
    request_id: str | None
    run_id: str | None
    agent_mode: str | None
    schema_version: str
    execution_subject_id: str | None
    execution_subject_display_name: str | None
    execution_subject_kind: str | None
    execution_subject_parent_id: str | None


class OtlpSpanSnapshotRecordLike(Protocol):
    """Structural contract implemented by the Agent Core live snapshot record."""

    raw_json: bytes
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_unix_nano: int
    observed_time_unix_nano: int
    record_revision: int
    update_kind: str
    lifecycle: str
    session_id: str | None
    request_id: str | None
    run_id: str | None
    agent_mode: str | None
    schema_version: str
    execution_subject_id: str | None
    execution_subject_display_name: str | None
    execution_subject_kind: str | None
    execution_subject_parent_id: str | None


@dataclass(frozen=True, slots=True)
class SequenceNodeData:
    """One prefix of a content-addressed sequence."""

    seq_hash: str
    prev_hash: str | None
    blob_hash: str
    depth: int


@dataclass(frozen=True, slots=True)
class AddressedSequenceData:
    """One restated attribute expressed as a chain, plus what it introduced.

    The GenAI convention has every call state its whole input. Storage keeps
    one copy of each distinct element and one node per distinct prefix, so a
    conversation costs what it added rather than what it restated.
    """

    key: str
    seq_hash: str
    depth: int
    nodes: tuple[SequenceNodeData, ...]
    blobs: dict[str, bytes]

    @classmethod
    def from_core_sequence(cls, sequence: object) -> AddressedSequenceData:
        """Copy one sequence as Agent Core published it."""
        nodes = tuple(
            SequenceNodeData(
                seq_hash=str(node.seq_hash),
                prev_hash=None if node.prev_hash is None else str(node.prev_hash),
                blob_hash=str(node.blob_hash),
                depth=int(node.depth),
            )
            for node in getattr(sequence, "nodes", ())
        )
        if not nodes:
            raise ValueError("an addressed sequence states no nodes")
        return cls(
            key=str(getattr(sequence, "key", "")),
            seq_hash=str(sequence.seq_hash),
            depth=int(sequence.depth),
            nodes=nodes,
            blobs={str(k): bytes(v) for k, v in dict(getattr(sequence, "blobs", {})).items()},
        )


class StreamFrameRecordLike(Protocol):
    """Structural contract implemented by the Agent Core stream frame record."""

    event_name: str
    timestamp_unix_nano: int
    observed_timestamp_unix_nano: int
    trace_id: str
    span_id: str
    sequence: int
    kind: str
    session_id: str | None
    execution_subject_id: str | None
    execution_subject_session_id: str | None
    text: str | None
    tool_call_id: str | None
    tool_name: str | None
    arguments_delta: str | None
    request_id: str | None
    run_id: str | None
    agent_mode: str | None
    schema_version: str


@dataclass(frozen=True, slots=True)
class StreamFrameData:
    """Immutable copy of one model-stream frame queued for the writer.

    A frame is additive rather than a snapshot of anything: it states one
    increment of a model's answer and is meaningful only next to its
    neighbours. Nothing may coalesce frames, because a dropped one is an
    increment no later frame restates.
    """

    session_id: str
    execution_subject_id: str
    trace_id: str
    span_id: str
    sequence: int
    kind: str
    # When the model produced the frame. A frame is written within
    # milliseconds of that, so it carries no separate write timestamp.
    timestamp_unix_nano: int
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None

    @classmethod
    def from_core_frame(cls, record: StreamFrameRecordLike) -> StreamFrameData:
        """Copy one stream frame emitted by Agent Core.

        Args:
            record: The frame as Agent Core published it.

        Returns:
            The immutable copy the writer thread persists.

        Raises:
            ValueError: If the frame carries no owning span, no session, or a
                negative sequence.
        """
        trace_id = str(record.trace_id or "").strip().lower()
        span_id = str(record.span_id or "").strip().lower()
        if not trace_id or not span_id:
            raise ValueError("trace_id and span_id are required")
        session_id = _normalize_text(record.session_id)
        if session_id is None:
            raise ValueError("session_id is required")
        sequence = int(record.sequence)
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        kind = _normalize_text(record.kind)
        if kind is None:
            raise ValueError("kind is required")
        timestamp = int(record.timestamp_unix_nano)
        if timestamp < 0:
            raise ValueError("frame timestamp must be non-negative")
        return cls(
            session_id=session_id,
            execution_subject_id=_subject_id(record),
            trace_id=trace_id,
            span_id=span_id,
            sequence=sequence,
            kind=kind,
            timestamp_unix_nano=timestamp,
            text=_frame_text(record.text),
            tool_call_id=_normalize_text(getattr(record, "tool_call_id", None)),
            tool_name=_normalize_text(getattr(record, "tool_name", None)),
            arguments_delta=_frame_text(getattr(record, "arguments_delta", None)),
        )


@dataclass(frozen=True, slots=True)
class TraceRecordData:
    """Immutable copy queued by Swarm without parsing or rewriting raw JSON."""

    raw_json: bytes
    raw_sha256: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int
    session_id: str | None
    request_id: str | None
    run_id: str | None
    agent_mode: str | None
    schema_version: str
    source: str
    created_at: int
    # The execution subject owns the canonical trajectory chain: Agent Core
    # keys its context windows by (session_id, subject_id), and the viewer
    # groups and replays by the same pair. Persisting it as a column is what
    # lets a reader follow one chain without parsing every payload.
    execution_subject_id: str = "main"
    execution_subject_display_name: str | None = None
    execution_subject_kind: str | None = None
    execution_subject_parent_id: str | None = None
    lifecycle: str = "final"
    record_revision: int = 1
    observed_time_unix_nano: int = 0
    update_kind: str = "completed"
    # What this record measures once its references are rebuilt. A reader is
    # budgeted by what it receives, and it receives the rebuilt span, so the
    # reference-carrying length of raw_json would understate a page badly.
    logical_size_bytes: int = 0
    sequences: tuple[AddressedSequenceData, ...] = ()

    @classmethod
    def from_core_record(
        cls,
        record: OtlpSpanRecordLike,
        *,
        source: str = "processor",
        created_at: int | None = None,
    ) -> TraceRecordData:
        """Copy one Core record while preserving its original UTF-8 bytes.

        Args:
            record: Core record implementing the frozen inter-repository contract.
            source: Ingestion source label used only for diagnostics.
            created_at: Optional Unix-second ingestion timestamp.

        Returns:
            A validated immutable record ready for the writer queue.

        Raises:
            TypeError: If ``raw_json`` is not bytes-like.
            ValueError: If required identity or timestamp fields are invalid.
        """
        raw_value = record.raw_json
        if not isinstance(raw_value, (bytes, bytearray, memoryview)):
            raise TypeError("raw_json must be bytes-like")
        raw_json = bytes(raw_value)
        trace_id = str(record.trace_id or "").strip().lower()
        span_id = str(record.span_id or "").strip().lower()
        if not trace_id or not span_id:
            raise ValueError("trace_id and span_id are required")
        start_time = int(record.start_time_unix_nano)
        end_time = int(record.end_time_unix_nano)
        if start_time < 0 or end_time < 0:
            raise ValueError("span timestamps must be non-negative")
        source_value = str(source or "").strip()
        if not source_value:
            raise ValueError("source is required")
        return cls(
            raw_json=raw_json,
            raw_sha256=hashlib.sha256(raw_json).hexdigest(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=_normalize_text(record.parent_span_id, lowercase=True),
            start_time_unix_nano=start_time,
            end_time_unix_nano=end_time,
            session_id=_normalize_text(record.session_id),
            request_id=_normalize_text(record.request_id),
            run_id=_normalize_text(record.run_id),
            agent_mode=_normalize_text(record.agent_mode),
            schema_version=str(record.schema_version or "1"),
            source=source_value,
            created_at=int(created_at if created_at is not None else time.time()),
            execution_subject_id=_subject_id(record),
            execution_subject_display_name=_normalize_text(
                getattr(record, "execution_subject_display_name", None)
            ),
            execution_subject_kind=_normalize_text(
                getattr(record, "execution_subject_kind", None)
            ),
            execution_subject_parent_id=_normalize_text(
                getattr(record, "execution_subject_parent_id", None)
            ),
            lifecycle="final",
            record_revision=max(1, int(getattr(record, "record_revision", 1))),
            observed_time_unix_nano=max(
                0,
                int(getattr(record, "observed_time_unix_nano", end_time) or end_time),
            ),
            update_kind="completed",
            logical_size_bytes=_logical_size(record, raw_json),
            sequences=_addressed_sequences(record),
        )

    @classmethod
    def from_core_snapshot(
        cls,
        record: OtlpSpanSnapshotRecordLike,
        *,
        source: str = "processor_snapshot",
        created_at: int | None = None,
    ) -> TraceRecordData:
        """Copy one independently recoverable live span snapshot."""
        raw_value = record.raw_json
        if not isinstance(raw_value, (bytes, bytearray, memoryview)):
            raise TypeError("raw_json must be bytes-like")
        raw_json = bytes(raw_value)
        trace_id = str(record.trace_id or "").strip().lower()
        span_id = str(record.span_id or "").strip().lower()
        if not trace_id or not span_id:
            raise ValueError("trace_id and span_id are required")
        start_time = int(record.start_time_unix_nano)
        observed_time = int(record.observed_time_unix_nano)
        revision = int(record.record_revision)
        if start_time < 0 or observed_time < 0:
            raise ValueError("span timestamps must be non-negative")
        if revision < 1:
            raise ValueError("record_revision must be positive")
        source_value = str(source or "").strip()
        update_kind = str(record.update_kind or "").strip()
        snapshot_lifecycle = str(
            getattr(record, "lifecycle", "running") or "running"
        ).strip().lower()
        if not source_value or not update_kind:
            raise ValueError("source and update_kind are required")
        if snapshot_lifecycle not in {"running", "provisional"}:
            raise ValueError("snapshot lifecycle must be running or provisional")
        return cls(
            raw_json=raw_json,
            raw_sha256=hashlib.sha256(raw_json).hexdigest(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=_normalize_text(record.parent_span_id, lowercase=True),
            start_time_unix_nano=start_time,
            end_time_unix_nano=0,
            session_id=_normalize_text(record.session_id),
            request_id=_normalize_text(record.request_id),
            run_id=_normalize_text(record.run_id),
            agent_mode=_normalize_text(record.agent_mode),
            schema_version=str(record.schema_version or "1"),
            source=source_value,
            created_at=int(created_at if created_at is not None else time.time()),
            execution_subject_id=_subject_id(record),
            execution_subject_display_name=_normalize_text(
                getattr(record, "execution_subject_display_name", None)
            ),
            execution_subject_kind=_normalize_text(
                getattr(record, "execution_subject_kind", None)
            ),
            execution_subject_parent_id=_normalize_text(
                getattr(record, "execution_subject_parent_id", None)
            ),
            # ``running`` is the current storage contract.  Accept the earlier
            # ``provisional`` alias additively, but normalize it before the
            # restart recovery and finalization state machine sees the record.
            lifecycle="running",
            record_revision=revision,
            observed_time_unix_nano=observed_time,
            update_kind=update_kind,
            logical_size_bytes=_logical_size(record, raw_json),
            sequences=_addressed_sequences(record),
        )


@dataclass(frozen=True, slots=True)
class CommittedTraceUpdate:
    """Highest committed revision for one session and trace in a writer batch.

    ``frame_seq`` is a second, independent watermark. Records and stream
    frames advance on their own sequences, so a reader that only ever saw
    ``revision`` would never learn that new frames had landed for a span
    whose record did not change.
    """

    session_id: str
    trace_id: str
    revision: int
    store_epoch: str | None = None
    lifecycle: str = "final"
    frame_seq: int = 0


@dataclass(frozen=True, slots=True)
class WriteBatchResult:
    """Result of one committed writer transaction."""

    inserted: int
    conflicts: int
    updates: tuple[CommittedTraceUpdate, ...]


@dataclass(frozen=True, slots=True)
class TraceSinkStats:
    """Snapshot of record sink counters."""

    accepted: int
    committed: int
    dropped: int
    failed: int
    conflicts: int
    queued: int
    coalesced: int = 0
    evicted_provisional: int = 0
    dropped_final: int = 0
    stale_ignored: int = 0


def _normalize_text(value: str | None, *, lowercase: bool = False) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return normalized.lower() if lowercase else normalized


def _addressed_sequences(record: object) -> tuple[AddressedSequenceData, ...]:
    """Copy the sequences a Core record references, if it states any.

    A record that predates content addressing simply states none, and the
    whole path degrades to storing its payload as it always did.
    """
    stated = getattr(record, "sequences", ()) or ()
    return tuple(AddressedSequenceData.from_core_sequence(item) for item in stated)


def _logical_size(record: object, raw_json: bytes) -> int:
    """Return what a reader receives once this record's references are rebuilt."""
    stated = getattr(record, "logical_size_bytes", None)
    try:
        size = int(stated)
    except (TypeError, ValueError):
        return len(raw_json)
    return size if size > 0 else len(raw_json)


def _frame_text(value: str | None) -> str | None:
    """Return one frame's text verbatim, or None when it carries none.

    A frame's text is an increment of the model's answer, so its leading and
    trailing spaces are content. Trimming them the way record metadata is
    trimmed would glue words together as soon as a reader concatenates the
    stream, and would drop a frame whose whole content is one space.
    """
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _subject_id(record: object) -> str:
    """Return the execution subject owning one record.

    A record without an explicit subject belongs to the main agent, which is
    the same default Agent Core stamps and the viewer groups under.
    """
    return _normalize_text(getattr(record, "execution_subject_id", None)) or "main"


__all__ = [
    "CommittedTraceUpdate",
    "OtlpSpanRecordLike",
    "OtlpSpanSnapshotRecordLike",
    "TraceRecordData",
    "TraceSinkStats",
    "WriteBatchResult",
]
