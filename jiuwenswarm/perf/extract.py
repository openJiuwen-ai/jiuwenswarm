# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_OK_STATUS_VALUES = frozenset({"ok", "success", "succeeded", "completed", "skipped"})
_ERROR_STATUS_VALUES = frozenset({"error", "failed", "failure", "timeout", "cancelled"})


def extract_agent_id(agent: Any, *, deep_agent: Any | None = None) -> str:
    """Extract a stable agent identifier (card.id / subagent_id), not internal instance uuid.

    Prefer the explicit ``deep_agent`` / ``agent`` card (the rail owner). Ambient
    ContextVar subagent ids are last-resort only — after TaskTool returns, leftover
    subagent context must not relabel the parent's tool/LLM events.
    """
    for source in (deep_agent, agent):
        if source is None:
            continue
        card = getattr(source, "card", None)
        if card is not None:
            for attr in ("id", "name"):
                value = str(getattr(card, attr, "") or "").strip()
                if value and not _is_internal_instance_id(value):
                    return value

    if agent is not None:
        raw_id = str(getattr(agent, "id", "") or "").strip()
        if raw_id and not _is_internal_instance_id(raw_id):
            return raw_id

    subagent_id = resolve_subagent_id()
    if subagent_id:
        return subagent_id
    return ""


def _is_internal_instance_id(value: str) -> bool:
    """32-char hex string —openjiuwen runtime instance id, not a logical agent id."""
    text = value.strip().lower()
    return len(text) == 32 and all(ch in "0123456789abcdef" for ch in text)


def extract_model_info(agent: Any) -> tuple[str, str]:
    """Extract (model_name, provider) from an agent instance."""
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


def extract_llm_result(ctx: Any) -> Any:
    inputs = getattr(ctx, "inputs", None)
    if inputs is not None:
        result = getattr(inputs, "response", None)
        if result is None and isinstance(inputs, dict):
            result = inputs.get("response")
        if result is not None:
            return result
    return getattr(ctx, "result", None)


def _extract_reasoning_tokens(usage: Any) -> int:
    """Read provider reasoning/thinking tokens from usage metadata."""
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            nested = int(details.get("reasoning_tokens") or 0)
            if nested:
                return nested
        return int(usage.get("reasoning_tokens") or 0)
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        nested = int(getattr(details, "reasoning_tokens", 0) or 0)
        if nested:
            return nested
    return int(getattr(usage, "reasoning_tokens", 0) or 0)


def extract_usage_tokens(result: Any) -> tuple[int, int, int, int]:
    """Return ``(input, output, cache_read, reasoning)`` token counts."""
    if result is None:
        return 0, 0, 0, 0
    usage = getattr(result, "usage_metadata", None)
    if usage is None and isinstance(result, dict):
        usage = result.get("usage_metadata", result)
    if usage is None:
        return 0, 0, 0, 0
    if isinstance(usage, dict):
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(
            usage.get("cache_read_input_tokens")
            or usage.get("cache_read_tokens")
            or 0
        )
        return input_tokens, output_tokens, cache_read, _extract_reasoning_tokens(usage)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(
        getattr(usage, "cache_read_input_tokens", 0)
        or getattr(usage, "cache_read_tokens", 0)
        or 0
    )
    return input_tokens, output_tokens, cache_read, _extract_reasoning_tokens(usage)


def llm_status_from_ctx(ctx: Any) -> str:
    err = getattr(ctx, "error", None)
    return "error" if err else "ok"


def extract_llm_error(ctx: Any) -> str | None:
    err = getattr(ctx, "error", None)
    if err is None:
        return None
    text = str(err).strip()
    return text[:512] if text else None


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    norm = str(value).strip().lower()
    if norm in _OK_STATUS_VALUES:
        return "ok"
    if norm in _ERROR_STATUS_VALUES:
        return "error"
    return None


def _looks_like_success_text(text: str) -> bool:
    lower = text.strip().lower()
    if not lower:
        return True
    if lower.startswith("successfully"):
        return True
    if lower.startswith("ok"):
        return True
    if "跳过大纲确认" in text or "skipped" in lower:
        return True
    return False


def _extract_explicit_error_field(result: Any) -> str | None:
    """Read only explicit error fields —never treat informational message as failure."""
    if result is None:
        return None
    if isinstance(result, dict):
        err = result.get("error")
        if err:
            text = str(err).strip()
            if text and not _looks_like_success_text(text):
                return text[:512]
        return None
    if hasattr(result, "error"):
        err = getattr(result, "error", None)
        if err:
            text = str(err).strip()
            if text and not _looks_like_success_text(text):
                return text[:512]
    return None


def tool_status_from_result(result: Any) -> str:
    if result is None:
        return "ok"

    if isinstance(result, dict):
        if "success" in result:
            return "ok" if bool(result["success"]) else "error"
        status = _normalize_status(result.get("status"))
        if status is not None:
            return status
        if _extract_explicit_error_field(result):
            return "error"
        return "ok"

    if hasattr(result, "success"):
        return "ok" if bool(getattr(result, "success")) else "error"
    if hasattr(result, "status"):
        status = _normalize_status(getattr(result, "status"))
        if status is not None:
            return status
    if _extract_explicit_error_field(result):
        return "error"

    result_str = str(result)
    if not result_str:
        return "ok"
    if _looks_like_success_text(result_str):
        return "ok"
    lower = result_str.lower()
    if "traceback (most recent call last)" in lower:
        return "error"
    if lower.startswith("error:") or lower.startswith("exception:"):
        return "error"
    if "execution error" in lower or "execution timeout" in lower:
        return "error"
    if "timeout after" in lower:
        return "error"
    return "ok"


def extract_tool_error(result: Any) -> str | None:
    if tool_status_from_result(result) != "error":
        return None
    explicit = _extract_explicit_error_field(result)
    if explicit:
        return explicit
    if isinstance(result, str):
        text = result.strip()
        return text[:512] if text else "unknown error"
    text = str(result).strip()
    return text[:512] if text else "unknown error"


def extract_tool_call_info(ctx: Any) -> tuple[str, str, dict[str, Any]]:
    tool_name = ""
    tool_call_id = ""
    arguments: dict[str, Any] = {}
    inputs = getattr(ctx, "inputs", None)
    if inputs is not None:
        if hasattr(inputs, "tool_call"):
            tc = inputs.tool_call
            tool_name = getattr(tc, "name", "") or ""
            tool_call_id = getattr(tc, "id", "") or ""
            arguments = getattr(tc, "arguments", {}) or {}
        elif hasattr(inputs, "tool_name"):
            tool_name = getattr(inputs, "tool_name", "") or ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return tool_name, tool_call_id, arguments


def extract_tool_result(ctx: Any) -> Any:
    inputs = getattr(ctx, "inputs", None)
    if inputs is not None and hasattr(inputs, "tool_result"):
        return inputs.tool_result
    return None


def resolve_subagent_id() -> str | None:
    """Best-effort subagent id for perf attribution; safe when APIs differ."""
    try:
        from jiuwenswarm.agents.harness.common.rails import subagent_rail as _subagent_rail

        getter = getattr(_subagent_rail, "get_current_subagent_id", None)
        if callable(getter):
            subagent_id = getter()
            if subagent_id:
                return str(subagent_id)
    except Exception as exc:
        logger.debug(
            "[perf] resolve_subagent_id failed: %s",
            exc,
            exc_info=True,
        )
    return None


def extract_stream_source_id(ctx: Any) -> str | None:
    for attr in ("stream_source_id",):
        value = getattr(ctx, attr, None)
        if value:
            return str(value)
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        value = extra.get("stream_source_id")
        if value:
            return str(value)
    return None


def extract_react_iteration(ctx: Any) -> int:
    """Read ReAct loop iteration —perf counter first, then framework inputs."""
    from jiuwenswarm.perf.context import get_react_iteration

    perf_iter = get_react_iteration()
    if perf_iter > 0:
        return perf_iter

    inputs = getattr(ctx, "inputs", None)
    if inputs is None:
        return 0
    for attr in ("iteration", "iter", "step", "round"):
        value = getattr(inputs, attr, None)
        if value is not None:
            try:
                parsed = int(value)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass
    if isinstance(inputs, dict):
        for key in ("iteration", "iter", "step", "round"):
            if key in inputs:
                try:
                    parsed = int(inputs[key])
                    if parsed > 0:
                        return parsed
                except (TypeError, ValueError):
                    pass
    return 0


def current_trace_id_hex() -> str | None:
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        span_context = span.get_span_context()
        if span_context is None or not span_context.trace_id:
            return None
        return format(span_context.trace_id, "032x")
    except Exception:
        return None
