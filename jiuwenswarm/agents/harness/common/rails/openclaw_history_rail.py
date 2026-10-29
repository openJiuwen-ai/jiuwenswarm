"""Read-only runtime observer that writes an isolated OpenClaw transcript."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
    UserMessageInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.invocation_context.runtime import (
    get_current_invocation_context,
)
from jiuwenswarm.server.runtime.session.openclaw_runtime_history import (
    OPENCLAW_HISTORY_ENABLED,
    append_openclaw_message,
    mark_openclaw_session_active,
    release_openclaw_session,
)

logger = logging.getLogger(__name__)

_SID_KEY = "__openclaw_history_session_id"
_CHANNEL_KEY = "__openclaw_history_channel_id"
_SUPPORTED_CHANNELS = frozenset({"desktop", "xiaoyi"})


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
            if isinstance(result, dict):
                return result
        except Exception:
            return {}
    return {}


def _content_blocks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if isinstance(value, (int, float, bool)):
        return [{"type": "text", "text": str(value)}]
    if not isinstance(value, list):
        text = _text(value)
        return [{"type": "text", "text": text}] if text else []

    blocks: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        if isinstance(item, Mapping):
            block = dict(item)
            blocks.append(block)
            continue
        text = _text(item)
        if text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _user_content_blocks(parts: Any) -> list[dict[str, Any]]:
    """Normalize user parts without double-encoding structured content."""
    content: list[dict[str, Any]] = []
    for part in parts or []:
        if isinstance(part, str):
            if part:
                content.append({"type": "text", "text": part})
            continue
        part_dict = _as_dict(part)
        text = part_dict.get("text")
        if isinstance(text, str):
            content.append({"type": "text", "text": text})
            continue
        object_text = getattr(part, "text", None)
        if isinstance(object_text, str):
            content.append({"type": "text", "text": object_text})
            continue
        content.extend(_content_blocks(part_dict or part))
    return content


def _tool_arguments(value: Any) -> Any:
    if isinstance(value, Mapping) or isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except (TypeError, ValueError):
                return value
    return value if value is not None else {}


def _openclaw_usage(value: Any) -> dict[str, Any] | None:
    usage = _as_dict(value)
    if not usage or not any(
        key in usage
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input",
            "output",
            "totalTokens",
        )
    ):
        return None

    def _int(*names: str, default: int = 0) -> int:
        for name in names:
            value = usage.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return default

    def _float(*names: str, default: float = 0.0) -> float:
        for name in names:
            value = usage.get(name)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
        return default

    input_tokens = _int("input_tokens", "input")
    output_tokens = _int("output_tokens", "output")
    cache_read = _int("cache_tokens", "cacheRead", "cache_read")
    cache_write = _int("cache_write_tokens", "cacheWrite", "cache_write")
    input_cost = _float("input_cost")
    output_cost = _float("output_cost")
    cache_read_cost = _float("cache_read_cost")
    cache_write_cost = _float("cache_write_cost")
    total_cost = _float(
        "total_cost",
        default=input_cost + output_cost + cache_read_cost + cache_write_cost,
    )
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": _int(
            "total_tokens",
            "totalTokens",
            default=input_tokens + output_tokens,
        ),
        "cost": {
            "input": input_cost,
            "output": output_cost,
            "cacheRead": cache_read_cost,
            "cacheWrite": cache_write_cost,
            "total": total_cost,
        },
    }


def _stop_reason(value: Any) -> str:
    normalized = str(value or "").strip()
    aliases = {
        "tool_calls": "toolUse",
        "tool_use": "toolUse",
        "toolUse": "toolUse",
        "end_turn": "stop",
        "stop": "stop",
        "length": "length",
        "error": "error",
        "aborted": "aborted",
        "cancelled": "aborted",
        "canceled": "aborted",
    }
    return aliases.get(normalized, normalized or "stop")


def _assistant_message(response: Any) -> dict[str, Any]:
    content = _content_blocks(getattr(response, "content", ""))
    reasoning = str(getattr(response, "reasoning_content", "") or "")
    if reasoning:
        content.insert(0, {"type": "thinking", "thinking": reasoning})

    for raw_call in getattr(response, "tool_calls", None) or []:
        call = _as_dict(raw_call)
        if not call and not isinstance(raw_call, Mapping):
            call = {
                "id": getattr(raw_call, "id", ""),
                "name": getattr(raw_call, "name", ""),
                "arguments": getattr(raw_call, "arguments", {}),
            }
        content.append(
            {
                "type": "toolCall",
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": _tool_arguments(call.get("arguments")),
            }
        )

    message: dict[str, Any] = {"role": "assistant", "content": content}
    usage = _openclaw_usage(getattr(response, "usage_metadata", None))
    if usage:
        message["usage"] = usage
        model = _as_dict(getattr(response, "usage_metadata", None)).get("model_name")
        if model:
            message["model"] = str(model)
    metadata = _as_dict(getattr(response, "metadata", None))
    provider = metadata.get("provider") or metadata.get("model_provider")
    api = metadata.get("api") or metadata.get("api_type")
    if provider:
        message["provider"] = str(provider)
    if api:
        message["api"] = str(api)
    model = metadata.get("model") or metadata.get("model_name")
    if model and "model" not in message:
        message["model"] = str(model)
    message["stopReason"] = _stop_reason(getattr(response, "finish_reason", "stop"))
    return message


def _tool_result_has_error(result: Any, exception: BaseException | None) -> bool:
    if exception is not None:
        return True
    if isinstance(result, Mapping):
        if result.get("is_error") is True or result.get("success") is False:
            return True
        status = str(result.get("status") or "").strip().lower()
        return status in {"error", "failed", "failure", "timeout"}
    return bool(getattr(result, "is_error", False))


def _tool_result_message(
    ctx: AgentCallbackContext, inputs: ToolCallInputs
) -> dict[str, Any]:
    tool_message = inputs.tool_msg
    raw_call = inputs.tool_call
    tool_call_id = str(
        getattr(tool_message, "tool_call_id", "") or getattr(raw_call, "id", "") or ""
    )
    tool_name = str(inputs.tool_name or getattr(raw_call, "name", "") or "")
    content = _content_blocks(getattr(tool_message, "content", ""))
    if not content:
        content = _content_blocks(inputs.tool_result)
    if not content and ctx.exception is not None:
        content = _content_blocks(str(ctx.exception))
    return {
        "role": "toolResult",
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "content": content,
        "isError": _tool_result_has_error(inputs.tool_result, ctx.exception),
    }


def _resolve_session(ctx: AgentCallbackContext) -> tuple[str, str]:
    extra = ctx.extra if isinstance(ctx.extra, dict) else {}
    invocation = None
    try:
        invocation = get_current_invocation_context()
    except Exception:
        invocation = None
    session_id = str(
        getattr(invocation, "session_id", None) or extra.get(_SID_KEY) or ""
    ).strip()
    if not session_id and isinstance(ctx.inputs, InvokeInputs):
        session_id = str(ctx.inputs.conversation_id or "").strip()
    channel_id = str(
        getattr(invocation, "channel_id", None) or extra.get(_CHANNEL_KEY) or ""
    ).strip().lower()
    return session_id, channel_id


class OpenClawHistoryRail(DeepAgentRail):
    """Observe raw runtime messages without mutating model or tool state."""

    priority = 10

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        if not OPENCLAW_HISTORY_ENABLED or ctx.session is None:
            return
        try:
            if not isinstance(ctx.extra, dict):
                ctx.extra = {}
            session_id, channel_id = _resolve_session(ctx)
            if session_id and channel_id in _SUPPORTED_CHANNELS:
                ctx.extra[_SID_KEY] = session_id
                ctx.extra[_CHANNEL_KEY] = channel_id
                mark_openclaw_session_active(session_id)
        except Exception:
            logger.warning("[OpenClawHistory] before_invoke failed", exc_info=True)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if not OPENCLAW_HISTORY_ENABLED:
            return
        try:
            if not isinstance(ctx.extra, dict):
                return
            session_id = str(ctx.extra.pop(_SID_KEY, "") or "").strip()
            ctx.extra.pop(_CHANNEL_KEY, None)
            if session_id:
                release_openclaw_session(session_id)
        except Exception:
            logger.warning("[OpenClawHistory] after_invoke failed", exc_info=True)

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        if (
            not OPENCLAW_HISTORY_ENABLED
            or ctx.session is None
            or not isinstance(
            ctx.inputs, UserMessageInputs
            )
        ):
            return
        try:
            session_id, channel_id = _resolve_session(ctx)
            if not session_id or channel_id not in _SUPPORTED_CHANNELS:
                return
            content = _user_content_blocks(ctx.inputs.parts)
            if not content:
                return
            append_openclaw_message(
                session_id,
                {
                    "role": "user",
                    "content": content,
                },
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.warning("[OpenClawHistory] on_user_message failed", exc_info=True)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if (
            not OPENCLAW_HISTORY_ENABLED
            or ctx.session is None
            or not isinstance(ctx.inputs, ModelCallInputs)
        ):
            return
        try:
            session_id, channel_id = _resolve_session(ctx)
            if not session_id or channel_id not in _SUPPORTED_CHANNELS:
                return
            response = ctx.inputs.response
            if response is None:
                return
            append_openclaw_message(
                session_id,
                _assistant_message(response),
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.warning("[OpenClawHistory] after_model_call failed", exc_info=True)

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        if not OPENCLAW_HISTORY_ENABLED or ctx.session is None:
            return
        try:
            session_id, channel_id = _resolve_session(ctx)
            if not session_id or channel_id not in _SUPPORTED_CHANNELS:
                return
            error = str(ctx.exception or "model call failed")
            append_openclaw_message(
                session_id,
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": error}],
                    "stopReason": "error",
                },
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.warning("[OpenClawHistory] on_model_exception failed", exc_info=True)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if (
            not OPENCLAW_HISTORY_ENABLED
            or ctx.session is None
            or not isinstance(ctx.inputs, ToolCallInputs)
        ):
            return
        try:
            session_id, channel_id = _resolve_session(ctx)
            if not session_id or channel_id not in _SUPPORTED_CHANNELS:
                return
            append_openclaw_message(
                session_id,
                _tool_result_message(ctx, ctx.inputs),
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception:
            logger.warning("[OpenClawHistory] after_tool_call failed", exc_info=True)


__all__ = ["OpenClawHistoryRail"]
