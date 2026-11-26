# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared W3C trace context for Gateway entries and southbound clients.

One rule for every edge: an explicit caller trace wins over the currently
bound Gateway span, and a new id is minted only when neither exists.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from jiuwenswarm.gateway.telemetry import inject_request_headers

TRACE_ID_HEADER = "X-Trace-Id"
TRACE_CONTEXT_HEADERS = frozenset(
    {"traceparent", "tracestate", "x-trace-id", "trace_id"}
)
TRACE_ID_MAX_LEN = 128


def _trace_id_hex(value: str | None) -> str:
    """Return a valid 32-hex W3C trace id, or empty when unusable."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, TypeError, ValueError):
        return ""
    return parsed.hex if parsed.int else ""


def _traceparent_trace_id(value: str | None) -> str:
    parts = str(value or "").strip().split("-")
    if len(parts) < 2:
        return ""
    return _trace_id_hex(parts[1])


def _traceparent_for(trace_id: str) -> str:
    trace_hex = _trace_id_hex(trace_id)
    if not trace_hex:
        return ""
    span_id = uuid.uuid4().hex[:16]
    if span_id == "0" * 16:
        span_id = "0000000000000001"
    # Keep the synthetic parent sampled: ADX otherwise drops the entry span
    # when its provider follows a ParentBased sampler.
    return f"00-{trace_hex}-{span_id}-01"


def trace_context_from_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Collect caller trace headers, lower-cased; empty values are dropped."""
    if headers is None:
        return {}
    try:
        items = list(headers.items())
    except (AttributeError, TypeError):
        return {}
    context: dict[str, str] = {}
    for key, value in items:
        name = str(key).strip().lower()
        text = str(value or "").strip()
        if name in TRACE_CONTEXT_HEADERS and text:
            context[name] = text
    return context


def trace_id_from_headers(headers: Mapping[str, Any] | None) -> str:
    """Read X-Trace-Id first, then a valid W3C traceparent trace id."""
    if headers is None:
        return ""
    traceparent = ""
    try:
        for key, value in headers.items():
            name = str(key).strip().lower()
            text = str(value or "").strip()
            if name == "x-trace-id" and text:
                return text
            if name == "traceparent" and text:
                traceparent = text
    except (AttributeError, TypeError):
        return ""
    parts = traceparent.split("-")
    if len(parts) < 2 or len(parts[1]) != 32:
        return ""
    try:
        parsed = uuid.UUID(hex=parts[1])
    except (TypeError, ValueError):
        return ""
    return str(parsed) if parsed.int else ""


def current_trace_id() -> str:
    """Return the trace id bound to the current Gateway span, if any."""
    return str(inject_request_headers({}).get(TRACE_ID_HEADER) or "").strip()


def resolve_trace_id(trace_id: str | None = None) -> str:
    """Explicit caller trace, then the current span, then a fresh UUID."""
    resolved = str(trace_id or "").strip()
    if not resolved:
        resolved = current_trace_id()
    if not resolved:
        resolved = str(uuid.uuid4())
    return resolved[:TRACE_ID_MAX_LEN]


def outbound_trace_headers(
    headers: Mapping[str, str] | None = None,
    trace_id: str | None = None,
) -> dict[str, str]:
    """Build headers whose traceparent and X-Trace-Id carry the same key.

    ADX gives a valid W3C ``traceparent`` priority over ``X-Trace-Id``. When
    the caller supplies an explicit trace, rebuild ``traceparent`` from that
    value; otherwise ADX would keep tracing the Gateway-minted parent instead
    of the caller's trace.
    """
    carrier = inject_request_headers({
        str(key): str(value) for key, value in (headers or {}).items()
    })
    current_parent = carrier.get("traceparent")
    resolved = str(trace_id or carrier.get(TRACE_ID_HEADER) or "").strip()
    if not resolved:
        resolved = _traceparent_trace_id(current_parent)
    if not resolved:
        resolved = str(uuid.uuid4())
    resolved = resolved[:TRACE_ID_MAX_LEN]

    if current_parent and _traceparent_trace_id(current_parent) == _trace_id_hex(resolved):
        parent = current_parent
    else:
        parent = _traceparent_for(resolved)
        carrier.pop("tracestate", None)
    if parent:
        carrier["traceparent"] = parent
    else:
        carrier.pop("traceparent", None)
        carrier.pop("tracestate", None)
    carrier[TRACE_ID_HEADER] = resolved
    return carrier
