from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .messages import _get_value
from .messages import _items
from .messages import _json_safe
from .messages import _safe_string
from .messages import _schema_value
from .messages import _stable_json
from .messages import message_content
from .messages import message_role


@dataclass(frozen=True)
class ContextTokenBreakdown:
    system_prompt: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_results: int = 0
    skill: int = 0
    tool_definitions: int = 0
    per_tool_tokens: tuple[tuple[str, int], ...] = ()
    per_skill_tokens: tuple[tuple[str, int], ...] = ()

    @property
    def message_total(self) -> int:
        return (
            self.system_prompt
            + self.user_messages
            + self.assistant_messages
            + self.tool_results
            + self.skill
        )

    @property
    def total(self) -> int:
        return self.message_total + self.tool_definitions


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    uncached_input_tokens: int = 0
    cache_usage_observed: bool = False


_TOKEN_COUNTER_UNSET = object()
_TOKEN_COUNTER_FAILED = object()
_token_counter: Any = _TOKEN_COUNTER_UNSET
_token_counter_lock = Lock()


def _get_token_counter() -> Any | None:
    global _token_counter
    if _token_counter is _TOKEN_COUNTER_UNSET:
        with _token_counter_lock:
            if _token_counter is _TOKEN_COUNTER_UNSET:
                try:
                    from openjiuwen.core.context_engine import TiktokenCounter

                    initialized = TiktokenCounter(model="gpt-4")
                except Exception:
                    initialized = _TOKEN_COUNTER_FAILED
                _token_counter = initialized
    return None if _token_counter is _TOKEN_COUNTER_FAILED else _token_counter


def _count_text(text: str, counter: Any | None) -> int:
    if not text:
        return 0
    if counter is not None:
        try:
            count = counter.count(text)
        except Exception:
            count = None
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
    return max(len(text) // 4, 0)


def _text_content(message: Any) -> str:
    content = _get_value(message, "content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if _get_value(part, "type") == "text":
                parts.append(
                    str(_get_value(part, "text") or _get_value(part, "content") or "")
                )
        return "\n".join(parts)
    return message_content(message)


def _metadata(message: Any) -> Mapping[str, Any]:
    metadata = _get_value(message, "metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _assistant_extra_text(message: Any) -> tuple[str, ...]:
    extras: list[str] = []
    tool_calls = _get_value(message, "tool_calls")
    if tool_calls:
        extras.append(_stable_json(tool_calls))
    reasoning = _get_value(message, "reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        extras.append(reasoning)
    return tuple(extras)


def _nested_tool_payload(tool: Any, index: int) -> tuple[str, str, str]:
    if isinstance(tool, Mapping):
        function = tool.get("function", tool)
        name = _safe_string(_get_value(function, "name"))
        normalized = _json_safe(tool)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fallback_text = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
        )
    else:
        function = _get_value(tool, "function")
        name = _safe_string(
            _get_value(tool, "name") or _get_value(function, "name")
        )
        description = _get_value(_get_value(tool, "card"), "description")
        if not description:
            description = _get_value(function, "description")
        if not description:
            description = _get_value(tool, "description")
        function_payload: dict[str, Any] = {
            "name": name,
            "description": _safe_string(description),
        }
        parameters = _get_value(function, "parameters")
        if parameters is None:
            parameters = _get_value(tool, "parameters")
        if parameters is not None:
            function_payload["parameters"] = _json_safe(_schema_value(parameters))
        normalized = {
            "type": _safe_string(_get_value(tool, "type", "function"))
            or "function",
            "function": function_payload,
        }
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fallback_text = ""

    piece = f"<|start|>functions.{name}:{index}\n{payload}<|end|>"
    return name or f"_unknown_{index}", piece, fallback_text or piece


def _tool_definition_tokens(
    tools: Any,
    counter: Any | None,
) -> tuple[int, dict[str, int]]:
    payloads = [
        _nested_tool_payload(tool, index)
        for index, tool in enumerate(_items(tools))
    ]
    if not payloads:
        return 0, {}

    def breakdown(counts: list[int], *, priming: bool) -> tuple[int, dict[str, int]]:
        per_tool: dict[str, int] = {}
        for (name, _, _), count in zip(payloads, counts, strict=True):
            per_tool[name] = per_tool.get(name, 0) + count
        return sum(counts) + (3 if priming else 0), per_tool

    def fallback() -> tuple[int, dict[str, int]]:
        return breakdown(
            [max(len(fallback_text) // 4, 0) for _, _, fallback_text in payloads],
            priming=False,
        )

    if counter is None:
        return fallback()
    counts: list[int] = []
    try:
        for _, piece, _ in payloads:
            count = counter.count(piece)
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                return fallback()
            counts.append(count)
    except Exception:
        return fallback()
    return breakdown(counts, priming=True)


def _skill_tool_call_names(messages: Any) -> dict[str, str]:
    skill_calls: dict[str, str] = {}
    for message in _items(messages):
        if message_role(message) != "assistant":
            continue
        for tool_call in _items(_get_value(message, "tool_calls")):
            function = _get_value(tool_call, "function")
            tool_name = _safe_string(
                _get_value(tool_call, "name") or _get_value(function, "name")
            ).strip()
            if tool_name != "skill_tool":
                continue
            call_id = _safe_string(_get_value(tool_call, "id")).strip()
            arguments = _get_value(tool_call, "arguments")
            if arguments is None:
                arguments = _get_value(function, "arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    arguments = None
            skill_name = _safe_string(_get_value(arguments, "skill_name")).strip()
            if call_id and skill_name:
                skill_calls[call_id] = skill_name
    return skill_calls


def count_context_tokens(messages: Any, tools: Any) -> ContextTokenBreakdown:
    counter = _get_token_counter()
    system_prompt = 0
    user_messages = 0
    assistant_messages = 0
    tool_results = 0
    skill = 0
    per_skill: dict[str, int] = {}
    skill_calls = _skill_tool_call_names(messages)

    for message in _items(messages):
        role = message_role(message)
        estimated = _count_text(_text_content(message), counter)
        metadata = _metadata(message)
        skill_name = ""
        is_skill_body = False
        if role == "tool" and (
            _get_value(metadata, "is_skill_body")
            or _get_value(metadata, "original_is_skill_body")
        ):
            is_skill_body = True
            skill_name = _safe_string(_get_value(metadata, "skill_name")).strip()
        elif role == "tool":
            tool_call_id = _safe_string(_get_value(message, "tool_call_id")).strip()
            skill_name = skill_calls.get(tool_call_id, "")
            is_skill_body = bool(skill_name)
        if role == "tool" and is_skill_body:
            skill += estimated
            if skill_name:
                per_skill[skill_name] = per_skill.get(skill_name, 0) + estimated
        elif role == "system" and _get_value(metadata, "active_skill_pin"):
            skill += estimated
            skill_name = _safe_string(
                _get_value(metadata, "skill_name")
                or _get_value(metadata, "active_skill_pin")
            ).strip()
            if skill_name.lower() not in {"", "true", "false"}:
                per_skill[skill_name] = per_skill.get(skill_name, 0) + estimated
        elif role == "system":
            system_prompt += estimated
        elif role == "user":
            user_messages += estimated
        elif role == "assistant":
            assistant_messages += estimated
            assistant_messages += sum(
                _count_text(extra, counter) for extra in _assistant_extra_text(message)
            )
        elif role == "tool":
            tool_results += estimated

    tool_definitions, per_tool = _tool_definition_tokens(tools, counter)
    return ContextTokenBreakdown(
        system_prompt=system_prompt,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_results=tool_results,
        skill=skill,
        tool_definitions=tool_definitions,
        per_tool_tokens=tuple(sorted(per_tool.items())),
        per_skill_tokens=tuple(sorted(per_skill.items())),
    )


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return 0
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(parsed, 0)
    return 0


def _first_count(source: Any, *names: str) -> int:
    for name in names:
        value = _token_count(_get_value(source, name))
        if value:
            return value
    return 0


def _first_present(source: Any, *names: str) -> Any:
    for name in names:
        value = _get_value(source, name)
        if value is not None:
            return value
    return None


def extract_usage(result: Any) -> UsageBreakdown:
    usage = _get_value(result, "usage_metadata")
    if usage is None:
        usage = _get_value(result, "usage")
    if usage is None:
        usage = result
    if usage is None:
        return UsageBreakdown()

    input_tokens = _first_count(usage, "input_tokens", "prompt_tokens")
    output_tokens = _first_count(usage, "output_tokens", "completion_tokens")
    prompt_details = _get_value(usage, "prompt_tokens_details")

    unified_read = _first_present(
        usage,
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cache_tokens",
    )
    unified_creation = _first_present(
        usage,
        "cache_creation_input_tokens",
        "cache_write_tokens",
    )
    unified_miss = _first_present(usage, "cache_miss_tokens")
    cache_status = _get_value(usage, "cache_status")

    prompt_cached = _first_present(
        prompt_details,
        "cached_tokens",
        "cachedTokens",
    )

    # The unified SDK usage is authoritative: its input_tokens already contains
    # cache reads and writes, so the cache components must not be added again.
    if any(
        value is not None
        for value in (unified_read, unified_creation, unified_miss)
    ) or cache_status is not None or prompt_cached is not None:
        cache_read = _token_count(unified_read)
        if not cache_read:
            cache_read = _token_count(prompt_cached)
        cache_creation = _token_count(unified_creation)
        miss_value = _token_count(unified_miss)
        uncached_input = (
            miss_value
            if unified_miss is not None
            else max(input_tokens - cache_read - cache_creation, 0)
        )
        cache_usage_observed = True
    else:
        # Legacy provider payloads may only expose OpenAI's nested details.
        cache_read = _token_count(prompt_cached)
        cache_creation = _first_count(usage, "cache_creation_input_tokens")
        uncached_input = (
            max(input_tokens - cache_read, 0)
            if prompt_details is not None
            else input_tokens
        )
        cache_usage_observed = prompt_details is not None

    completion_details = _get_value(usage, "completion_tokens_details")
    reasoning = _first_count(completion_details, "reasoning_tokens")
    if not reasoning:
        reasoning = _first_count(usage, "reasoning_tokens")

    return UsageBreakdown(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        reasoning_output_tokens=reasoning,
        uncached_input_tokens=uncached_input,
        cache_usage_observed=cache_usage_observed,
    )


__all__ = [
    "ContextTokenBreakdown",
    "UsageBreakdown",
    "count_context_tokens",
    "extract_usage",
]
