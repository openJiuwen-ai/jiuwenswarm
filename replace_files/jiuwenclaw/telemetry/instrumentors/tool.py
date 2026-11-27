# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for tool execution — TOOL span.

Wraps _emit_tool_call / _emit_tool_result to create tool spans that track
tool name, arguments, results, duration, and errors.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Tuple

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    GEN_AI_SPAN_TYPE,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenclaw.telemetry.metrics import add_tool_call_count, add_tool_error_count, record_tool_duration

_tracer = trace.get_tracer("jiuwenclaw.tool")

# Active tool spans keyed by tool_call_id -> (span, start_time, channel_id)
_active_tool_spans: Dict[str, Tuple[Any, float, str]] = {}


def instrument_tools() -> None:
    """Monkey-patch JiuClawReActAgent._emit_tool_call and _emit_tool_result."""
    try:
        from jiuwenclaw.agentserver.react_agent import JiuClawReActAgent
    except ImportError:
        logger.debug("[Telemetry] JiuClawReActAgent not available, skipping tool instrumentor")
        return

    _original_emit_tool_call = JiuClawReActAgent._emit_tool_call
    _original_emit_tool_result = JiuClawReActAgent._emit_tool_result

    async def _traced_emit_tool_call(self, session, tool_call):
        tool_name = getattr(tool_call, "name", "") or ""
        tool_call_id = getattr(tool_call, "id", "") or ""
        channel_id = getattr(self, "otel_channel_id", "")
        session_id = getattr(self, "otel_session_id", "")
        request_id = getattr(self, "otel_request_id", "")
        arguments = getattr(tool_call, "arguments", {})

        parent_ctx = getattr(self, "otel_agent_ctx", None)
        span = _tracer.start_span(
            f"gen_ai.tool.execute: {tool_name}",
            context=parent_ctx,
            attributes={
                GEN_AI_TOOL_NAME: tool_name,
                GEN_AI_TOOL_CALL_ID: tool_call_id,
                GEN_AI_SPAN_TYPE: "tool",
                JIUWENCLAW_SESSION_ID: session_id,
                JIUWENCLAW_CHANNEL_ID: channel_id,
                JIUWENCLAW_REQUEST_ID: request_id,
            },
        )
        span.add_event("tool.arguments", {"arguments": str(arguments)[:4096]})
        _active_tool_spans[tool_call_id] = (span, time.monotonic(), channel_id)

        add_tool_call_count(1, {GEN_AI_TOOL_NAME: tool_name, JIUWENCLAW_CHANNEL_ID: channel_id})

        await _original_emit_tool_call(self, session, tool_call)

    async def _traced_emit_tool_result(self, session, tool_call, result):
        tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
        tool_name = getattr(tool_call, "name", "") if tool_call else ""

        entry = _active_tool_spans.pop(tool_call_id, None)
        if entry:
            span, start_time, channel_id = entry
            result_str = str(result)[:4096] if result is not None else ""

            span.add_event("tool.result", {"result": result_str})

            # Simple error detection
            is_error = False
            if result_str:
                lower = result_str.lower()
                is_error = "error" in lower or "exception" in lower or "traceback" in lower

            if is_error:
                span.set_status(StatusCode.ERROR, result_str[:256])
                add_tool_error_count(1, {GEN_AI_TOOL_NAME: tool_name, JIUWENCLAW_CHANNEL_ID: channel_id})
            else:
                span.set_status(StatusCode.OK)

            duration = time.monotonic() - start_time
            record_tool_duration(duration, {GEN_AI_TOOL_NAME: tool_name, JIUWENCLAW_CHANNEL_ID: channel_id})
            span.end()

        await _original_emit_tool_result(self, session, tool_call, result)

    JiuClawReActAgent._emit_tool_call = _traced_emit_tool_call
    JiuClawReActAgent._emit_tool_result = _traced_emit_tool_result
