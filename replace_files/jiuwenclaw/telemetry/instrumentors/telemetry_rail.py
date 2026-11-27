# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TelemetryRail — OpenTelemetry instrumentation via DeepAgentRail hooks.

This Rail creates spans for:
- AGENT (jiuwenclaw.agent.invoke) — via before_invoke/after_invoke
- LLM (gen_ai.chat) — via before_model_call/after_model_call
- TOOL (gen_ai.tool.execute) — via before_tool_call/after_tool_call

Follows OpenTelemetry GenAI semantic conventions and integrates with
the existing telemetry module (attributes, metrics, context_propagation).
"""

from __future__ import annotations

import asyncio
import functools
import os
import time
from contextvars import ContextVar, Token
from typing import Any, Optional, Tuple

from opentelemetry import trace, context
from opentelemetry.trace import SpanKind, StatusCode

from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import (
    ERROR_TYPE,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_REQUEST_STREAMING,
    GEN_AI_REQUEST_TEMPERATURE,
    GEN_AI_REQUEST_TOP_P,
    GEN_AI_RESPONSE_FINISH_REASON,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SPAN_TYPE,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_CACHE_READ_TOKENS,
    GEN_AI_USAGE_ESTIMATED,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_TOTAL_TOKENS,
    JIUWENCLAW_AGENT_NAME,
    JIUWENCLAW_CANCELED,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_ITERATION,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenclaw.telemetry.metrics import (
    agent_duration,
    llm_call_count,
    llm_duration,
    token_usage,
    tool_call_count,
    tool_duration,
    tool_error_count,
    _with_resource_labels,
)
from jiuwenclaw.telemetry.context_propagation import extract_trace_context

_tracer = trace.get_tracer("jiuwenclaw.telemetry_rail")

# Module-level flag for message logging
_log_messages: bool = True

# Request-scoped context variables (isolates state across concurrent requests)
# Each request sets these via set_telemetry_context(), and they are automatically
# isolated per asyncio task/context.
_request_context: ContextVar[Optional[dict]] = ContextVar("telemetry_request_context", default=None)
# Stores (span, start_time, context_token) for the active AGENT span
_agent_span_ctx: ContextVar[Optional[Tuple[trace.Span, float, Optional[Token]]]] = ContextVar(
    "telemetry_agent_span", default=None
)


def _extract_model_info(agent: Any) -> tuple[str, str]:
    """Extract (model_name, gen_ai.system) from a BaseAgent/ReActAgent instance.

    ReActAgent stores model_provider/model_client_config/model_name on
    ``agent._config`` (a ReActAgentConfig), not directly on the agent. Read
    direct attributes first (for future compatibility), then fall back to
    ``_config``, then to ``_config.model_client_config.client_provider``.
    """
    if agent is None:
        return "", "unknown"

    config = getattr(agent, "_config", None)

    model_name = getattr(agent, "model_name", "")
    if not model_name and config is not None:
        model_name = getattr(config, "model_name", "")

    provider = getattr(agent, "model_provider", "")
    if not provider and config is not None:
        provider = getattr(config, "model_provider", "")

    if not provider:
        mcc = getattr(agent, "model_client_config", None)
        if mcc is None and config is not None:
            mcc = getattr(config, "model_client_config", None)
        if mcc is not None:
            if isinstance(mcc, dict):
                provider = mcc.get("client_provider", "")
            else:
                provider = getattr(mcc, "client_provider", "")

    system = str(provider).lower() if provider else "unknown"
    return str(model_name or ""), system


def set_log_messages(enabled: bool) -> None:
    """Enable/disable logging full message content in span events."""
    global _log_messages
    _log_messages = enabled


def _hook_safe(method):
    """Decorator: swallow exceptions in TelemetryRail hooks; count toward circuit breaker."""
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        if getattr(self, "_degraded", False):
            return None
        try:
            return await method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._failure_count = getattr(self, "_failure_count", 0) + 1
            logger.warning(
                "[TelemetryRail] hook %s failed (%d/%d): %s",
                method.__name__,
                self._failure_count,
                self._failure_threshold,
                exc,
            )
            if self._failure_count >= self._failure_threshold:
                self._degraded = True
                logger.warning(
                    "[TelemetryRail] circuit breaker tripped — hooks disabled until process restart"
                )
            return None
    return wrapper


class TelemetryRail(DeepAgentRail):
    """Rail that creates OpenTelemetry spans for agent, LLM, and tool calls.

    This Rail hooks into DeepAgent's lifecycle callbacks to create spans
    following OpenTelemetry GenAI semantic conventions.

    Priority should be set low (e.g., 10) so it runs first and captures
    the full execution timeline.

    Request-scoped state (channel_id, session_id, request_id, trace_context,
    agent_span, iteration) is stored in ContextVars to isolate across
    concurrent requests on the same agent instance.
    """

    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self._agent: Optional[Any] = None
        # Active spans keyed by call_id (LLM/tool call IDs are unique per request)
        self._llm_spans: dict[str, tuple[trace.Span, float]] = {}
        self._tool_spans: dict[str, tuple[trace.Span, float, str]] = {}
        # Circuit breaker (instance-level, applies to all hooks)
        self._failure_count: int = 0
        self._degraded: bool = False
        try:
            self._failure_threshold: int = int(os.getenv("OTEL_HOOK_FAILURE_THRESHOLD", "10"))
        except ValueError:
            self._failure_threshold = 10

    def init(self, agent: Any) -> None:
        """Called when Rail is attached to agent."""
        self._agent = agent

    def set_telemetry_context(
        self,
        channel_id: str = "",
        session_id: str = "",
        request_id: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Set telemetry context for the current request.

        Called by JiuWenClawDeepAdapter before invoking the agent.
        Uses ContextVars to isolate state across concurrent requests.

        IMPORTANT: Always clears _trace_context when metadata is None or {},
        preventing inheritance from previous request's traceparent.
        """
        # Extract W3C TraceContext from metadata; explicitly clear if no metadata
        trace_ctx = None
        if metadata:
            trace_ctx = extract_trace_context(metadata)

        # Store all request-scoped state in ContextVar
        _request_context.set({
            "channel_id": channel_id,
            "session_id": session_id,
            "request_id": request_id,
            "trace_context": trace_ctx,
            "iteration": 0,
        })

    def _get_request_context(self) -> dict:
        """Get current request context from ContextVar."""
        ctx = _request_context.get()
        return ctx if ctx is not None else {
            "channel_id": "",
            "session_id": "",
            "request_id": "",
            "trace_context": None,
            "iteration": 0,
        }

    # ------------------------------------------------------------------
    # Invoke hooks — AGENT span
    # ------------------------------------------------------------------

    @_hook_safe
    async def before_invoke(self, ctx: Any) -> None:
        """Create AGENT span when agent starts processing."""
        req_ctx = self._get_request_context()
        # Reset iteration for this request
        req_ctx["iteration"] = 0
        _request_context.set(req_ctx)

        # Get conversation_id from inputs
        conversation_id = ""
        if hasattr(ctx, "inputs"):
            inputs = ctx.inputs
            if hasattr(inputs, "conversation_id"):
                conversation_id = inputs.conversation_id or ""
            # Also extract from inputs dict if available
            if isinstance(inputs, dict):
                conversation_id = inputs.get("conversation_id", "")

        agent_name = ""
        if self._agent and hasattr(self._agent, "card"):
            agent_name = getattr(self._agent.card, "id", "")

        attrs = {
            GEN_AI_AGENT_NAME: agent_name,
            GEN_AI_CONVERSATION_ID: conversation_id,
            GEN_AI_SPAN_TYPE: "agent",
            JIUWENCLAW_AGENT_NAME: agent_name,
            JIUWENCLAW_SESSION_ID: req_ctx["session_id"],
            JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
            JIUWENCLAW_REQUEST_ID: req_ctx["request_id"],
        }

        # Start span with extracted parent context (for cross-WebSocket propagation)
        agent_span = _tracer.start_span(
            "jiuwenclaw.agent.invoke",
            context=req_ctx["trace_context"],
            kind=SpanKind.SERVER,
            attributes=attrs,
        )
        agent_start_time = time.monotonic()

        # Activate span context for child spans (LLM/TOOL)
        ctx_token: Optional[Token] = None
        if agent_span:
            ctx_token = context.attach(trace.set_span_in_context(agent_span))

        # Store span state in ContextVar for this request
        _agent_span_ctx.set((agent_span, agent_start_time, ctx_token))

    @_hook_safe
    async def after_invoke(self, ctx: Any) -> None:
        """End AGENT span after agent finishes."""
        entry = _agent_span_ctx.get()
        if not entry:
            return

        agent_span, agent_start_time, ctx_token = entry
        req_ctx = self._get_request_context()

        if agent_span:
            # Check for errors
            err = getattr(ctx, "error", None)
            if err:
                agent_span.set_status(StatusCode.ERROR, str(err)[:256])
                agent_span.record_exception(err)
                if isinstance(err, asyncio.CancelledError):
                    agent_span.set_attribute(JIUWENCLAW_CANCELED, True)
                    agent_span.set_attribute(ERROR_TYPE, "CancelledError")
                elif isinstance(err, asyncio.TimeoutError):
                    agent_span.set_attribute(ERROR_TYPE, "TimeoutError")
                else:
                    agent_span.set_attribute(ERROR_TYPE, type(err).__name__)
            else:
                agent_span.set_status(StatusCode.OK)

            # Record duration metric
            duration = time.monotonic() - agent_start_time
            agent_duration.record(duration, _with_resource_labels({
                JIUWENCLAW_AGENT_NAME: getattr(self._agent, "card", None) and getattr(self._agent.card, "id", ""),
                JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
            }))

            agent_span.end()

        # Detach context token
        if ctx_token is not None:
            context.detach(ctx_token)

        # Clear the agent span context
        _agent_span_ctx.set(None)

    # ------------------------------------------------------------------
    # Model call hooks — LLM span
    # ------------------------------------------------------------------

    @_hook_safe
    async def before_model_call(self, ctx: Any) -> None:
        """Create LLM span before model call."""
        req_ctx = self._get_request_context()

        temperature = None
        top_p = None

        agent = getattr(ctx, "agent", None)
        model_name, system = _extract_model_info(agent)

        # model_config_obj may live on agent or agent._config
        model_cfg = getattr(agent, "model_config_obj", None)
        if model_cfg is None:
            config = getattr(agent, "_config", None)
            if config is not None:
                model_cfg = getattr(config, "model_config_obj", None)
        if model_cfg is not None:
            temperature = getattr(model_cfg, "temperature", None)
            top_p = getattr(model_cfg, "top_p", None)

        # Generate unique span key
        call_id = str(time.monotonic_ns())

        attrs = {
            GEN_AI_SYSTEM: system,
            GEN_AI_REQUEST_MODEL: model_name,
            GEN_AI_RESPONSE_MODEL: model_name,
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_SPAN_TYPE: "model",
            JIUWENCLAW_SESSION_ID: req_ctx["session_id"],
            JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
            JIUWENCLAW_REQUEST_ID: req_ctx["request_id"],
        }

        if temperature is not None:
            attrs[GEN_AI_REQUEST_TEMPERATURE] = float(temperature)
        if top_p is not None:
            attrs[GEN_AI_REQUEST_TOP_P] = float(top_p)

        # Increment iteration counter (request-scoped)
        req_ctx["iteration"] += 1
        _request_context.set(req_ctx)
        attrs[JIUWENCLAW_ITERATION] = req_ctx["iteration"]

        streaming = False
        if hasattr(ctx, "model"):
            streaming = bool(getattr(ctx.model, "streaming", False))
        attrs[GEN_AI_REQUEST_STREAMING] = streaming

        # Use the current OTel context — supports sub-agent / handoff nesting automatically.
        span = _tracer.start_span(
            "gen_ai.chat",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        )

        # Record input messages as span events
        if _log_messages and hasattr(ctx, "messages"):
            self._record_input_messages(span, ctx.messages)

        self._llm_spans[call_id] = (span, time.monotonic())
        ctx._otel_llm_call_id = call_id

    def record_first_token(self, ctx: Any) -> None:
        """Emit a `gen_ai.first_token` event on the active LLM span.

        Call sites are responsible for invoking this at the first streaming chunk.
        No-op if no matching span is found.
        """
        call_id = getattr(ctx, "_otel_llm_call_id", None)
        if not call_id:
            return
        entry = self._llm_spans.get(call_id)
        if not entry:
            return
        span, _ = entry
        span.add_event("gen_ai.first_token")

    @_hook_safe
    async def after_model_call(self, ctx: Any) -> None:
        """End LLM span after model call completes."""
        call_id = getattr(ctx, "_otel_llm_call_id", None)
        if not call_id:
            return

        entry = self._llm_spans.pop(call_id, None)
        if not entry:
            return

        span, start_time = entry
        req_ctx = self._get_request_context()
        channel_id = req_ctx["channel_id"]

        agent = getattr(ctx, "agent", None)
        model_name, system = _extract_model_info(agent)

        # Upstream ReActAgent sets the assistant message on ``ctx.inputs.response``
        # (see openjiuwen/core/single_agent/agents/react_agent.py L778). Older
        # plans referenced ``ctx.result`` which does not exist on
        # AgentCallbackContext; falling back keeps both shapes working.
        result = None
        inputs = getattr(ctx, "inputs", None)
        if inputs is not None:
            result = getattr(inputs, "response", None)
            if result is None and isinstance(inputs, dict):
                result = inputs.get("response")
        if result is None:
            result = getattr(ctx, "result", None)

        if result is not None:
            self._record_token_usage(span, result, model_name, system, channel_id)

            # Finish reason
            finish_reason = getattr(result, "finish_reason", None)
            if finish_reason and str(finish_reason) != "null":
                span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [str(finish_reason)])
                span.set_attribute(GEN_AI_RESPONSE_FINISH_REASON, str(finish_reason))

            # Record output message
            if _log_messages:
                self._record_output_message(span, result)

        # Check for errors
        if hasattr(ctx, "error") and ctx.error:
            span.set_status(StatusCode.ERROR, str(ctx.error)[:256])
            span.record_exception(ctx.error)
            llm_call_count.add(1, _with_resource_labels({
                GEN_AI_REQUEST_MODEL: model_name,
                "status": "error",
                JIUWENCLAW_CHANNEL_ID: channel_id,
            }))
        else:
            span.set_status(StatusCode.OK)
            llm_call_count.add(1, _with_resource_labels({
                GEN_AI_REQUEST_MODEL: model_name,
                "status": "success",
                JIUWENCLAW_CHANNEL_ID: channel_id,
            }))

        # Record duration metric
        duration = time.monotonic() - start_time
        llm_duration.record(duration, _with_resource_labels({
            GEN_AI_REQUEST_MODEL: model_name,
            GEN_AI_SYSTEM: system,
            JIUWENCLAW_CHANNEL_ID: channel_id,
        }))

        span.end()

    @_hook_safe
    async def on_model_exception(self, ctx: Any) -> None:
        """Handle model call exception."""
        call_id = getattr(ctx, "_otel_llm_call_id", None)
        if not call_id:
            return

        entry = self._llm_spans.pop(call_id, None)
        if not entry:
            return

        span, start_time = entry

        if hasattr(ctx, "exception"):
            exc = ctx.exception
            span.set_status(StatusCode.ERROR, str(exc)[:256])
            span.record_exception(exc)

        span.end()

    # ------------------------------------------------------------------
    # Tool call hooks — TOOL span
    # ------------------------------------------------------------------

    @_hook_safe
    async def before_tool_call(self, ctx: Any) -> None:
        """Create TOOL span before tool execution."""
        req_ctx = self._get_request_context()

        # Get tool info from inputs
        tool_name = ""
        tool_call_id = ""
        arguments = {}

        if hasattr(ctx, "inputs"):
            inputs = ctx.inputs
            if hasattr(inputs, "tool_call"):
                tc = inputs.tool_call
                tool_name = getattr(tc, "name", "") or ""
                tool_call_id = getattr(tc, "id", "") or ""
                arguments = getattr(tc, "arguments", {}) or {}
            elif hasattr(inputs, "tool_name"):
                tool_name = inputs.tool_name

        # Fix: Generate unique key when tool_call_id is empty to prevent span overwrite
        span_key = tool_call_id if tool_call_id else f"__no_id__{tool_name}_{time.monotonic_ns()}"

        attrs = {
            GEN_AI_TOOL_NAME: tool_name,
            GEN_AI_TOOL_CALL_ID: tool_call_id,  # Keep original (may be empty)
            GEN_AI_SPAN_TYPE: "tool",
            JIUWENCLAW_SESSION_ID: req_ctx["session_id"],
            JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
            JIUWENCLAW_REQUEST_ID: req_ctx["request_id"],
        }

        # Use the current OTel context — supports sub-agent / handoff nesting automatically.
        span = _tracer.start_span(
            f"gen_ai.tool.execute: {tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
        )

        # Record arguments as span event
        span.add_event("tool.arguments", {"arguments": str(arguments)[:4096]})

        self._tool_spans[span_key] = (span, time.monotonic(), tool_name)

        tool_call_count.add(1, _with_resource_labels({
            GEN_AI_TOOL_NAME: tool_name,
            JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
        }))

        # Store span_key in ctx for retrieval in after_tool_call
        ctx._otel_tool_span_key = span_key

    @_hook_safe
    async def after_tool_call(self, ctx: Any) -> None:
        """End TOOL span after tool execution."""
        req_ctx = self._get_request_context()

        # Retrieve span_key from ctx (set in before_tool_call)
        span_key = getattr(ctx, "_otel_tool_span_key", None)

        # Fallback: reconstruct span_key using same logic as before_tool_call
        if not span_key:
            tool_call_id = ""
            tool_name = ""
            if hasattr(ctx, "inputs"):
                inputs = ctx.inputs
                if hasattr(inputs, "tool_call"):
                    tc = inputs.tool_call
                    tool_call_id = getattr(tc, "id", "") if tc else ""
                    tool_name = getattr(tc, "name", "") if tc else ""
                elif hasattr(inputs, "tool_name"):
                    tool_name = inputs.tool_name
            span_key = tool_call_id if tool_call_id else f"__no_id__{tool_name}_{time.monotonic_ns()}"

        entry = self._tool_spans.pop(span_key, None)
        if not entry:
            return

        span, start_time, span_tool_name = entry

        # Get result
        result = None
        if hasattr(ctx, "inputs"):
            inputs = ctx.inputs
            if hasattr(inputs, "tool_result"):
                result = inputs.tool_result

        # Record result
        result_str = str(result)[:4096] if result is not None else ""
        span.add_event("tool.result", {"result": result_str})

        # Error detection
        is_error = False
        if result_str:
            lower = result_str.lower()
            is_error = "error" in lower or "exception" in lower or "traceback" in lower

        if is_error:
            span.set_status(StatusCode.ERROR, result_str[:256])
            tool_error_count.add(1, _with_resource_labels({
                GEN_AI_TOOL_NAME: span_tool_name,
                JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
            }))
        else:
            span.set_status(StatusCode.OK)

        # Record duration metric
        duration = time.monotonic() - start_time
        tool_duration.record(duration, _with_resource_labels({
            GEN_AI_TOOL_NAME: span_tool_name,
            JIUWENCLAW_CHANNEL_ID: req_ctx["channel_id"],
        }))

        span.end()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_input_messages(self, span: trace.Span, messages: Any) -> None:
        """Record input messages as span events."""
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

    def _record_output_message(self, span: trace.Span, result: Any) -> None:
        """Record the assistant output message as a span event."""
        content = getattr(result, "content", "") or ""
        tool_calls = getattr(result, "tool_calls", None)
        attrs = {"content": str(content)}
        if tool_calls:
            attrs["tool_calls"] = str(tool_calls)[:4096]
        span.add_event("gen_ai.assistant.message", attrs)

    def _record_token_usage(
        self,
        span: trace.Span,
        result: Any,
        model_name: str,
        system: str,
        channel_id: str = "",
    ) -> None:
        """Extract token usage from result and record."""
        usage = getattr(result, "usage_metadata", None)
        # TODO(§11.5): when an estimator provides a fallback count because the
        # adapter did not return usage, set GEN_AI_USAGE_ESTIMATED=True on the
        # span so downstream consumers can distinguish reported vs estimated.
        if not usage:
            return

        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_tokens", 0) or 0

        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
        span.set_attribute(GEN_AI_USAGE_TOTAL_TOKENS, input_tokens + output_tokens)
        if cache_read:
            span.set_attribute(GEN_AI_USAGE_CACHE_READ_TOKENS, cache_read)

        # Metric counters
        base_attrs = _with_resource_labels({
            GEN_AI_REQUEST_MODEL: model_name,
            GEN_AI_SYSTEM: system,
            JIUWENCLAW_CHANNEL_ID: channel_id,
        })
        if input_tokens:
            token_usage.add(input_tokens, {**base_attrs, "gen_ai.token.type": "input"})
        if output_tokens:
            token_usage.add(output_tokens, {**base_attrs, "gen_ai.token.type": "output"})
        if cache_read:
            token_usage.add(cache_read, {**base_attrs, "gen_ai.token.type": "cache_read"})