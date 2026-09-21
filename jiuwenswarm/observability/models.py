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
    lifecycle: str = "final"
    record_revision: int = 1
    observed_time_unix_nano: int = 0
    update_kind: str = "completed"

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
            lifecycle="final",
            record_revision=max(1, int(getattr(record, "record_revision", 1))),
            observed_time_unix_nano=max(
                0,
                int(getattr(record, "observed_time_unix_nano", end_time) or end_time),
            ),
            update_kind="completed",
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
            # ``running`` is the current storage contract.  Accept the earlier
            # ``provisional`` alias additively, but normalize it before the
            # restart recovery and finalization state machine sees the record.
            lifecycle="running",
            record_revision=revision,
            observed_time_unix_nano=observed_time,
            update_kind=update_kind,
        )


@dataclass(frozen=True, slots=True)
class CommittedTraceUpdate:
    """Highest committed revision for one session and trace in a writer batch."""

    session_id: str
    trace_id: str
    revision: int
    store_epoch: str | None = None
    lifecycle: str = "final"


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


__all__ = [
    "CommittedTraceUpdate",
    "OtlpSpanRecordLike",
    "OtlpSpanSnapshotRecordLike",
    "TraceRecordData",
    "TraceSinkStats",
    "WriteBatchResult",
]
