from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any


def _get_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        try:
            return value.get(name, default)
        except Exception:
            return default
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return _stable_json(value)
    if type(value).__str__ is object.__str__:
        return f"<{type(value).__name__}>"
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _json_safe(value: Any, *, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    seen = _seen if _seen is not None else set()
    value_id = id(value)
    if value_id in seen:
        return "<cycle>"

    if isinstance(value, Mapping):
        seen.add(value_id)
        try:
            try:
                return {
                    _safe_string(key): _json_safe(item, _seen=seen)
                    for key, item in value.items()
                }
            except Exception:
                return f"<{type(value).__name__}>"
        finally:
            seen.remove(value_id)
    if isinstance(value, (list, tuple)):
        seen.add(value_id)
        try:
            try:
                return [_json_safe(item, _seen=seen) for item in value]
            except Exception:
                return f"<{type(value).__name__}>"
        finally:
            seen.remove(value_id)
    if isinstance(value, (set, frozenset)):
        seen.add(value_id)
        try:
            try:
                normalized = [_json_safe(item, _seen=seen) for item in value]
                return sorted(normalized, key=_stable_json)
            except Exception:
                return f"<{type(value).__name__}>"
        finally:
            seen.remove(value_id)

    model_dump = _get_value(value, "model_dump")
    if callable(model_dump):
        seen.add(value_id)
        try:
            try:
                dumped = model_dump()
            except Exception:
                dumped = None
            else:
                return _json_safe(dumped, _seen=seen)
        finally:
            seen.remove(value_id)
    if is_dataclass(value) and not isinstance(value, type):
        seen.add(value_id)
        try:
            return {
                field.name: _json_safe(_get_value(value, field.name), _seen=seen)
                for field in fields(value)
            }
        finally:
            seen.remove(value_id)
    try:
        attributes = vars(value)
    except (TypeError, ValueError):
        attributes = None
    if attributes:
        seen.add(value_id)
        try:
            return {
                name: _json_safe(item, _seen=seen)
                for name, item in attributes.items()
                if not name.startswith("_")
            }
        finally:
            seen.remove(value_id)

    if type(value).__str__ is not object.__str__:
        return _safe_string(value)
    return f"<{type(value).__name__}>"


def _stable_json(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_json(value: Any, *, max_chars: int) -> str:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError("max_chars must be a non-negative integer")
    if max_chars == 0:
        return ""
    return _stable_json(value)[:max_chars]


def _bounded_value(value: Any, *, max_chars: int) -> Any:
    normalized = _json_safe(value)
    if isinstance(normalized, str):
        return normalized[:max_chars]
    serialized = _stable_json(normalized)
    return normalized if len(serialized) <= max_chars else serialized[:max_chars]


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        if isinstance(value, Sequence):
            return list(value)
        return list(value)
    except Exception:
        return []


def _input_message_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    try:
        if isinstance(value, Sequence):
            return list(value)
        return list(value)
    except Exception:
        return [
            {
                "role": "unknown",
                "content": f"<{type(value).__name__}>",
            }
        ]


def message_role(message: Any) -> str:
    role = _get_value(message, "role")
    text = _safe_string(role).strip()
    return text or "unknown"


def message_content(message: Any) -> str:
    return _safe_string(_get_value(message, "content"))


def _message_entry(message: Any, *, max_chars: int = 4096) -> dict[str, Any]:
    role = message_role(message)
    content = message_content(message)
    if role == "tool":
        content = content[:max_chars]
    entry: dict[str, Any] = {
        "role": role,
        "parts": [{"type": "text", "content": content}],
    }
    if entry["role"] == "tool":
        tool_call_id = _safe_string(_get_value(message, "tool_call_id"))
        entry["tool_call_id"] = tool_call_id
    return entry


def serialize_input_messages(messages: Any, *, max_chars: int) -> str:
    return _bounded_json(
        [
            _message_entry(message, max_chars=max_chars)
            for message in _input_message_items(messages)
        ],
        max_chars=max_chars,
    )


def _tool_call_entry(tool_call: Any, *, max_chars: int = 4096) -> dict[str, Any]:
    function = _get_value(tool_call, "function")
    name = _get_value(tool_call, "name")
    if not name:
        name = _get_value(function, "name")
    arguments = _get_value(tool_call, "arguments")
    if arguments is None:
        arguments = _get_value(function, "arguments")
    entry: dict[str, Any] = {
        "id": _safe_string(_get_value(tool_call, "id")),
        "name": _safe_string(name),
    }
    if arguments not in (None, ""):
        entry["arguments"] = _bounded_value(arguments, max_chars=max_chars)
    return entry


def serialize_output_message(result: Any, *, max_chars: int) -> str:
    content = _safe_string(_get_value(result, "content"))
    output: dict[str, Any] = {
        "role": "assistant",
        "parts": [{"type": "text", "content": content}],
    }
    tool_calls = _items(_get_value(result, "tool_calls"))
    if tool_calls:
        output["tool_calls"] = [
            _tool_call_entry(call, max_chars=max_chars) for call in tool_calls
        ]
    reasoning = _safe_string(_get_value(result, "reasoning_content"))
    if reasoning:
        output["parts"].append({"type": "reasoning", "content": reasoning[:max_chars]})
    return _bounded_json([output], max_chars=max_chars)


def _schema_value(value: Any) -> Any:
    if isinstance(value, type):
        schema = _get_value(value, "model_json_schema")
        if callable(schema):
            try:
                return schema()
            except Exception:
                return f"<{value.__name__}>"
    return value


def _canonical_tool_definition(tool: Any) -> dict[str, Any] | None:
    function = _get_value(tool, "function")
    source = function if function is not None else tool
    name = _safe_string(_get_value(source, "name") or _get_value(tool, "name"))
    if not name:
        return None

    definition: dict[str, Any] = {
        "type": _safe_string(_get_value(tool, "type", "function")) or "function",
        "name": name,
    }
    description = _get_value(source, "description") or _get_value(tool, "description")
    if not description:
        description = _get_value(_get_value(tool, "card"), "description")
    if description:
        definition["description"] = _safe_string(description)
    parameters = _get_value(source, "parameters")
    if parameters is None:
        parameters = _get_value(tool, "parameters")
    if parameters is not None:
        definition["parameters"] = _json_safe(_schema_value(parameters))
    return definition


def serialize_tool_definitions(tools: Any, *, max_chars: int) -> str:
    definitions = []
    for tool in _items(tools):
        definition = _canonical_tool_definition(tool)
        if definition is not None:
            definitions.append(definition)
    return _bounded_json(definitions, max_chars=max_chars)


def classify_decision(result: Any) -> tuple[str, list[str]]:
    raw_tool_calls = _get_value(result, "tool_calls")
    tool_calls = _items(raw_tool_calls) if raw_tool_calls is not None else []
    if tool_calls:
        names = []
        for call in tool_calls:
            entry = _tool_call_entry(call)
            if entry["name"]:
                names.append(entry["name"])
        return "tool_call", names
    if message_content(result).strip():
        return "answer", []
    return "unknown", []


__all__ = [
    "classify_decision",
    "message_content",
    "message_role",
    "serialize_input_messages",
    "serialize_output_message",
    "serialize_tool_definitions",
]
