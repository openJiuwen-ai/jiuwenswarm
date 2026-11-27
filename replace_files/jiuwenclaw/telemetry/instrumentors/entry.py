# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for MessageHandler.process_stream — ENTRY span."""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    GEN_AI_SPAN_TYPE,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenclaw.telemetry.context_propagation import inject_trace_context
from jiuwenclaw.telemetry.metrics import add_request_count, add_request_error_count, record_request_duration

_tracer = trace.get_tracer("jiuwenclaw.entry")


def instrument_entry() -> None:
    """Monkey-patch MessageHandler to create entry spans and propagate trace context."""
    try:
        from jiuwenclaw.gateway.message_handler import MessageHandler
    except ImportError:
        logger.debug("[Telemetry] MessageHandler not available, skipping entry instrumentor")
        return

    _original_process_stream = MessageHandler.process_stream
    _original_message_to_e2a = MessageHandler.message_to_e2a

    @staticmethod
    def _traced_message_to_e2a(msg):
        envelope = _original_message_to_e2a(msg)
        # Inject W3C TraceContext into E2A channel_context for cross-WebSocket propagation
        inject_trace_context(envelope.channel_context)

        return envelope

    async def _traced_process_stream(
        self,
        env,
        session_id,
        request_metadata: dict[str, Any] | None = None,
    ):
        with _tracer.start_as_current_span(
            "channel.request",
            attributes={
                JIUWENCLAW_CHANNEL_ID: env.channel or "",
                JIUWENCLAW_SESSION_ID: session_id or "",
                JIUWENCLAW_REQUEST_ID: env.request_id or "",
                GEN_AI_SPAN_TYPE: "workflow",
            },
        ) as span:
            # Re-inject after span is created so the correct trace_id is propagated
            inject_trace_context(env.channel_context)

            add_request_count(1, {JIUWENCLAW_CHANNEL_ID: env.channel or ""})
            start = time.monotonic()
            try:
                await _original_process_stream(self, env, session_id, request_metadata)
                span.set_status(StatusCode.OK)
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc)[:256])
                span.record_exception(exc)
                add_request_error_count(1, {JIUWENCLAW_CHANNEL_ID: env.channel or ""})
                raise
            finally:
                duration = time.monotonic() - start
                record_request_duration(duration, {JIUWENCLAW_CHANNEL_ID: env.channel or ""})

    MessageHandler.message_to_e2a = _traced_message_to_e2a
    MessageHandler.process_stream = _traced_process_stream
