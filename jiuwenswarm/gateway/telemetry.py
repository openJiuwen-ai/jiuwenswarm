# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped tracing for the gateway."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import threading
from copy import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

logger = logging.getLogger(__name__)


def _is_empty_setting(value: object) -> bool:
    """Whether a trace setting is absent or an empty string."""
    return value is None or (isinstance(value, str) and not value.strip())


def _matches_attached_parent(
    attached: object | None,
    current: object | None,
    request_id: str,
    channel: str,
    method: str,
) -> bool:
    """Whether current is the derived child of the envelope's attached owner."""
    if attached is None or current is None:
        return False
    if current.parent_operation is not attached:
        return False
    return (current.request_id, current.channel, current.method) == (request_id, channel, method)


def _matches_envelope_operation(envelope: object, operation: object | None) -> bool:
    """Whether an envelope carries the identity of the current operation."""
    if operation is None or operation.finished:
        return False
    if envelope.request_id != operation.request_id:
        return False
    if str(getattr(envelope, "channel", operation.channel) or "") != operation.channel:
        return False
    return str(getattr(envelope, "method", operation.method) or "") == operation.method


_current = ContextVar("gateway_request_trace", default=None)
_request_trace_start = ContextVar("gateway_request_trace_start", default=None)
_response = ContextVar("gateway_response_trace", default=None)
_cleanup = ContextVar("gateway_cleanup_trace", default=None)
_root_trace_id = ContextVar("gateway_root_trace_id", default=0)
_runtime = None
_context_warning_at = float("-inf")
_TRACE_ID_PATTERN = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")
_TRACESTATE_MEMBER = re.compile(
    r"([a-z][_0-9a-z*/-]{0,255}|[a-z0-9][_0-9a-z*/-]{0,240}@[a-z][_0-9a-z*/-]{0,13})"
    r"=([\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e])",
)


def create_request_id_generator() -> object:
    """Use a supplied root trace ID only during request span creation."""
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    class RequestIdGenerator(RandomIdGenerator):
        def generate_trace_id(self) -> int:
            supplied_id = _root_trace_id.get()
            _root_trace_id.set(0)
            return supplied_id or super().generate_trace_id()

    return RequestIdGenerator()


def _create_request_sampler() -> object:
    """Apply the server root policy to browser parents, regardless of flags."""
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased

    name = os.environ.get("OTEL_TRACES_SAMPLER", "parentbased_always_on").lower()
    if name in ("always_on", "parentbased_always_on"):
        root = ALWAYS_ON
    elif name in ("always_off", "parentbased_always_off"):
        root = ALWAYS_OFF
    elif name in ("traceidratio", "parentbased_traceidratio"):
        rate = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1.0"))
        if not 0 <= rate <= 1:
            raise ValueError("Invalid gateway trace sampling ratio")

        class ServerRatioSampler(TraceIdRatioBased):
            def should_sample(
                self, parent_context: object, trace_id: int, name: str,
                kind: object | None = None, attributes: dict | None = None,
                links: object | None = None, trace_state: object | None = None,
            ) -> object:
                # Client-controlled IDs must not decide the server's sampling.
                sampling_id = RandomIdGenerator().generate_trace_id()
                return super().should_sample(parent_context, sampling_id, name, kind, attributes, links, trace_state)

        root = ServerRatioSampler(rate)
    else:
        raise ValueError("Unsupported gateway trace sampler")
    return ParentBased(root, remote_parent_sampled=root, remote_parent_not_sampled=root)


def _trace_context_warning(reason: str) -> None:
    """Rate-limit diagnostics without logging untrusted carrier values."""
    global _context_warning_at
    now = time.monotonic()
    if now - _context_warning_at >= 60:
        _context_warning_at = now
        logger.warning("Gateway trace context ignored: reason=%s", reason)


def _parse_trace_id(value: object) -> int:
    if isinstance(value, str) and len(value) <= 36 and _TRACE_ID_PATTERN.fullmatch(value):
        return int(value.replace("-", ""), 16)
    return 0


def _extract_request_context(carrier: dict | None) -> tuple[object, int, str]:
    """Resolve this message's W3C parent or a standalone trace identifier."""
    from opentelemetry import context, trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    empty = context.Context()
    if carrier is None:
        return empty, 0, "generated"
    if not isinstance(carrier, dict):
        _trace_context_warning("invalid_carrier")
        return empty, 0, "generated"
    supplied_id = 0
    for key in ("trace_id", "X-Trace-Id", "x-trace-id"):
        candidate = _parse_trace_id(carrier.get(key))
        if candidate and not supplied_id:
            supplied_id = candidate
        elif candidate and candidate != supplied_id:
            _trace_context_warning("conflicting_trace_id")
        if key in carrier and not candidate:
            _trace_context_warning("invalid_trace_id")
    parent = carrier.get("traceparent")
    if isinstance(parent, str) and len(parent) <= 512:
        propagator = TraceContextTextMapPropagator()
        parent_carrier = {"traceparent": parent}
        extracted = propagator.extract(parent_carrier, context=empty)
        span_context = trace.get_current_span(extracted).get_span_context()
        if span_context.is_valid:
            state = carrier.get("tracestate")
            if state:
                # Bound and preflight W3C members before the SDK parser, which
                # otherwise logs arbitrary invalid member values at WARNING.
                members = state.split(",") if isinstance(state, str) and len(state) <= 512 else []
                matches = [_TRACESTATE_MEMBER.fullmatch(member.strip(" \t"))
                           for member in members if member.strip(" \t")]
                if members and len(matches) <= 32 and all(matches):
                    keys = [match.group(1) for match in matches]
                    if len(keys) == len(set(keys)):
                        parent_carrier["tracestate"] = state
                        extracted = propagator.extract(parent_carrier, context=empty)
                    else:
                        _trace_context_warning("invalid_tracestate")
                else:
                    _trace_context_warning("invalid_tracestate")
            if supplied_id and supplied_id != span_context.trace_id:
                _trace_context_warning("conflicting_traceparent")
            return extracted, 0, "traceparent"
    if "traceparent" in carrier:
        _trace_context_warning("invalid_traceparent")
    return empty, supplied_id, "trace_id" if supplied_id else "generated"


@dataclass(eq=False)
class TraceOperation:
    """A single gateway request and its tracing context."""

    request_id: str
    method: str
    channel: str
    connection_id: str
    span: object
    context: object
    started_at: float
    finished: bool = False
    delivery_pending: bool = False
    safe_user_id: str = ""
    accepted: bool = False
    producer_done: bool = False
    pending_deliveries: int = 0
    written_frames: int = 0
    dropped_frames: int = 0
    received_frames: int = 0
    attempted_frames: int = 0
    client_queue_wait_ms: float = 0.0
    client_send_ms: float = 0.0
    outcome: str = "success"
    reason: str = ""
    on_finish: Callable[[], None] | None = None
    parent_operation: TraceOperation | None = None
    log_summaries: bool = True
    _events_seen: set[str] = field(default_factory=set)
    _event_count: int = 0

    @property
    def trace_id(self) -> str:
        """Return the trace identifier, or an empty string on SDK failure."""
        try:
            return format(self.span.get_span_context().trace_id, "032x")
        except Exception:
            return ""

    def event(self, name: str, attributes: dict | None = None) -> None:
        """Record a bounded event and selected local summaries."""
        if self.finished:
            return
        try:
            first_events = {
                "response.first", "response.received", "client.first_content",
                "client.write.first", "queue.dequeued", "response.dequeued",
            }
            if name in first_events and name in self._events_seen:
                return
            boundary_events = {"received", "send.returned", "client.written"}
            # Reserve one occurrence of each boundary after the stage budget.
            if self._event_count >= 64 and (name not in boundary_events or name in self._events_seen):
                return
            self._events_seen.add(name)
            self._event_count += 1
            allowed = {"elapsed_ms", "reason", "outcome", "status", "status_code", "count", "size", "duration_ms",
                       "lock_wait_ms", "send_ms", "queue_wait_ms", "response_wait_ms",
                       "response_received", "error_type", "instance_id", "attempt"}
            attrs = {
                k: v for k, v in (attributes or {}).items()
                if k in allowed and isinstance(v, (str, int, float, bool))
            }
            if self.log_summaries and name in boundary_events:
                summary = {
                    k: attrs[k]
                    for k in ("outcome", "elapsed_ms", "lock_wait_ms", "send_ms", "queue_wait_ms")
                    if k in attrs
                }
                summary.setdefault("elapsed_ms", round((time.monotonic() - self.started_at) * 1000, 3))
                fields = " ".join(f"{key}={value}" for key, value in summary.items())
                logger.info("event=%s trace_id=%s request_id=%s user_id=%s %s",
                            name, self.trace_id, self.request_id, self.safe_user_id, fields)
            self.span.add_event(name, attrs)
        except Exception:
            logger.debug("Unable to record gateway trace event", exc_info=True)

    def record_outcome(self, outcome: str, *, reason: str = "") -> None:
        """Keep failures even if a later frame or cleanup succeeds."""
        if self.finished:
            return
        priorities = {"success": 0, "incomplete": 1, "cancelled": 2, "failed": 3, "timeout": 4}
        if priorities.get(outcome, 1) > priorities.get(self.outcome, 0):
            self.outcome = outcome
            self.reason = reason
        elif outcome == self.outcome and reason and not self.reason:
            self.reason = reason

    def set_attribute(self, name: str, value: object) -> None:
        """Write an observation without exposing SDK failures to business code."""
        if self.finished:
            return
        try:
            self.span.set_attribute(name, value)
        except Exception:
            logger.debug("Unable to record gateway span attribute", exc_info=True)

    def retain_delivery(self) -> None:
        """Hold a queue item or an actual writer frame until it is resolved."""
        if not self.finished:
            self.pending_deliveries += 1
            self.delivery_pending = True

    def release_delivery(self, *, reason: str = "", count: int = 1) -> None:
        """Release observational references without changing queue behavior."""
        if reason and not self.finished:
            self.dropped_frames += count
            self.record_outcome("incomplete", reason=reason)
        self.pending_deliveries = max(0, self.pending_deliveries - count)
        if self.producer_done and self.pending_deliveries == 0 and not self.finished:
            if self.written_frames:
                self.event("client.written", {
                    "outcome": "written" if not self.dropped_frames else "partial", "count": self.written_frames,
                    "queue_wait_ms": round(self.client_queue_wait_ms, 3), "send_ms": round(self.client_send_ms, 3),
                })
            self.finish(self.outcome, reason=self.reason)

    def complete(self, outcome: str = "success", *, reason: str = "") -> None:
        """Finish producing responses, then wait for real delivery boundaries."""
        self.record_outcome(outcome, reason=reason)
        self.producer_done = True
        self.release_delivery(count=0)

    def finish(self, outcome: str, *, reason: str = "") -> None:
        """End this operation once, without changing business behavior."""
        if self.finished:
            return
        self.record_outcome(outcome, reason=reason)
        outcome, reason = self.outcome, self.reason
        self.finished = True
        if self.log_summaries and outcome != "success":
            try:
                logger.info("event=request.finished trace_id=%s request_id=%s user_id=%s outcome=%s reason=%s",
                            self.trace_id, self.request_id, self.safe_user_id, outcome, reason)
            except Exception:
                logger.debug("Unable to log gateway finish summary", exc_info=True)
        try:
            from opentelemetry.trace import Status, StatusCode
            self.span.set_attribute("gateway.outcome", outcome)
            self.span.set_attribute("gateway.frames_written", self.written_frames)
            self.span.set_attribute("gateway.frames_dropped", self.dropped_frames)
            if reason:
                self.span.set_attribute("gateway.reason", reason)
            if outcome in ("failed", "timeout"):
                self.span.set_status(Status(StatusCode.ERROR))
        except Exception:
            logger.debug("Unable to finish gateway trace", exc_info=True)
        finally:
            try:
                self.span.end()
            except Exception:
                logger.debug("Unable to end gateway span", exc_info=True)
            if self.on_finish is not None:
                callback, self.on_finish = self.on_finish, None
                try:
                    callback()
                except Exception:
                    logger.debug("Unable to release gateway trace owner", exc_info=True)


@dataclass
class GatewayTelemetry:
    """Gateway tracer and provider ownership."""

    tracer: object | None = None
    provider: object | None = None
    owned: bool = False
    user_id_formatter: Callable[[str], str] | None = None
    diagnostics: object | None = None


class _ExportDiagnostics:
    """Count observable exporter outcomes without inspecting the SDK queue."""

    def __init__(self, exporter: object) -> None:
        self.exporter = exporter
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.counts = {"ended": 0, "exported": 0, "export_failed": 0, "batches": 0,
                       "pending": 0, "admission_dropped": 0}
        self.warning_at = float("-inf")
        self.drop_warning_at = float("-inf")
        self.reported_drops = 0
        self.capacity = 0
        self.closing = False

    def export(self, spans: object) -> object:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            result = SpanExportResult.FAILURE if self.stopped.is_set() else self.exporter.export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        with self.lock:
            self.counts["batches"] += 1
            self.counts["exported" if result == SpanExportResult.SUCCESS else "export_failed"] += len(spans)
            self.counts["pending"] -= len(spans)
            now = time.monotonic()
            warn = result != SpanExportResult.SUCCESS and now - self.warning_at >= 60
            if warn:
                self.warning_at = now
        if warn:
            logger.warning("Gateway trace export failed; spans were not confirmed")
        self.report_drops()
        return result

    def report_drops(self) -> None:
        with self.lock:
            now = time.monotonic()
            count = self.counts["admission_dropped"]
            warn = count > self.reported_drops and now - self.drop_warning_at >= 60
            if warn:
                self.drop_warning_at = now
                self.reported_drops = count
        if warn:
            logger.warning("Gateway trace admission limit reached; spans dropped")

    def shutdown(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)


class _CountingProcessor:
    def __init__(self, processor: object, diagnostics: _ExportDiagnostics) -> None:
        self.processor = processor
        self.diagnostics = diagnostics

    def on_start(self, span: object, parent_context: object = None) -> None:
        self.processor.on_start(span, parent_context)

    def on_end(self, span: object) -> None:
        if not (span.context and span.context.trace_flags.sampled):
            return
        diagnostics = self.diagnostics
        with diagnostics.lock:
            diagnostics.counts["ended"] += 1
            admitted = not diagnostics.closing and diagnostics.counts["pending"] < diagnostics.capacity
            if admitted:
                diagnostics.counts["pending"] += 1
                try:
                    self.processor.on_end(span)
                except Exception:
                    diagnostics.counts["pending"] -= 1
                    diagnostics.counts["admission_dropped"] += 1
            else:
                diagnostics.counts["admission_dropped"] += 1

    def shutdown(self) -> None:
        with self.diagnostics.lock:
            self.diagnostics.closing = True
        self.processor.shutdown()
        self.diagnostics.report_drops()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.processor.force_flush(timeout_millis)


def gateway_telemetry_stats() -> dict[str, int]:
    """Return observed outcomes; differences are unconfirmed, not exact drops."""
    diagnostics = _runtime.diagnostics if _runtime else None
    if diagnostics is None:
        return {}
    with diagnostics.lock:
        result = dict(diagnostics.counts)
        result["unconfirmed"] = result["pending"] + result["export_failed"] + result["admission_dropped"]
        return result


def _batch_options() -> dict[str, int]:
    values = {}
    for name, default, maximum in (
        ("MAX_QUEUE_SIZE", 2048, 65536),
        ("MAX_EXPORT_BATCH_SIZE", 512, 4096),
        ("SCHEDULE_DELAY", 5000, 60000),
        ("EXPORT_TIMEOUT", 5000, 5000),
    ):
        raw = os.environ.get("OTEL_BSP_" + name)
        try:
            value = default if raw is None else int(raw)
            if not 0 < value <= maximum:
                raise ValueError
        except ValueError:
            logger.warning("Invalid Gateway batch setting: %s; using default", name)
            value = default
        values[name] = value
    if values.get("MAX_EXPORT_BATCH_SIZE", 0) > values.get("MAX_QUEUE_SIZE", 0):
        logger.warning("Gateway batch size exceeds queue size; using queue size")
        values["MAX_EXPORT_BATCH_SIZE"] = min(512, values.get("MAX_QUEUE_SIZE", 0))
    return values


@dataclass
class _CleanupObservation:
    operation: TraceOperation
    results: dict[str, tuple[str, str]] = field(default_factory=dict)


def configure_gateway_telemetry(
    config: dict, *, user_id_formatter: Callable[[str], str] | None = None,
) -> GatewayTelemetry:
    """Initialize a dedicated provider without replacing the global SDK."""
    global _runtime
    if _runtime is not None:
        return _runtime
    runtime = GatewayTelemetry(user_id_formatter=user_id_formatter)
    _runtime = runtime
    settings = config.get("telemetry") if isinstance(config, dict) else None
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        logger.warning("Gateway tracing unavailable: telemetry must be a mapping")
        return runtime
    enabled = os.environ.get("OTEL_ENABLED", settings.get("enabled", False))
    if isinstance(enabled, str):
        value = enabled.strip().lower()
        if value not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            logger.warning("Gateway tracing unavailable: invalid enabled setting")
            return runtime
        enabled = value in ("1", "true", "yes", "on")
    if not isinstance(enabled, bool):
        logger.warning("Gateway tracing unavailable: enabled must be a boolean")
        return runtime
    if not enabled:
        return runtime
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        if isinstance(trace.get_tracer_provider(), TracerProvider):
            logger.info("Gateway tracing uses a dedicated provider; the global provider is unchanged")
        runtime.provider = TracerProvider(
            resource=Resource.create({"service.name": "jiuwenswarm-gateway"}),
            id_generator=create_request_id_generator(),
            sampler=_create_request_sampler(),
            shutdown_on_exit=False,
        )
        runtime.owned = True
        overrides = settings.get("traces")
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, dict):
            raise ValueError("Invalid trace settings")

        def option(key, default=None):
            value = overrides.get(key)
            # Empty trace headers inherit the shared headers in the existing template.
            if _is_empty_setting(value):
                value = settings.get(key)
            elif key == "headers" and value == {}:
                value = settings.get(key)
            return default if _is_empty_setting(value) else value
        exporter_type = option("exporter", "otlp")
        options = _batch_options() if exporter_type != "none" else None
        exporter = None
        if exporter_type == "console":
            exporter = ConsoleSpanExporter()
        elif exporter_type == "otlp":
            protocol = option("protocol")
            if protocol is None:
                protocol = os.environ.get(
                    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
                    os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
                )
            if protocol not in ("http", "http/protobuf", "grpc"):
                raise ValueError("Unsupported OTLP protocol")
            if protocol in ("http", "http/protobuf"):
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            endpoint = option("endpoint")
            if endpoint is not None and not isinstance(endpoint, str):
                raise ValueError("Invalid trace endpoint")
            if protocol in ("http", "http/protobuf") and endpoint:
                parts = urlsplit(endpoint)
                path = parts.path.rstrip("/")
                if not path:
                    path = "/v1/traces"
                endpoint = urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
            headers = option("headers")
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=options["EXPORT_TIMEOUT"] / 1000)
        elif exporter_type != "none":
            raise ValueError("Unsupported trace exporter")
        if exporter is not None:
            diagnostics = _ExportDiagnostics(exporter)
            diagnostics.capacity = options["MAX_QUEUE_SIZE"]
            runtime.diagnostics = diagnostics
            processor = BatchSpanProcessor(
                diagnostics,
                max_queue_size=options["MAX_QUEUE_SIZE"],
                max_export_batch_size=options["MAX_EXPORT_BATCH_SIZE"],
                schedule_delay_millis=options["SCHEDULE_DELAY"],
                export_timeout_millis=options["EXPORT_TIMEOUT"],
            )

            class CountingProcessor(_CountingProcessor, SpanProcessor):
                pass

            runtime.provider.add_span_processor(CountingProcessor(processor, diagnostics))
        runtime.tracer = runtime.provider.get_tracer("jiuwenswarm.gateway")
    except Exception:
        logger.warning("Gateway tracing unavailable during configuration")
        if runtime.owned and runtime.provider:
            try:
                runtime.provider.shutdown()
            except Exception:
                logger.warning("Gateway tracing cleanup failed")
        runtime.tracer = None
        runtime.provider = None
        runtime.owned = False
    return runtime


def start_request_trace(
    request_id: str, method: str, *, channel: str, connection_id: str,
    user_id: str = "", session_id: str = "", trace_context: dict | None = None,
) -> TraceOperation | None:
    """Continue this request's trace or start a root, unless disabled."""
    if not _runtime or not _runtime.tracer:
        return None
    try:
        from opentelemetry import context, trace
        parent_context, root_trace_id, source = _extract_request_context(trace_context)
        attrs = {
            "request_id": request_id,
            "method": method,
            "channel": channel,
            "channel_id": channel,
            "connection_id": connection_id,
        }
        attrs["gateway.trace_source"] = source
        if session_id:
            attrs["session_id"] = session_id
        safe_user_id = ""
        if user_id and _runtime.user_id_formatter:
            try:
                safe_user_id = _runtime.user_id_formatter(user_id) or ""
                if not isinstance(safe_user_id, str) or safe_user_id == user_id:
                    safe_user_id = ""
            except Exception:
                logger.debug("Unable to format gateway user identity")
        if safe_user_id:
            attrs["user_id"] = safe_user_id
        token = _root_trace_id.set(root_trace_id)
        try:
            span = _runtime.tracer.start_span("gw.session.message", context=parent_context, attributes=attrs)
        finally:
            _root_trace_id.reset(token)
        if root_trace_id and span.get_span_context().trace_id != root_trace_id:
            span.end()
            _trace_context_warning("incompatible_id_generator")
            return None
        if not span.get_span_context().is_valid:
            span.end()
            return None
        ctx = trace.set_span_in_context(span, context.Context())
        operation = TraceOperation(
            request_id, method, channel, connection_id, span, ctx,
            time.monotonic(), safe_user_id=safe_user_id,
        )
        operation.event("received")
        return operation
    except Exception:
        logger.debug("Unable to start gateway trace", exc_info=True)
        return None


def current_request_trace() -> TraceOperation | None:
    """Return the operation bound to the current execution context."""
    return _current.get()


@contextmanager
def bind_request_trace_start(start: Callable[[str, str], TraceOperation | None]):
    """Keep the incoming request's starter available until dispatch returns."""
    token = _request_trace_start.set(start)
    try:
        yield
    finally:
        _request_trace_start.reset(token)


def ensure_request_trace(
    request_id: str, channel: str, *, start: Callable[[], TraceOperation | None] | None = None,
) -> TraceOperation | None:
    """Reuse a matching request or start it at an explicit user dispatch."""
    operation = current_request_trace()
    if (operation is not None and not operation.finished
            and (operation.request_id, operation.channel) == (request_id, channel)):
        return operation
    starter = _request_trace_start.get()
    if starter is not None:
        return starter(request_id, channel)
    return start() if start is not None else None


def current_response_trace() -> TraceOperation | None:
    """Return the explicit message owner while producing its client frames."""
    return _response.get()


def start_gateway_trace(
    name: str, *, attributes: dict | None = None, parent: TraceOperation | None = None,
    root: bool = False, links: list[TraceOperation] | None = None,
) -> TraceOperation | None:
    """Start a Gateway operation with explicit parentage and safe scalar facts."""
    if not _runtime or not _runtime.tracer:
        return None
    try:
        from opentelemetry import context, trace

        owner = None if root else parent or current_request_trace()
        attrs = {key: value for key, value in (attributes or {}).items()
                 if isinstance(value, (str, int, float, bool))}
        user_id = attrs.pop("user_id", "")
        safe_user_id = owner.safe_user_id if owner is not None else ""
        if user_id:
            safe_user_id = ""
            if _runtime.user_id_formatter:
                try:
                    formatted = _runtime.user_id_formatter(user_id)
                    if isinstance(formatted, str) and formatted and formatted != user_id:
                        safe_user_id = formatted
                except Exception:
                    logger.debug("Unable to format gateway operation identity")
        if safe_user_id:
            attrs["user_id"] = safe_user_id
        if owner is not None:
            for key, value in (("request_id", owner.request_id), ("channel_id", owner.channel),
                               ("connection_id", owner.connection_id)):
                if value:
                    attrs.setdefault(key, value)
            session_id = (getattr(owner.span, "attributes", None) or {}).get("session_id")
            if session_id:
                attrs.setdefault("session_id", session_id)
        sdk_links = [trace.Link(operation.span.get_span_context()) for operation in (links or [])
                     if operation is not None and operation.span.get_span_context().is_valid]
        span = _runtime.tracer.start_span(
            name, context=owner.context if owner is not None else context.Context(),
            attributes=attrs, links=sdk_links,
        )
        if not span.get_span_context().is_valid:
            span.end()
            return None
        return TraceOperation(
            str(attrs.get("request_id", "")), name, str(attrs.get("channel_id", "")),
            str(attrs.get("connection_id", "")), span,
            trace.set_span_in_context(span, context.Context()), time.monotonic(),
            safe_user_id=safe_user_id, parent_operation=owner, log_summaries=False,
        )
    except Exception:
        logger.debug("Unable to start gateway operation trace", exc_info=True)
        return None


@contextmanager
def trace_operation(
    name: str, *, attributes: dict | None = None, root: bool = False,
    links: list[TraceOperation] | None = None,
):
    """Observe an existing operation without changing its exception policy."""
    operation = start_gateway_trace(name, attributes=attributes, root=root, links=links)
    if operation is None:
        yield None
        return
    with bind_request_trace(operation):
        try:
            yield operation
        except asyncio.CancelledError:
            operation.record_outcome("cancelled", reason="cancelled")
            raise
        except BaseException as exc:
            outcome = (
                "timeout" if isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.lower()
                else "failed"
            )
            operation.record_outcome(outcome, reason=type(exc).__name__)
            raise
        finally:
            operation.finish(operation.outcome, reason=operation.reason)


def current_cleanup_trace() -> TraceOperation | None:
    """Return only the explicitly bound cleanup owner, never a message parent."""
    observation = _cleanup.get()
    if observation is not None and not observation.operation.finished:
        return observation.operation
    return None


def get_cleanup_result(stage: str) -> str | None:
    """Read an observed leaf result even when the SDK span is not sampled."""
    observation = _cleanup.get()
    result = observation.results.get(stage) if observation is not None else None
    return result[0] if result is not None else None


def record_cleanup_result(stage: str, outcome: str, *, reason: str = "") -> None:
    """Report an actual cleanup result without changing any business return."""
    observation = _cleanup.get()
    if observation is None or observation.operation.finished:
        return
    try:
        priorities = {"success": 0, "skipped": 1, "incomplete": 2, "cancelled": 3, "failed": 4, "timeout": 5}
        previous = observation.results.get(stage)
        if previous is not None and priorities.get(previous[0], 2) >= priorities.get(outcome, 2):
            return
        observation.results[stage] = (outcome, reason)
        operation = observation.operation
        operation.set_attribute(f"cleanup.{stage}.outcome", outcome)
        if reason:
            operation.set_attribute(f"cleanup.{stage}.reason", reason)
        if outcome in {"failed", "timeout", "cancelled", "incomplete"}:
            operation.record_outcome(outcome, reason=reason or stage)
        elif outcome == "skipped" and stage != "ws.close":
            operation.record_outcome("incomplete", reason=reason or stage)
    except Exception:
        logger.debug("Unable to record gateway cleanup result", exc_info=True)


@contextmanager
def bind_cleanup_trace(operation: TraceOperation | None):
    """Collect cleanup facts locally, independently of the public return value."""
    previous = _cleanup.get()
    if operation is not None and previous is not None and previous.operation is operation:
        yield operation
        return
    observation = _CleanupObservation(operation) if operation is not None else None
    token = _cleanup.set(observation)
    try:
        yield operation
    except BaseException as exc:
        outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            outcome = "timeout"
        record_cleanup_result("operation", outcome, reason=type(exc).__name__)
        raise
    finally:
        try:
            if observation is not None and not operation.finished:
                results = [value[0] for stage, value in observation.results.items()
                           if stage != "ws.close" or value[0] != "skipped"]
                partial = "success" in results and any(value != "success" for value in results)
                if not results:
                    operation.record_outcome("incomplete", reason="cleanup_unconfirmed")
                result = "partial" if partial else "success" if operation.outcome == "success" else operation.outcome
                if results and all(value == "skipped" for value in results):
                    result = "skipped"
                operation.set_attribute("cleanup.result", result)
        except Exception:
            logger.debug("Unable to summarize gateway cleanup", exc_info=True)
        finally:
            _cleanup.reset(token)


@contextmanager
def trace_stage(name: str):
    """Measure one fixed Gateway stage without changing its failure policy."""
    operation = current_request_trace()
    if operation is None or operation.finished:
        yield
        return
    started = time.monotonic()
    outcome = "success"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        operation.event(name, {"duration_ms": round((time.monotonic() - started) * 1000, 3), "outcome": outcome})


@contextmanager
def bind_request_trace(operation: TraceOperation | None):
    """Bind an operation and restore the previous context afterwards."""
    token = _current.set(operation)
    start_token = _request_trace_start.set(None)
    response_token = _response.set(None)
    cleanup_token = _cleanup.set(None) if operation is None else None
    detach_context = None
    sdk_token = None
    try:
        try:
            from opentelemetry import context as otel_context
            sdk_token = otel_context.attach(
                operation.context if operation else otel_context.Context()
            )
            detach_context = otel_context
        except Exception:
            logger.debug("Unable to bind gateway trace context", exc_info=True)
        yield
    finally:
        if detach_context is not None and sdk_token is not None:
            try:
                detach_context.detach(sdk_token)
            except Exception:
                logger.debug("Unable to restore gateway trace context", exc_info=True)
        _response.reset(response_token)
        if cleanup_token is not None:
            _cleanup.reset(cleanup_token)
        _request_trace_start.reset(start_token)
        _current.reset(token)


@dataclass
class _MessageTraceReference:
    operation: TraceOperation
    enqueued_at: float
    released: bool = False


def get_message_trace(message: object) -> TraceOperation | None:
    """Read a process-local carrier; never extract SDK objects from metadata."""
    operation = getattr(message, "_gateway_trace_operation", None)
    if isinstance(operation, TraceOperation) and not operation.finished:
        return operation
    return None


def attach_message_trace(message: object, operation: TraceOperation | None = None) -> TraceOperation | None:
    """Attach the explicit owner or the matching request's local operation."""
    explicit = operation is not None
    if not explicit and getattr(message, "_gateway_trace_operation", None) is not None:
        operation = get_message_trace(message)
        if operation is None:
            return None
    operation = operation or get_message_trace(message) or current_request_trace()
    if operation is None or operation.finished:
        return None
    channel = str(getattr(message, "channel_id", None) or getattr(message, "channel", "") or "")
    if channel != operation.channel:
        return None
    if not explicit and str(getattr(message, "id", "") or "") != operation.request_id:
        return None
    try:
        setattr(message, "_gateway_trace_operation", operation)
        session_id = getattr(message, "session_id", None)
        if isinstance(session_id, str) and session_id:
            operation.span.set_attribute("session_id", session_id)
        if getattr(message, "type", None) == "req":
            operation.span.set_attribute("is_stream", bool(getattr(message, "is_stream", False)))
    except Exception:
        logger.debug("Unable to attach gateway message trace", exc_info=True)
    return operation


def queue_request_trace(message: object) -> TraceOperation | None:
    """Associate an accepted user request before transferring it to the queue."""
    operation = get_message_trace(message) or attach_message_trace(message)
    if operation is None and getattr(message, "_gateway_trace_operation", None) is None:
        def start():
            if getattr(message, "type", None) != "req":
                return None
            method = getattr(message, "req_method", None)
            method = str(getattr(method, "value", method) or "")
            if not method:
                return None
            metadata = getattr(message, "metadata", None) or {}
            return start_request_trace(
                str(getattr(message, "id", "") or ""), method,
                channel=str(getattr(message, "channel_id", "") or ""),
                connection_id=str(metadata.get("ws_id", "") or ""),
                session_id=str(getattr(message, "session_id", "") or ""),
                user_id=str(getattr(message, "user_id", "") or ""),
                trace_context=metadata.get("trace_context"),
            )

        operation = ensure_request_trace(
            str(getattr(message, "id", "") or ""),
            str(getattr(message, "channel_id", "") or ""), start=start,
        )
        if operation is not None:
            operation = attach_message_trace(message, operation)
    if operation is not None:
        operation.accepted = True
        setattr(message, "_gateway_trace_enqueued_at", time.monotonic())
    return operation


def retain_request_continuation(message: object, operation: TraceOperation | None) -> TraceOperation | None:
    """Keep an explicitly derived queued request alive across producer handoff."""
    continuation = getattr(message, "_gateway_trace_continuation", None)
    if isinstance(continuation, _MessageTraceReference) and not continuation.released:
        return continuation.operation
    operation = attach_message_trace(message, operation) if operation is not None else None
    if operation is not None:
        operation.retain_delivery()
        setattr(message, "_gateway_trace_continuation", _MessageTraceReference(operation, time.monotonic()))
    return operation


def release_request_continuation(message: object, *, reason: str = "") -> None:
    """Release a continuation once, including cancellation before task startup."""
    continuation = getattr(message, "_gateway_trace_continuation", None)
    if not isinstance(continuation, _MessageTraceReference) or continuation.released:
        return
    continuation.released = True
    continuation.operation.release_delivery(reason=reason)


def complete_request_trace(message: object, outcome: str = "success", *, reason: str = "") -> None:
    """Complete the original producer; continuations only report their outcome."""
    operation = get_message_trace(message)
    if operation is None:
        return
    continuation = getattr(message, "_gateway_trace_continuation", None)
    if isinstance(continuation, _MessageTraceReference) and continuation.operation is operation:
        operation.record_outcome(outcome, reason=reason)
    else:
        operation.complete(outcome, reason=reason)


def queue_response_trace(message: object) -> object:
    """Give each queued response its own delivery hold and local carrier."""
    operation = get_message_trace(message) or attach_message_trace(message)
    if operation is None:
        return message
    payload = getattr(message, "payload", None)
    if (
        getattr(message, "ok", True) is False
        or (isinstance(payload, dict) and payload.get("event_type") in {"chat.error", "runtime.error"})
    ):
        operation.record_outcome("failed", reason="response_failed")
    queued = copy(message)
    setattr(queued, "_gateway_trace_operation", operation)
    setattr(queued, "_gateway_trace_delivery", _MessageTraceReference(operation, time.monotonic()))
    operation.retain_delivery()
    operation.received_frames += 1
    return queued


@contextmanager
def bind_message_trace(message: object):
    """Restore a message's scope while a long-lived queue consumer uses it."""
    operation = get_message_trace(message)
    with bind_request_trace(operation):
        response_token = _response.set(operation if getattr(message, "type", None) in {"event", "res"} else None)
        try:
            if operation is not None:
                queued_at = getattr(message, "_gateway_trace_enqueued_at", None)
                if isinstance(queued_at, (int, float)):
                    operation.event("queue.dequeued", {
                        "queue_wait_ms": round((time.monotonic() - queued_at) * 1000, 3),
                    })
                    delattr(message, "_gateway_trace_enqueued_at")
                delivery = getattr(message, "_gateway_trace_delivery", None)
                if isinstance(delivery, _MessageTraceReference):
                    operation.event("response.dequeued", {
                        "queue_wait_ms": round((time.monotonic() - delivery.enqueued_at) * 1000, 3),
                    })
            yield operation
        finally:
            _response.reset(response_token)


def release_response_trace(message: object, *, reason: str = "") -> None:
    """Release a queued response once, after channel dispatch has finished."""
    delivery = getattr(message, "_gateway_trace_delivery", None)
    if not isinstance(delivery, _MessageTraceReference) or delivery.released:
        return
    delivery.released = True
    delivery.operation.release_delivery(reason=reason)


def release_message_trace(message: object, *, requests: bool = False) -> None:
    """Detach observational state while retaining the queued business message."""
    operation = get_message_trace(message)
    if operation is not None:
        operation.record_outcome("incomplete", reason="shutdown")
        if requests:
            complete_request_trace(message, "incomplete", reason="shutdown")
    release_response_trace(message, reason="shutdown")
    release_request_continuation(message, reason="shutdown")
    for attribute in ("_gateway_trace_operation", "_gateway_trace_delivery", "_gateway_trace_enqueued_at",
                      "_gateway_trace_continuation"):
        if hasattr(message, attribute):
            delattr(message, attribute)


def release_queued_message_traces(queue: object, *, requests: bool = False) -> None:
    """Clear local trace references without consuming or reordering a queue."""
    if not isinstance(queue, asyncio.Queue):
        return
    # asyncio.Queue has no public non-consuming iterator. Snapshot through
    # get_nowait and restore immediately on this loop; order is preserved.
    pending: list[object] = []
    while not queue.empty():
        try:
            message = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        queue.task_done()
        pending.append(message)
    for message in pending:
        release_message_trace(message, requests=requests)
        queue.put_nowait(message)


@contextmanager
def bind_agent_request_trace(envelope: object):
    """Bind an explicitly dispatched request, making a real child if derived."""
    current = current_request_trace()
    attached = get_message_trace(envelope)
    request_id = str(getattr(envelope, "request_id", "") or "")
    channel = str(getattr(envelope, "channel", "") or "")
    method = str(getattr(envelope, "method", "") or "")
    owner = attached or current
    if _matches_attached_parent(attached, current, request_id, channel, method):
        owner = current
    child = None
    if owner is not None and (owner.finished or channel != owner.channel):
        owner = None
    if owner is not None and (request_id != owner.request_id or method != owner.method):
        try:
            from opentelemetry import context, trace
            span = _runtime.tracer.start_span("gw.agent.request", context=owner.context, attributes={
                "request_id": request_id, "parent_request_id": owner.request_id,
                "method": method, "channel": channel, "channel_id": channel,
            })
            child = TraceOperation(request_id, method, channel, owner.connection_id, span,
                                   trace.set_span_in_context(span, context.Context()), time.monotonic(),
                                   safe_user_id=owner.safe_user_id, parent_operation=owner)
            owner = child
        except Exception:
            logger.debug("Unable to start derived gateway request trace", exc_info=True)
            owner = None
    if owner is None:
        # Keep unrelated third-party SDK contexts intact, but never inject the
        # previous Gateway operation into an unassociated transport call.
        token = _current.set(None)
        response_token = _response.set(None)
        try:
            yield None
        finally:
            _response.reset(response_token)
            _current.reset(token)
        return
    with bind_request_trace(owner):
        try:
            yield owner
        except asyncio.CancelledError:
            if child is not None:
                child.record_outcome("cancelled", reason="cancelled")
            raise
        except BaseException as exc:
            if child is not None:
                child.record_outcome("timeout" if isinstance(exc, TimeoutError) else "failed", reason="request_failed")
            raise
        finally:
            if child is not None:
                child.finish(child.outcome, reason=child.reason)


def inject_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject only W3C context and the UUID compatibility mirror for F2 HTTP."""
    operation = current_request_trace()
    if operation is None or operation.finished:
        return headers
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        carrier = {
            key: value for key, value in headers.items()
            if key.lower() not in {"traceparent", "tracestate", "x-trace-id"}
        }
        TraceContextTextMapPropagator().inject(carrier, context=operation.context)
        carrier["X-Trace-Id"] = str(UUID(hex=operation.trace_id))
        return carrier
    except Exception:
        logger.debug("Unable to inject gateway HTTP trace", exc_info=True)
        return headers


def inject_request_trace(envelope) -> None:
    """Inject the current W3C traceparent into a matching request envelope."""
    operation = current_request_trace()
    if not _matches_envelope_operation(envelope, operation):
        return
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier, context=operation.context)
        traceparent = carrier.get("traceparent")
        if traceparent:
            envelope.traceparent = traceparent
    except Exception:
        logger.debug("Unable to inject gateway trace", exc_info=True)


async def shutdown_gateway_telemetry() -> None:
    """Close an owned provider with a bounded asynchronous wait."""
    global _runtime
    runtime, _runtime = _runtime, None
    if runtime and runtime.owned and runtime.provider:
        if runtime.diagnostics is not None:
            with runtime.diagnostics.lock:
                runtime.diagnostics.closing = True

        def close():
            try:
                runtime.provider.shutdown()
            except Exception:
                logger.warning("Unable to shut down gateway tracing")
        # SDK shutdown may block in an exporter; a daemon thread bounds the caller.
        done = threading.Event()

        def run_close():
            try:
                close()
            finally:
                done.set()

        def stop_exports():
            if not done.is_set() and runtime.diagnostics is not None:
                try:
                    runtime.diagnostics.shutdown()
                except Exception:
                    logger.warning("Unable to stop Gateway trace exporter")

        timer = threading.Timer(5, stop_exports)
        timer.daemon = True
        timer.start()
        threading.Thread(target=run_close, daemon=True, name="gateway-telemetry-shutdown").start()
        deadline = time.monotonic() + 5
        while not done.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not done.is_set():
            logger.warning("Gateway tracing shutdown budget exceeded; remaining exports are unconfirmed")
        else:
            timer.cancel()
