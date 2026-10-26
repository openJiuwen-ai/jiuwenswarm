"""W3C propagation helpers shared by Gateway and AgentServer boundaries."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from opentelemetry import context
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from jiuwenswarm.telemetry.request_context import (
    IncomingRequestBinding,
    bind_incoming_request,
    reset_incoming_request,
)

_W3C_TRACE_CONTEXT = TraceContextTextMapPropagator()


def inject_trace_context(carrier: MutableMapping[str, Any]) -> None:
    """Inject the current W3C trace context into a mutable wire carrier."""
    _W3C_TRACE_CONTEXT.inject(carrier=carrier)


def extract_trace_context(carrier: Mapping[str, Any] | None = None) -> Context:
    """Extract a W3C trace context, preserving the current one when empty."""
    if not carrier:
        return context.get_current()
    return _W3C_TRACE_CONTEXT.extract(carrier=carrier)


__all__ = [
    "IncomingRequestBinding",
    "bind_incoming_request",
    "extract_trace_context",
    "inject_trace_context",
    "reset_incoming_request",
]
