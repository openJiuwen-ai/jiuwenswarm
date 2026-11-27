# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for JiuClawReActAgent._call_llm — LLM span (core).

Records GenAI semantic convention attributes, token usage, and full message
content as span events.
"""

from __future__ import annotations

import time
from typing import List, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_REQUEST_TEMPERATURE,
    GEN_AI_REQUEST_TOP_P,
    GEN_AI_RESPONSE_FINISH_REASON,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SPAN_TYPE,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_CACHE_READ_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_TOTAL_TOKENS,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenclaw.telemetry.metrics import add_llm_call_count, add_token_usage, record_llm_duration

_tracer = trace.get_tracer("jiuwenclaw.llm")

# Module-level flag — set by init_telemetry via instrumentors/__init__.py
_log_messages: bool = True


def set_log_messages(enabled: bool) -> None:
    global _log_messages
    _log_messages = enabled


def instrument_llm() -> None:
    """Monkey-patch JiuClawReActAgent._call_llm to create GenAI LLM spans."""
    try:
        from jiuwenclaw.agentserver.react_agent import JiuClawReActAgent
    except ImportError:
        logger.debug("[Telemetry] JiuClawReActAgent not available, skipping llm instrumentor")
        return

    _original_call_llm = JiuClawReActAgent._call_llm

    async def _traced_call_llm(self, messages, tools=None, session=None, chunk_threshold=10):
        model_name = getattr(self._config, "model_name", "unknown")
        system = _infer_gen_ai_system(self)
        channel_id = getattr(self, "otel_channel_id", "")
        session_id = getattr(self, "otel_session_id", "")
        request_id = getattr(self, "otel_request_id", "")
        model_cfg = getattr(self._config, "model_config_obj", None)
        temperature = getattr(model_cfg, "temperature", None)
        top_p = getattr(model_cfg, "top_p", None)

        span_attrs = {
            GEN_AI_SYSTEM: system,
            GEN_AI_REQUEST_MODEL: model_name,
            GEN_AI_RESPONSE_MODEL: model_name,
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_SPAN_TYPE: "model",
            JIUWENCLAW_SESSION_ID: session_id,
            JIUWENCLAW_CHANNEL_ID: channel_id,
            JIUWENCLAW_REQUEST_ID: request_id,
        }
        if temperature is not None:
            span_attrs[GEN_AI_REQUEST_TEMPERATURE] = float(temperature)
        if top_p is not None:
            span_attrs[GEN_AI_REQUEST_TOP_P] = float(top_p)

        parent_ctx = getattr(self, "otel_agent_ctx", None)
        with _tracer.start_as_current_span(
            "gen_ai.chat",
            context=parent_ctx,
            attributes=span_attrs,
        ) as span:
            # Record input messages as span events
            if _log_messages:
                _record_input_messages(span, messages)

            start = time.monotonic()
            try:
                result = await _original_call_llm(
                    self, messages, tools, session, chunk_threshold
                )

                # Token usage from AssistantMessage.usage_metadata
                _record_token_usage(span, result, model_name, system, channel_id)

                # Finish reason
                finish_reason = getattr(result, "finish_reason", None)
                if finish_reason and str(finish_reason) != "null":
                    span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [str(finish_reason)])
                    span.set_attribute(GEN_AI_RESPONSE_FINISH_REASON, str(finish_reason))

                # Record output message
                if _log_messages:
                    _record_output_message(span, result)

                span.set_status(StatusCode.OK)
                add_llm_call_count(
                    1,
                    {
                        GEN_AI_REQUEST_MODEL: model_name,
                        "status": "success",
                        JIUWENCLAW_CHANNEL_ID: channel_id,
                    },
                )
                return result

            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc)[:256])
                span.record_exception(exc)
                add_llm_call_count(
                    1,
                    {
                        GEN_AI_REQUEST_MODEL: model_name,
                        "status": "error",
                        JIUWENCLAW_CHANNEL_ID: channel_id,
                    },
                )
                raise
            finally:
                duration = time.monotonic() - start
                record_llm_duration(duration, {
                    GEN_AI_REQUEST_MODEL: model_name,
                    GEN_AI_SYSTEM: system,
                    JIUWENCLAW_CHANNEL_ID: channel_id,
                })

    JiuClawReActAgent._call_llm = _traced_call_llm


def _infer_gen_ai_system(agent) -> str:
    """Infer gen_ai.system from model_client_config.client_provider."""
    try:
        config = agent._config
        mcc = getattr(config, "model_client_config", None)
        if isinstance(mcc, dict):
            provider = mcc.get("client_provider", "")
        else:
            provider = getattr(mcc, "client_provider", "")
        if provider:
            return str(provider).lower()
    except Exception as exc:
        logger.debug("[Telemetry] Failed to infer gen_ai.system: %s", exc, exc_info=True)
    return "unknown"


def _record_input_messages(span, messages) -> None:
    """Record input messages (system/user/assistant/tool) as span events."""
    for msg in messages:
        role = getattr(msg, "role", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role", "unknown")
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content", "")

        content_str = str(content) if content else ""

        if role == "system":
            span.add_event("gen_ai.system.message", {"content": content_str})
        elif role == "user":
            span.add_event("gen_ai.user.message", {"content": content_str})
        elif role == "assistant":
            span.add_event("gen_ai.assistant.message", {"content": content_str})
        elif role == "tool":
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            span.add_event("gen_ai.tool.message", {
                "content": content_str[:4096],
                "tool_call_id": str(tool_call_id),
            })


def _record_output_message(span, result) -> None:
    """Record the assistant output message as a span event."""
    content = getattr(result, "content", "") or ""
    tool_calls = getattr(result, "tool_calls", None)
    attrs = {"content": str(content)}
    if tool_calls:
        attrs["tool_calls"] = str(tool_calls)[:4096]
    span.add_event("gen_ai.assistant.message", attrs)


def _record_token_usage(span, result, model_name: str, system: str, channel_id: str = "") -> None:
    """Extract token usage from AssistantMessage.usage_metadata and record."""
    usage = getattr(result, "usage_metadata", None)
    if not usage:
        return

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    # UsageMetadata uses cache_tokens (not cache_read_input_tokens)
    cache_read = getattr(usage, "cache_tokens", 0) or 0

    span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
    span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
    span.set_attribute(GEN_AI_USAGE_TOTAL_TOKENS, input_tokens + output_tokens)
    if cache_read:
        span.set_attribute(GEN_AI_USAGE_CACHE_READ_TOKENS, cache_read)

    # Metric counters
    base_attrs = {GEN_AI_REQUEST_MODEL: model_name, GEN_AI_SYSTEM: system, JIUWENCLAW_CHANNEL_ID: channel_id}
    if input_tokens:
        add_token_usage(input_tokens, {**base_attrs, "gen_ai.token.type": "input"})
    if output_tokens:
        add_token_usage(output_tokens, {**base_attrs, "gen_ai.token.type": "output"})
    if cache_read:
        add_token_usage(cache_read, {**base_attrs, "gen_ai.token.type": "cache_read"})
