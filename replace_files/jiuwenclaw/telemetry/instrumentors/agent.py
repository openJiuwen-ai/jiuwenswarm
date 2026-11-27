# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for JiuWenClaw.process_message / process_message_stream — AGENT span."""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import context, trace
from opentelemetry.trace import SpanKind, StatusCode

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_SPAN_TYPE,
    JIUWENCLAW_AGENT_NAME,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenclaw.telemetry.context_propagation import extract_trace_context
from jiuwenclaw.telemetry.metrics import record_agent_duration

_tracer = trace.get_tracer("jiuwenclaw.agent")


def instrument_agent() -> None:
    """Monkey-patch JiuWenClaw to create agent spans with cross-WebSocket context propagation."""
    try:
        from jiuwenclaw.agentserver.interface import JiuWenClaw
    except ImportError:
        logger.debug("[Telemetry] JiuWenClaw not available, skipping agent instrumentor")
        return

    _original_process_message = JiuWenClaw.process_message
    _original_process_message_stream = JiuWenClaw.process_message_stream

    async def _traced_process_message(self, request):
        parent_ctx = extract_trace_context(request.metadata)
        with _tracer.start_as_current_span(
            "jiuwenclaw.agent.invoke",
            context=parent_ctx,
            kind=SpanKind.SERVER,
            attributes=_build_attrs(self, request),
        ) as span:
            _store_agent_ctx(
                self, trace.set_span_in_context(span),
                request.channel_id or "", request.session_id or "", request.request_id or "",
            )
            start = time.monotonic()
            try:
                result = await _original_process_message(self, request)
                span.set_status(StatusCode.OK)
                return result
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc)[:256])
                span.record_exception(exc)
                raise
            finally:
                duration = time.monotonic() - start
                record_agent_duration(duration, {
                    JIUWENCLAW_AGENT_NAME: getattr(self, "_agent_name", ""),
                    JIUWENCLAW_CHANNEL_ID: request.channel_id or "",
                })

    async def _traced_process_message_stream(self, request):
        parent_ctx = extract_trace_context(request.metadata)
        span = _tracer.start_span(
            "jiuwenclaw.agent.invoke.stream",
            context=parent_ctx,
            kind=SpanKind.SERVER,
            attributes=_build_attrs(self, request),
        )
        ctx = trace.set_span_in_context(span)
        _store_agent_ctx(self, ctx, request.channel_id or "", request.session_id or "", request.request_id or "")
        token = context.attach(ctx)
        start = time.monotonic()
        try:
            async for chunk in _original_process_message_stream(self, request):
                yield chunk
            span.set_status(StatusCode.OK)
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc)[:256])
            span.record_exception(exc)
            raise
        finally:
            duration = time.monotonic() - start
            record_agent_duration(duration, {
                JIUWENCLAW_AGENT_NAME: getattr(self, "_agent_name", ""),
                JIUWENCLAW_CHANNEL_ID: request.channel_id or "",
            })
            span.end()
            context.detach(token)

    JiuWenClaw.process_message = _traced_process_message
    JiuWenClaw.process_message_stream = _traced_process_message_stream


def _store_agent_ctx(
    jiuwenclaw_server, ctx, channel_id: str = "", session_id: str = "", request_id: str = "",
) -> None:
    """Store agent span context, channel_id, session_id and request_id on the JiuClawReActAgent instance.

    JiuWenClaw._instance is JiuClawReActAgent — LLM/tool instrumentors
    read self.otel_agent_ctx, self.otel_channel_id, self.otel_session_id
    and self.otel_request_id from that same instance.
    """
    instance = getattr(jiuwenclaw_server, "_instance", None)
    if instance is not None:
        instance.otel_agent_ctx = ctx
        instance.otel_channel_id = channel_id
        instance.otel_session_id = session_id
        instance.otel_request_id = request_id


def _build_attrs(agent_server, request) -> dict[str, Any]:
    return {
        JIUWENCLAW_AGENT_NAME: getattr(agent_server, "_agent_name", ""),
        JIUWENCLAW_SESSION_ID: request.session_id or "",
        JIUWENCLAW_CHANNEL_ID: request.channel_id or "",
        JIUWENCLAW_REQUEST_ID: request.request_id or "",
        GEN_AI_AGENT_NAME: getattr(agent_server, "_agent_name", ""),
        GEN_AI_CONVERSATION_ID: request.session_id or "",
        GEN_AI_SPAN_TYPE: "agent",
    }
