"""Thread-safe lookup and request-attribute inheritance for active spans."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor


_INHERITED_CONTEXT_ATTRIBUTES = frozenset(
    {
        "session.id",
        "gen_ai.conversation.id",
        "gen_ai.agent.name",
        "user.id",
        "domain.id",
        "app.id",
        "service.version",
        "jiuwenclaw.claw.id",
        "jiuwenclaw.channel.id",
        "jiuwenclaw.session.id",
        "jiuwenclaw.user.id",
        "jiuwenclaw.domain.id",
        "jiuwenclaw.app.id",
        "jiuwenclaw.request.id",
        "jiuwenclaw.agent.name",
        "jiuwenclaw.agent.parent",
        "jiuwenclaw.iteration",
        "jiuwenclaw.mode",
        "jiuwenclaw.adapter.mode",
    }
)


@dataclass(frozen=True)
class _ActiveSpan:
    span: Span
    parent_span_id: int | None
    started_at: float
    sequence: int


@dataclass
class _TraceAttributes:
    attributes: dict[str, Any]
    updated_at: float
    sequence: int


def _has_value(value: object) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, tuple)) and not value:
        return False
    return True


class SpanRegistryProcessor(SpanProcessor):
    """Index active SDK spans and enrich children without owning their lifecycle."""

    def __init__(self, *, max_spans: int, ttl_seconds: float) -> None:
        if max_spans <= 0:
            raise ValueError("max_spans must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_spans = max_spans
        self._ttl_seconds = ttl_seconds
        self._spans: dict[tuple[int, int], _ActiveSpan] = {}
        self._trace_attributes: dict[int, _TraceAttributes] = {}
        self._sequence = 0
        self._lock = RLock()

    def bind_trace_attributes(self, trace_id: int, attributes: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            current = self._trace_attributes.get(trace_id)
            merged = dict(current.attributes) if current is not None else {}
            merged.update(attributes)
            self._trace_attributes[trace_id] = _TraceAttributes(
                attributes=merged,
                updated_at=now,
                sequence=self._next_sequence_locked(),
            )
            self._limit_trace_attributes_locked()

    def find(self, trace_id: int, span_id: int) -> Span | None:
        with self._lock:
            entry = self._spans.get((trace_id, span_id))
            return entry.span if entry is not None else None

    def find_latest(
        self,
        trace_id: int,
        *,
        name: str | None = None,
        name_prefix: str | None = None,
        parent_span_id: int | None = None,
    ) -> Span | None:
        with self._lock:
            matches = []
            for (entry_trace_id, _), entry in self._spans.items():
                if entry_trace_id != trace_id:
                    continue
                if name is not None and entry.span.name != name:
                    continue
                if name_prefix is not None and not entry.span.name.startswith(
                    name_prefix
                ):
                    continue
                if (
                    parent_span_id is not None
                    and entry.parent_span_id != parent_span_id
                ):
                    continue
                matches.append(entry)
            latest = max(matches, key=lambda item: item.sequence, default=None)
            return latest.span if latest is not None else None

    def find_parent(self, span: Span, *, name_prefix: str) -> Span | None:
        span_context = span.get_span_context()
        parent = span.parent
        parent_span_id = parent.span_id if parent is not None else None
        visited: set[int] = set()
        with self._lock:
            while parent_span_id is not None and parent_span_id not in visited:
                visited.add(parent_span_id)
                entry = self._spans.get((span_context.trace_id, parent_span_id))
                if entry is None:
                    return None
                if entry.span.name.startswith(name_prefix):
                    return entry.span
                parent_span_id = entry.parent_span_id
        return None

    def active_count(self) -> int:
        with self._lock:
            return len(self._spans)

    def prune(self, now: float | None = None) -> int:
        prune_at = time.monotonic() if now is None else now
        with self._lock:
            return self._prune_locked(prune_at)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        now = time.monotonic()
        span_context = span.get_span_context()
        parent = span.parent
        parent_span_id = parent.span_id if parent is not None else None
        with self._lock:
            self._prune_locked(now)
            inherited: dict[str, Any] = {}
            trace_attributes = self._trace_attributes.get(span_context.trace_id)
            if trace_attributes is not None:
                inherited.update(trace_attributes.attributes)
            if parent_span_id is not None:
                parent_entry = self._spans.get(
                    (span_context.trace_id, parent_span_id)
                )
                if parent_entry is not None:
                    for key, value in parent_entry.span.attributes.items():
                        if key in _INHERITED_CONTEXT_ATTRIBUTES and _has_value(value):
                            inherited[key] = value

            existing = span.attributes
            for key, value in inherited.items():
                if not _has_value(value):
                    continue
                if existing is not None and _has_value(existing.get(key)):
                    continue
                try:
                    span.set_attribute(key, value)
                except (TypeError, ValueError):
                    continue

            key = (span_context.trace_id, span_context.span_id)
            self._spans[key] = _ActiveSpan(
                span=span,
                parent_span_id=parent_span_id,
                started_at=now,
                sequence=self._next_sequence_locked(),
            )
            self._limit_spans_locked()

    def on_end(self, span: ReadableSpan) -> None:
        span_context = span.get_span_context()
        key = (span_context.trace_id, span_context.span_id)
        with self._lock:
            self._spans.pop(key, None)

    def shutdown(self) -> None:
        with self._lock:
            self._spans.clear()
            self._trace_attributes.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True

    def _next_sequence_locked(self) -> int:
        self._sequence += 1
        return self._sequence

    def _prune_locked(self, now: float) -> int:
        expired_spans = [
            key
            for key, entry in self._spans.items()
            if now - entry.started_at >= self._ttl_seconds
        ]
        expired_traces = [
            trace_id
            for trace_id, entry in self._trace_attributes.items()
            if now - entry.updated_at >= self._ttl_seconds
        ]
        for key in expired_spans:
            del self._spans[key]
        for trace_id in expired_traces:
            del self._trace_attributes[trace_id]
        return len(expired_spans) + len(expired_traces)

    def _limit_spans_locked(self) -> None:
        while len(self._spans) > self._max_spans:
            oldest = min(self._spans, key=lambda key: self._spans[key].sequence)
            del self._spans[oldest]

    def _limit_trace_attributes_locked(self) -> None:
        while len(self._trace_attributes) > self._max_spans:
            oldest = min(
                self._trace_attributes,
                key=lambda trace_id: self._trace_attributes[trace_id].sequence,
            )
            del self._trace_attributes[oldest]


__all__ = ["SpanRegistryProcessor"]
