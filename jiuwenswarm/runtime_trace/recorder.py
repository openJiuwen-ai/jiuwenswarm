# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime span recorder and its local tracer provider.

Every OpenTelemetry / OpenJiuwen import happens inside a function so importing
this module while the feature is disabled adds no heavy dependency.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import threading
import time
from typing import Any

from jiuwenswarm.runtime_trace.config import (
    _EXPORT_BATCH_SIZE,
    _EXPORT_MAX_QUEUE,
    _EXPORT_TIMEOUT_MS,
    _MAX_OPEN_HANDLES,
    RuntimeTraceSettings,
)

logger = logging.getLogger(__name__)

_LEVELS = frozenset({"root", "terminal", "event", "normal"})
_OUTCOMES = frozenset({"success", "error", "cancelled", "slow"})

_REASON_CODES = frozenset(
    {
        "ws_bind_failed",
        "ipc_timeout",
        "ipc_eof",
        "probe_failed",
        "loop_stall",
        "process_exit",
        "budget_exhausted",
        "cancel_requested",
        "ready_timeout",
        "runtime_load_failed",
        "bring_up_failed",
        "worker_reported_failed",
        "dispatch_stuck",
        "shutdown_timeout",
    }
)

# Attributes an instrumentation site may add. Base attributes are injected by
# the recorder and always win over these.
_ATTRIBUTE_WHITELIST = frozenset(
    {
        "runtime.worker_instance",
        "runtime.generation",
        "runtime.reason_code",
        "runtime.attempt",
        "runtime.budget",
        "runtime.backoff_ms",
        "runtime.count",
        "runtime.elapsed_ms",
        "runtime.stall_seconds",
        "runtime.launch",
        "runtime.worker_pid",
        "runtime.exit_code",
        "runtime.stage",
    }
)

_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
)
_LAUNCH_RE = re.compile(r"^[0-9a-f]{32}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DROP = object()

_WARN_INTERVAL_SECONDS = 60.0


def _hex32(value: int) -> str:
    return f"{int(value):032x}"


def _hex16(value: int) -> str:
    return f"{int(value):016x}"


def format_traceparent(span_context: Any) -> str:
    """Format one span context as W3C traceparent; empty when invalid."""
    if span_context is None or not getattr(span_context, "is_valid", False):
        return ""
    return (
        f"00-{_hex32(span_context.trace_id)}-"
        f"{_hex16(span_context.span_id)}-{int(span_context.trace_flags):02x}"
    )


def resolve_root_context() -> Any:
    """Return the parent context shared by every root span in this run.

    A valid ``TRACEPARENT`` supplies the upstream trace id and parent span id.
    Without one, this process generates a run-scoped trace id and exposes it
    through a virtual (non-recording) parent span. Every root of this run then
    shares that trace id, so Front and its spawned Worker can be viewed as one
    trace; the virtual parent itself is never exported.
    """
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        get_current_span,
        set_span_in_context,
    )
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    value = os.environ.get("TRACEPARENT", "").strip().lower()
    if _TRACEPARENT_RE.match(value):
        context = TraceContextTextMapPropagator().extract(
            {"traceparent": value}
        )
        # The W3C propagator also rejects some syntactically shaped values
        # (for example version ff). Only reuse a context that produced a
        # valid upstream span.
        if get_current_span(context).get_span_context().is_valid:
            return context

    trace_id = secrets.randbits(128) or 1
    span_id = secrets.randbits(64) or 1
    parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(parent))


class SpanKinds:
    """Lock-protected ``span_id -> (level, name, outcome)`` registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, str, str | None]] = {}

    def put(self, span_id: str, level: str, name: str) -> None:
        with self._lock:
            self._entries[span_id] = (level, name, None)

    def set_outcome(self, span_id: str, outcome: str) -> None:
        with self._lock:
            entry = self._entries.get(span_id)
            if entry is not None:
                self._entries[span_id] = (entry[0], entry[1], outcome)

    def take(self, span_id: str) -> tuple[str, str, str | None]:
        with self._lock:
            return self._entries.pop(span_id, ("normal", "", None))

    def discard(self, span_id: str) -> None:
        with self._lock:
            self._entries.pop(span_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def evict_oldest(self, levels: tuple[str, ...]) -> int:
        """Evict the oldest registrations of *levels* (dict keeps insert order)."""
        with self._lock:
            victims = [
                span_id
                for span_id, entry in self._entries.items()
                if entry[0] in levels
            ]
            limit = _MAX_OPEN_HANDLES // 8
            for span_id in victims[:limit]:
                del self._entries[span_id]
            return min(len(victims), limit)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class SpanHandle:
    """One started span plus the monotonic data needed to end it."""

    __slots__ = ("span", "name", "mono_start", "level", "ended")

    def __init__(self, span: Any, name: str, mono_start: int, level: str) -> None:
        self.span = span
        self.name = name
        self.mono_start = mono_start
        self.level = level
        self.ended = False


class _NullHandle(SpanHandle):
    """Returned when span creation fails; downstream calls become no-ops."""

    def __init__(self) -> None:
        super().__init__(span=None, name="", mono_start=0, level="normal")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sanitize_value(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            return _DROP
    elif isinstance(value, float):
        if not math.isfinite(value):
            return _DROP
    elif isinstance(value, str):
        if len(value) > 128:
            return _DROP
    else:
        return _DROP

    if key == "runtime.reason_code":
        return value if isinstance(value, str) and value in _REASON_CODES else _DROP
    if key == "runtime.launch":
        return value if isinstance(value, str) and _LAUNCH_RE.match(value) else _DROP
    if key == "runtime.worker_instance":
        return value if isinstance(value, str) and _INSTANCE_RE.match(value) else _DROP
    if key in {"runtime.generation", "runtime.backoff_ms", "runtime.count"}:
        return value if _is_int(value) and value >= 0 else _DROP
    if key in {"runtime.attempt", "runtime.budget", "runtime.worker_pid"}:
        return value if _is_int(value) and value >= 1 else _DROP
    if key == "runtime.exit_code":
        return value if _is_int(value) else _DROP
    if key in {"runtime.elapsed_ms", "runtime.stall_seconds"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _DROP
        return round(float(value), 1) if float(value) >= 0 else _DROP
    if key == "runtime.stage":
        return value if isinstance(value, str) else _DROP
    return value


class RuntimeRecorder:
    """Start and end spans with explicit times, never as the business span.

    All public methods swallow their own failures so a recording problem can
    never change startup, shutdown, or ACK behaviour.
    """

    def __init__(
        self,
        *,
        tracer: Any,
        kinds: SpanKinds,
        clock: Any,
        base_attrs: dict[str, Any],
        root_context: Any,
        on_worker_meta: Any = None,
    ) -> None:
        self._tracer = tracer
        self._kinds = kinds
        self._clock = clock
        self._base_attrs = dict(base_attrs)
        self._root_context = root_context
        self._on_worker_meta = on_worker_meta
        self.startup_handle: SpanHandle | None = None
        self.shutdown_handle: SpanHandle | None = None
        # Last completed Worker startup stage; signals.agent_ready uses it to
        # account for the wait between "ready" and the readiness callback.
        self.last_stage_mono: int | None = None
        self._state_lock = threading.Lock()
        self._failures = 0
        self._invalid_attributes = 0
        self._dropped_events = 0
        self._last_warning = 0.0

    # -- public surface -------------------------------------------------

    def start(
        self,
        name: str,
        *,
        parent: SpanHandle | None = None,
        level: str = "normal",
        mono_start: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> SpanHandle:
        try:
            return self._start(
                name,
                parent=parent,
                level=level,
                mono_start=mono_start,
                attributes=attributes,
            )
        except Exception:  # noqa: BLE001 - recording must never reach business code
            self._note_failure("start")
            return _NullHandle()

    def end(
        self,
        handle: SpanHandle,
        outcome: str,
        *,
        mono_end: int | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._end(handle, outcome, mono_end=mono_end, attributes=attributes)
        except Exception:  # noqa: BLE001
            self._note_failure("end")

    def event(
        self,
        handle: SpanHandle | None,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._event(handle, name, attributes)
        except Exception:  # noqa: BLE001
            self._note_failure("event")

    def set_worker_instance(self, instance: str) -> None:
        try:
            if not isinstance(instance, str) or not _INSTANCE_RE.match(instance):
                return
            with self._state_lock:
                self._base_attrs["runtime.worker_instance"] = instance
            if self._on_worker_meta is not None:
                self._on_worker_meta(instance, None)
        except Exception:  # noqa: BLE001
            self._note_failure("set_worker_instance")

    def set_worker_generation(self, generation: int | None) -> None:
        try:
            if generation is None:
                return
            if isinstance(generation, bool) or not isinstance(generation, int):
                return
            if generation < 0:
                return
            with self._state_lock:
                self._base_attrs["runtime.generation"] = generation
            if self._on_worker_meta is not None:
                self._on_worker_meta(None, generation)
        except Exception:  # noqa: BLE001
            self._note_failure("set_worker_generation")

    # -- internals ------------------------------------------------------

    def _attrs(
        self,
        attributes: dict[str, Any] | None,
        *,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            merged = dict(self._base_attrs)
        if attributes:
            for key, value in attributes.items():
                if key not in _ATTRIBUTE_WHITELIST:
                    self._invalid_attributes += 1
                    continue
                if key in merged:
                    # Base values win over instrumentation-provided duplicates.
                    continue
                sanitized = _sanitize_value(key, value)
                if sanitized is _DROP:
                    self._invalid_attributes += 1
                    continue
                merged[key] = sanitized
        if outcome is not None:
            merged["runtime.outcome"] = outcome
        return merged

    def _start(
        self,
        name: str,
        *,
        parent: SpanHandle | None,
        level: str,
        mono_start: int | None,
        attributes: dict[str, Any] | None,
    ) -> SpanHandle:
        from opentelemetry.trace import set_span_in_context

        if level not in _LEVELS:
            level = "normal"
        started_mono = mono_start if isinstance(mono_start, int) else time.monotonic_ns()

        if self._kinds.size() > _MAX_OPEN_HANDLES:
            evicted = self._kinds.evict_oldest(("normal", "event"))
            if evicted:
                logger.warning(
                    "runtime trace: evicted %d stale span registrations", evicted
                )

        if parent is not None and parent.span is not None and not parent.ended:
            context = set_span_in_context(parent.span)
        else:
            context = self._root_context

        span = self._tracer.start_span(
            name,
            context=context,
            start_time=self._clock.to_unix_nano(started_mono),
            record_exception=False,
            set_status_on_exception=False,
            attributes=self._attrs(attributes),
        )
        span_id = _hex16(span.get_span_context().span_id)
        self._kinds.put(span_id, level, name)
        return SpanHandle(
            span=span, name=name, mono_start=started_mono, level=level
        )

    def _end(
        self,
        handle: SpanHandle | None,
        outcome: str,
        *,
        mono_end: int | None,
        attributes: dict[str, Any] | None,
    ) -> None:
        if handle is None or handle.span is None:
            return
        if handle.ended:
            logger.warning("runtime trace: span %s ended twice", handle.name)
            return
        handle.ended = True
        if outcome not in _OUTCOMES:
            self._invalid_attributes += 1
            outcome = "error"
        ended_mono = mono_end if isinstance(mono_end, int) else time.monotonic_ns()
        span_id = _hex16(handle.span.get_span_context().span_id)
        self._kinds.set_outcome(span_id, outcome)
        ended_attrs = self._attrs(attributes, outcome=outcome)
        if ended_attrs:
            handle.span.set_attributes(ended_attrs)
        handle.span.end(end_time=self._clock.to_unix_nano(ended_mono))
        # The processor's on_end consumes the registry entry synchronously. The
        # discard is only a safety net for a failed or missing consumer.
        self._kinds.discard(span_id)

    def _event(
        self,
        handle: SpanHandle | None,
        name: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        if handle is None or handle.span is None or handle.ended:
            self._dropped_events += 1
            logger.debug("runtime trace: event %s dropped; root is closed", name)
            return
        handle.span.add_event(
            name,
            attributes=self._attrs(attributes),
            timestamp=self._clock.to_unix_nano(time.monotonic_ns()),
        )

    def _note_failure(self, action: str) -> None:
        self._failures += 1
        now = time.monotonic()
        if now - self._last_warning < _WARN_INTERVAL_SECONDS:
            return
        self._last_warning = now
        logger.warning(
            "runtime trace: %s failed (%d failures so far)", action, self._failures,
            exc_info=True,
        )


def build_runtime_tracer(
    service_name: str, settings: RuntimeTraceSettings
) -> tuple[Any, Any]:
    """Build the process-local provider and its record processor."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanLimits, SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    from openjiuwen.extensions.observability.span_record_processor import (
        SpanRecordProcessor,
    )

    class _SafeSpanProcessor(SpanProcessor):
        """Local guard equivalent to Core's wrapper.

        Importing ``openjiuwen.extensions.observability.runtime`` for its
        ``SafeSpanProcessor`` pulls the whole Langfuse/export/instrumentation
        chain and adds seconds to Front's cold start. This process only needs
        exception isolation, so the wrapper stays local and uses the standard
        ``logging`` module (no Core LogManager initialization).
        """

        def __init__(self, inner: SpanProcessor) -> None:
            self._inner = inner
            self._shutdown_called = False

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            try:
                self._inner.on_start(span, parent_context=parent_context)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "runtime trace: processor on_start failed", exc_info=True
                )

        def on_end(self, span: Any) -> None:
            try:
                self._inner.on_end(span)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "runtime trace: processor on_end failed", exc_info=True
                )

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            try:
                result = self._inner.force_flush(timeout_millis)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "runtime trace: processor force_flush failed", exc_info=True
                )
                return True
            return True if result is None else bool(result)

        def shutdown(self) -> None:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            try:
                self._inner.shutdown()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "runtime trace: processor shutdown failed", exc_info=True
                )

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ALWAYS_ON,
        shutdown_on_exit=False,
        span_limits=SpanLimits(max_attributes=32, max_attribute_length=512),
    )
    processor = SpanRecordProcessor()
    provider.add_span_processor(_SafeSpanProcessor(processor))

    if settings.exporter != "none":
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )

        exporter = _build_exporter(settings)
        if exporter is not None:
            if settings.exporter == "console":
                span_processor: Any = SimpleSpanProcessor(exporter)
            else:
                span_processor = BatchSpanProcessor(
                    exporter,
                    max_queue_size=_EXPORT_MAX_QUEUE,
                    max_export_batch_size=_EXPORT_BATCH_SIZE,
                    # Kept for API compatibility only; OTel 1.35+ no longer
                    # passes it to the exporter. The explicit exporter timeout
                    # below is what bounds a single batch.
                    export_timeout_millis=_EXPORT_TIMEOUT_MS,
                )
            provider.add_span_processor(_SafeSpanProcessor(span_processor))
    return provider, processor


def _build_exporter(settings: RuntimeTraceSettings) -> Any:
    """Build the optional external exporter; failures degrade to local-only."""
    try:
        headers = dict(settings.headers)
        timeout_seconds = _EXPORT_TIMEOUT_MS / 1000.0
        if settings.exporter == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            return ConsoleSpanExporter()
        if settings.exporter == "otlp_grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            return OTLPSpanExporter(
                endpoint=settings.endpoint or "http://127.0.0.1:4317",
                insecure=True,
                headers=headers,
                timeout=timeout_seconds,
            )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )

        return HttpExporter(
            endpoint=settings.endpoint or "http://127.0.0.1:4318/v1/traces",
            headers=headers,
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 - external export is best effort
        logger.warning(
            "runtime trace: exporter init failed; local-only", exc_info=True
        )
        return None
