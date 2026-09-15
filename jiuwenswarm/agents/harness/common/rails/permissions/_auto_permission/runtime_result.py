"""Migrated Auto Permission runtime result slice."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    classify_permission_result,
)


def _resolve_base_call_args(
    before_tool_call: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    invocation: ToolInvocation,
) -> tuple[Any, ...]:
    candidate_args = args
    if not candidate_args:
        candidate_args = (invocation.ctx, invocation.tool_call)
    elif _needs_runtime_tool_call(
        before_tool_call,
        candidate_args,
        kwargs,
        invocation,
    ):
        candidate_args = (*candidate_args, invocation.tool_call)
    capacity = _positional_capacity(before_tool_call)
    if capacity is None:
        return candidate_args
    if capacity <= 0:
        return ()
    if capacity == 1 and len(candidate_args) > 1:
        return (invocation.ctx,)
    return candidate_args[:capacity]


def _needs_runtime_tool_call(
    callable_obj: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    invocation: ToolInvocation,
) -> bool:
    if not (
        len(args) == 1
        and args[0] is invocation.ctx
        and _is_openjiuwen_runtime_context(invocation.ctx)
        and invocation.tool_call is not None
    ):
        return False
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    positional: list[inspect.Parameter] = []
    positional_kinds = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    }
    for parameter in signature.parameters.values():
        if parameter.kind in positional_kinds:
            positional.append(parameter)
    if len(positional) < 2:
        return False
    second = positional[1]
    if second.default is not inspect.Parameter.empty:
        return False
    return second.kind == inspect.Parameter.POSITIONAL_ONLY or second.name not in kwargs


def _positional_capacity(callable_obj: Callable[..., Any]) -> int | None:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return None
    positional_count = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return None
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional_count += 1
    return positional_count


def _filter_base_call_kwargs(
    before_tool_call: Callable[..., Any], kwargs: dict[str, Any]
) -> dict[str, Any]:
    if not kwargs:
        return {}
    try:
        signature = inspect.signature(before_tool_call)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return dict(kwargs)
    filtered: dict[str, Any] = {}
    for key, value in kwargs.items():
        parameter = parameters.get(key)
        if parameter is not None and parameter.kind != inspect.Parameter.POSITIONAL_ONLY:
            filtered[key] = value
    return filtered


def _result_from_base_side_effect(ctx: Any | None, result: Any) -> Any:
    if result is not None or ctx is None:
        return result
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, Mapping) or extra.get("_skip_tool") is not True:
        return result
    inputs = getattr(ctx, "inputs", None)
    tool_result = getattr(inputs, "tool_result", None)
    if tool_result is None:
        return result
    return tool_result


def _is_openjiuwen_runtime_context(ctx: Any | None) -> bool:
    return isinstance(ctx, AgentCallbackContext)


def _permission_tool_result_text(result: Any) -> str:
    feedback = _permission_feedback_text(result)
    if feedback:
        return feedback
    if isinstance(result, str) and result:
        return result
    return "[PERMISSION_DENIED] Tool execution was denied."


def _permission_tool_result_payload(result: Any, text: str) -> Any:
    if _is_permission_denied_metadata_payload(result):
        return _permission_denied_tool_result_payload(result, text)
    return text




def _is_permission_denied_metadata_payload(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    if str(result.get("permission_status") or "").strip().lower() == "denied":
        return True
    if str(result.get("status") or "").strip().lower() == "denied":
        return True
    return "[permission_denied]" in _permission_feedback_text(result).lower()


def _permission_denied_tool_result_payload(
    result: Any,
    text: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "status": "denied",
        "is_error": True,
        "result": text,
        "error": text,
        "permission_decision": "deny",
        "permission_status": "denied",
    }
    if isinstance(result, Mapping):
        reviewer_metadata = result.get("reviewer_metadata")
        if isinstance(reviewer_metadata, Mapping):
            payload["reviewer_metadata"] = dict(reviewer_metadata)
        reason = result.get("reason")
        if reason:
            payload["reason"] = str(reason)
    return payload


def _permission_interrupt_message(result: Any) -> str:
    if isinstance(result, Mapping):
        value = result.get("value")
        if isinstance(value, Mapping):
            message = value.get("message")
            if message is not None:
                return str(message)
        message = result.get("message")
        if message is not None:
            return str(message)
    feedback = _permission_feedback_text(result)
    if feedback:
        return feedback
    return "manual approval required"


def _permission_interrupt_extra_fields(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    value = result.get("value")
    extra_fields: dict[str, Any] = {}
    metadata = result.get("metadata")
    if isinstance(metadata, Mapping):
        extra_fields.update(metadata)
    if isinstance(value, Mapping):
        extra_fields.update(
            {
                str(key): nested_value
                for key, nested_value in value.items()
                if key not in {"message", "question"}
            }
        )
    return extra_fields


def _permission_terminal_outcome(result: Any) -> str | None:
    result_class = classify_permission_result(result)
    return {
        "allow": "allow",
        "denied": "deny",
        "interrupt": None,
        "user_rejection": "cancel",
    }.get(result_class)


def _permission_terminal_decision_source(source: str) -> str:
    """Normalize an explicit host-controlled source for audit observation."""
    normalized = str(source or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized):
        return normalized
    return "unknown"


def _permission_feedback_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        raw_feedback = result.get("feedback")
        if raw_feedback is None and isinstance(result.get("value"), Mapping):
            raw_feedback = result["value"].get("feedback")
        return str(raw_feedback or "")
    return str(getattr(result, "feedback", "") or "")
