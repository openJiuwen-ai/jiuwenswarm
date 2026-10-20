# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trajectory snapshot export and diagnosis range resolution.

The diagnosis host does not interpret the trajectory. It exports every finished
span of the session as one :class:`TrajectorySnapshot`, derives the per-subject
time ranges of the requested scope, and builds a :class:`ScopeIndex` that later
validates the step references an engine returns. The shapes follow
``docs/ai/design/轨迹诊断/06-诊断引擎接口.md``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from jiuwenswarm.diagnosis.models import (
    DiagnosisHostError,
    DiagnosisScope,
    StepRef,
    is_valid_node_id,
)

_RECORD_PAGE_SIZE = 200
_MAX_RECORD_PAGES_PER_SUBJECT = 500
_SEQUENCE_PREFIX = "@oj-seq:"
_LIFECYCLE_RUNNING = "running"


class SubjectReader(Protocol):
    """Subset of ``AsyncTrajectoryReader`` the snapshot loader depends on."""

    async def list_subjects(
        self,
        session_id: str,
        *,
        after_revision: int = 0,
    ) -> tuple[list[dict[str, Any]], str, int]:
        """List the execution subjects of one session."""
        ...

    async def get_subject_records(
        self,
        session_id: str,
        subject_id: str,
        *,
        since_revision: int,
        limit: int,
        max_bytes: int = ...,
    ) -> dict[str, Any] | None:
        """Read one page of a subject chain."""
        ...

    async def resolve_sequences(
        self,
        session_id: str,
        seq_hashes: Sequence[str],
        *,
        since_revision: int = 0,
    ) -> dict[str, Any] | None:
        """Resolve content-addressed chains."""
        ...


@dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    """One finished span exported from the store.

    Attributes:
        trace_id: 32-hex trace identifier.
        span_id: 16-hex span identifier.
        parent_span_id: Parent span identifier, if any.
        start_nano: Span start time in Unix nanoseconds.
        end_nano: Span end time in Unix nanoseconds (never before the start).
        otlp: OTLP payload holding exactly this span, with long ``@oj-seq``
            content already restored.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_nano: int
    end_nano: int
    otlp: dict[str, Any]


@dataclass(slots=True)
class SubjectTrajectory:
    """Finished records of one execution subject, ordered by start time."""

    subject_id: str
    display_name: str
    kind: str
    records: list[TrajectoryRecord] = field(default_factory=list)


@dataclass(slots=True)
class TrajectorySnapshot:
    """Every finished record of a session at one point in time."""

    captured_at_ns: int
    subjects: list[SubjectTrajectory] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SubjectRange:
    """Closed time interval of one subject, matched against node start times."""

    subject_id: str
    start_nano: int
    end_nano: int


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def rebuild_attributes(
    otlp: Mapping[str, Any],
    sequences: Mapping[str, Mapping[str, Any]],
    chains: Mapping[str, list[str]],
    blobs: Mapping[str, str],
) -> dict[str, Any]:
    """Return a copy of ``otlp`` with ``@oj-seq`` references replaced by content."""
    rebuilt: dict[str, str] = {}
    for key, reference in sequences.items():
        head = str(reference.get("hash") or "")
        elements = chains.get(head)
        if not elements:
            continue
        parts: list[str] = []
        for element in elements:
            content = blobs.get(element)
            if content is None:
                parts = []
                break
            parts.append(content)
        if parts:
            rebuilt[key] = "[" + ",".join(parts) + "]"
    if not rebuilt:
        return dict(otlp)
    copied = json.loads(json.dumps(otlp))
    for resource in copied.get("resourceSpans") or []:
        for scope in (resource.get("scopeSpans") or []) if isinstance(resource, dict) else []:
            for span in (scope.get("spans") or []) if isinstance(scope, dict) else []:
                for attribute in (span.get("attributes") or []) if isinstance(span, dict) else []:
                    _replace_sequence_attribute(attribute, rebuilt)
    return copied


def _replace_sequence_attribute(attribute: Any, rebuilt: Mapping[str, str]) -> None:
    """Swap one ``@oj-seq`` placeholder attribute for its rebuilt content, in place."""
    if not isinstance(attribute, dict):
        return
    key = attribute.get("key")
    value = attribute.get("value")
    if key not in rebuilt or not isinstance(value, dict):
        return
    content = value.get("stringValue")
    if isinstance(content, str) and content.startswith(_SEQUENCE_PREFIX):
        value["stringValue"] = rebuilt[key]


def _split_records(
    otlp: Mapping[str, Any],
    envelope: Mapping[str, Any],
    subject_id: str,
) -> list[TrajectoryRecord]:
    """Split one OTLP payload into one record per span."""
    records: list[TrajectoryRecord] = []
    for resource in otlp.get("resourceSpans") or []:
        if not isinstance(resource, Mapping):
            continue
        for scope in resource.get("scopeSpans") or []:
            if not isinstance(scope, Mapping):
                continue
            for span in scope.get("spans") or []:
                if not isinstance(span, Mapping):
                    continue
                trace_id = str(span.get("traceId") or envelope.get("trace_id") or "")
                span_id = str(span.get("spanId") or envelope.get("span_id") or "")
                try:
                    StepRef(trace_id=trace_id, span_id=span_id, subject_id=subject_id)
                except ValueError:
                    continue
                start = _to_int(span.get("startTimeUnixNano"))
                end = max(_to_int(span.get("endTimeUnixNano")), start)
                parent = span.get("parentSpanId")
                payload = {
                    "resourceSpans": [
                        {
                            **{key: value for key, value in resource.items() if key != "scopeSpans"},
                            "scopeSpans": [
                                {
                                    **{key: value for key, value in scope.items() if key != "spans"},
                                    "spans": [span],
                                },
                            ],
                        },
                    ],
                }
                records.append(
                    TrajectoryRecord(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=str(parent) if parent else None,
                        start_nano=start,
                        end_nano=end,
                        otlp=payload,
                    ),
                )
    return records


def _sequence_heads(records: list[Mapping[str, Any]]) -> list[str]:
    """Return the sorted, distinct chain heads referenced by the ``sequences`` of ``records``."""
    heads: set[str] = set()
    for row in records:
        for reference in (row.get("sequences") or {}).values():
            if isinstance(reference, Mapping) and reference.get("hash"):
                heads.add(str(reference.get("hash")))
    return sorted(heads)


async def load_trajectory_snapshot(
    reader: SubjectReader,
    session_id: str,
) -> tuple[TrajectorySnapshot, list[StepRef]]:
    """Export every finished span of a session.

    Spans whose latest record is still running are not exported; they are
    returned separately so the host can tell the user they were set aside.

    Args:
        reader: Read-only trajectory reader.
        session_id: Session to read.

    Returns:
        The snapshot of finished records and the references of running spans.
    """
    items, _epoch, _watermark = await reader.list_subjects(session_id, after_revision=0)
    snapshot = TrajectorySnapshot(captured_at_ns=time.time_ns())
    excluded_running: list[StepRef] = []
    chains: dict[str, list[str]] = {}
    blobs: dict[str, str] = {}
    for item in items:
        subject_id = str(item.get("subject_id") or "")
        if not subject_id:
            continue
        latest: dict[str, tuple[TrajectoryRecord, str]] = {}
        cursor = 0
        for _ in range(_MAX_RECORD_PAGES_PER_SUBJECT):
            page = await reader.get_subject_records(
                session_id,
                subject_id,
                since_revision=cursor,
                limit=_RECORD_PAGE_SIZE,
            )
            if page is None:
                break
            records = [row for row in page.get("records") or [] if isinstance(row, Mapping)]
            heads = _sequence_heads(records)
            missing = [head for head in heads if head not in chains]
            if missing:
                resolved = await reader.resolve_sequences(session_id, missing)
                if resolved:
                    chains.update(
                        {str(k): list(v) for k, v in (resolved.get("sequences") or {}).items()},
                    )
                    blobs.update({str(k): str(v) for k, v in (resolved.get("blobs") or {}).items()})
            for row in records:
                otlp = row.get("otlp")
                if not isinstance(otlp, Mapping):
                    continue
                rebuilt = rebuild_attributes(otlp, row.get("sequences") or {}, chains, blobs)
                lifecycle = str(row.get("lifecycle") or "")
                for record in _split_records(rebuilt, row, subject_id):
                    latest[f"{record.trace_id}:{record.span_id}"] = (record, lifecycle)
            if not page.get("has_more"):
                break
            cursor = _to_int(page.get("next_since_revision"), cursor)
        trajectory = SubjectTrajectory(
            subject_id=subject_id,
            display_name=str(item.get("display_name") or subject_id),
            kind=str(item.get("kind") or ""),
        )
        for record, lifecycle in latest.values():
            if lifecycle == _LIFECYCLE_RUNNING:
                excluded_running.append(
                    StepRef(
                        trace_id=record.trace_id,
                        span_id=record.span_id,
                        subject_id=subject_id,
                    ),
                )
            else:
                trajectory.records.append(record)
        trajectory.records.sort(key=lambda record: (record.start_nano, record.span_id))
        snapshot.subjects.append(trajectory)
    return snapshot, excluded_running


def resolve_ranges(snapshot: TrajectorySnapshot, scope: DiagnosisScope) -> list[SubjectRange]:
    """Derive the per-subject time ranges of a scope.

    A full scope yields one range per subject covering all of its finished
    records. A selection keeps each requested range that contains the start of
    at least one finished record; ranges without records are dropped.

    Args:
        snapshot: Snapshot of the finished records.
        scope: Requested scope.

    Returns:
        At most one range per subject.

    Raises:
        DiagnosisHostError: ``EMPTY_SCOPE`` when nothing is left to diagnose.
    """
    populated = {subject.subject_id: subject for subject in snapshot.subjects if subject.records}
    ranges: list[SubjectRange] = []
    if scope.kind == "full":
        for subject in populated.values():
            ranges.append(
                SubjectRange(
                    subject_id=subject.subject_id,
                    start_nano=min(record.start_nano for record in subject.records),
                    end_nano=max(record.end_nano for record in subject.records),
                ),
            )
    else:
        for item in scope.ranges:
            subject = populated.get(item.subject_id)
            if subject is None:
                continue
            start = int(item.start_unix_nano)
            end = int(item.end_unix_nano)
            if any(start <= record.start_nano <= end for record in subject.records):
                ranges.append(SubjectRange(subject_id=item.subject_id, start_nano=start, end_nano=end))
    if not ranges:
        raise DiagnosisHostError("EMPTY_SCOPE", "no finished steps in the selected scope", 422)
    return ranges


def count_records_in_ranges(snapshot: TrajectorySnapshot, ranges: Sequence[SubjectRange]) -> int:
    """Count finished records whose start lies inside their subject's range."""
    window = {item.subject_id: item for item in ranges}
    total = 0
    for subject in snapshot.subjects:
        item = window.get(subject.subject_id)
        if item is None:
            continue
        total += sum(1 for record in subject.records if item.start_nano <= record.start_nano <= item.end_nano)
    return total


@dataclass(slots=True)
class ScopeIndex:
    """Lookup that decides whether a step reference belongs to the diagnosed scope."""

    _spans: dict[tuple[str, str, str], tuple[int, int]]
    _ranges: dict[str, SubjectRange]

    @classmethod
    def build(cls, snapshot: TrajectorySnapshot, ranges: Sequence[SubjectRange]) -> ScopeIndex:
        """Index the snapshot records and the per-subject ranges."""
        spans = {
            (subject.subject_id, record.trace_id, record.span_id): (record.start_nano, record.end_nano)
            for subject in snapshot.subjects
            for record in subject.records
        }
        return cls(_spans=spans, _ranges={item.subject_id: item for item in ranges})

    def clamp(self, subject_id: str, start_nano: int, end_nano: int) -> tuple[int, int] | None:
        """Return ``[start, end]`` cut to the subject's range, or ``None`` when nothing is left."""
        window = self._ranges.get(subject_id)
        if window is None:
            return None
        start = max(start_nano, window.start_nano)
        end = min(end_nano, window.end_nano)
        return (start, end) if start <= end else None

    def admits(self, ref: StepRef) -> bool:
        """Return whether ``ref`` points at a node of a record inside the scope."""
        window = self._ranges.get(ref.subject_id)
        times = self._spans.get((ref.subject_id, ref.trace_id, ref.span_id))
        if window is None or times is None:
            return False
        if ref.node_id is None or not is_valid_node_id(ref.node_id, ref.subject_id):
            return False
        start, end = times
        return start <= window.end_nano and end >= window.start_nano


__all__ = [
    "ScopeIndex",
    "SubjectRange",
    "SubjectReader",
    "SubjectTrajectory",
    "TrajectoryRecord",
    "TrajectorySnapshot",
    "count_records_in_ranges",
    "load_trajectory_snapshot",
    "rebuild_attributes",
    "resolve_ranges",
]
