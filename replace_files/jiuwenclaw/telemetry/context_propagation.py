# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""W3C TraceContext propagation helpers for cross-WebSocket trace context."""

from __future__ import annotations

from typing import Any, Dict, Optional

from opentelemetry import context, trace
from opentelemetry.propagate import inject, extract


def inject_trace_context(carrier: Dict[str, Any]) -> None:
    """Inject current trace context (traceparent/tracestate) into a dict carrier.

    Used on the Gateway side before sending AgentRequest over WebSocket.
    """
    inject(carrier=carrier)


def extract_trace_context(carrier: Optional[Dict[str, Any]] = None) -> context.Context:
    """Extract trace context from a dict carrier.

    Used on the AgentServer side when receiving AgentRequest from WebSocket.
    Returns a Context that can be used as parent for new spans.
    """
    if not carrier:
        return context.get_current()
    return extract(carrier=carrier)
