# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude-shaped session query contracts, validation and type registration."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
import math
from pathlib import Path
from typing import Any, Literal, TypedDict

from openjiuwen.core.events import ExtensionEventBus

from jiuwenswarm.extensions_v2.event_types.matching import match_pattern_subset

SESSION_QUERY_EVENTS = (
    "session.cwd", "session.root", "session.model", "session.repo",
    "session.messages", "session.turns", "session.usage",
)


class SessionUsageArgs(TypedDict, total=False):
    """Optional query arguments; no session identity is accepted."""

    breakdown: Literal["summary", "full"]
    columns: int


class SessionQueryValue(TypedDict):
    """A successful event result; Ops callers receive only ``value``."""

    value: Any


class SessionQueryDeny(TypedDict):
    """A deliberate query refusal, distinct from an invalid Handler output."""

    deny: str


class SessionQueryError(RuntimeError):
    """A stable session-query boundary error without platform data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"session query failed: {code}")


class SessionQueryDenied(SessionQueryError):
    """A query Handler explicitly returned a non-empty ``deny`` reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("DENIED")


def valid_query_input(event_type: str, value: object) -> bool:
    """Validate the public call arguments at entry and every next boundary."""
    if not isinstance(value, Mapping):
        return False
    if event_type != "session.usage":
        return not value
    if set(value) - {"breakdown", "columns"}:
        return False
    if "breakdown" in value and value["breakdown"] not in ("summary", "full"):
        return False
    columns = value.get("columns")
    return "columns" not in value or isinstance(columns, int) and not isinstance(columns, bool) and columns > 0


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _path(value: object) -> bool:
    return isinstance(value, str) and bool(value) and Path(value).is_absolute()


def _plain_data(value: object, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _plain_data(item, depth + 1) for key, item in value.items())
    return isinstance(value, (list, tuple)) and all(_plain_data(item, depth + 1) for item in value)


def _tool_result(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and {"tool_use_id", "text", "isError"} <= set(value)
        and not set(value) - {"tool_use_id", "text", "isError", "result"}
        and _text(value["tool_use_id"])
        and isinstance(value["text"], str)
        and isinstance(value["isError"], bool)
    )


def _tool_use(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and {"tool_use_id", "tool", "input"} <= set(value)
        and not set(value) - {"tool_use_id", "tool", "input", "result", "text", "isError"}
        and _text(value["tool_use_id"]) and _text(value["tool"])
        and isinstance(value["input"], Mapping)
        and ("text" not in value or isinstance(value["text"], str))
        and ("isError" not in value or value["isError"] is True)
    )


def _messages(value: object) -> bool:
    if not isinstance(value, list) or len(value) > 4096:
        return False
    for message in value:
        if not isinstance(message, Mapping) or not {"role", "text", "toolUses"} <= set(message):
            return False
        if set(message) - {"role", "text", "toolUses", "toolResults"}:
            return False
        if message["role"] not in ("user", "assistant") or not isinstance(message["text"], str):
            return False
        if not isinstance(message["toolUses"], list) or not all(_tool_use(item) for item in message["toolUses"]):
            return False
        if "toolResults" in message and (
            message["role"] != "user" or not isinstance(message["toolResults"], list)
            or not all(_tool_result(item) for item in message["toolResults"])
        ):
            return False
    return True


def _repo(value: object) -> bool:
    return (
        isinstance(value, Mapping) and set(value) == {"root", "remote", "internal", "name"}
        and _path(value["root"]) and (value["remote"] is None or _text(value["remote"]))
        and isinstance(value["internal"], bool)
        and (value["name"] is None or _text(value["name"]))
        and (_text(value["name"]) if value["internal"] else value["name"] is None)
    )


def _usage(value: object) -> bool:
    if not isinstance(value, Mapping) or not {"context", "rateLimits"} <= set(value):
        return False
    if set(value) - {"context", "rateLimits", "cost"} or not isinstance(value["context"], Mapping):
        return False
    context = value["context"]
    if set(context) - {"tokens", "window", "percent", "breakdown"}:
        return False
    if any(not _number(context[key]) for key in ("tokens", "window", "percent") if key in context):
        return False
    if "window" in context and context["window"] <= 0:
        return False
    if "percent" in context and (not isinstance(context["percent"], int) or context["percent"] > 100):
        return False
    if "breakdown" in context and not isinstance(context["breakdown"], Mapping):
        return False
    if not isinstance(value["rateLimits"], list):
        return False
    for window in value["rateLimits"]:
        if (
            not isinstance(window, Mapping) or not {"kind", "percentUsed"} <= set(window)
            or set(window) - {"kind", "percentUsed", "resetsAt"}
            or not _text(window["kind"]) or not _number(window["percentUsed"])
            or "resetsAt" in window and not _text(window["resetsAt"])
        ):
            return False
    return "cost" not in value or (
        isinstance(value["cost"], Mapping) and set(value["cost"]) == {"usd"} and _number(value["cost"]["usd"])
    )


def valid_query_output(event_type: str, value: object) -> bool:
    """Reject bare values, ambiguous envelopes and incompatible public views."""
    if not isinstance(value, Mapping):
        return False
    if set(value) == {"deny"}:
        return _text(value["deny"]) and len(value["deny"]) <= 512
    if set(value) != {"value"}:
        return False
    answer = value["value"]
    if not _plain_data(answer):
        return False
    if event_type in ("session.cwd", "session.root"):
        return answer is None or _path(answer)
    if event_type == "session.model":
        return answer is None or _text(answer)
    if event_type == "session.turns":
        return answer is None or isinstance(answer, int) and not isinstance(answer, bool) and answer >= 0
    if event_type == "session.repo":
        return answer is None or _repo(answer)
    if event_type == "session.messages":
        return _messages(answer)
    return event_type == "session.usage" and _usage(answer)


def register_session_query_events(bus: ExtensionEventBus) -> None:
    """Register all seven query types with one platform CORE per event."""
    from jiuwenswarm.extensions_v2.event_handlers.session_queries import session_query_core

    for event_type in SESSION_QUERY_EVENTS:
        bus.register_event_type(
            event_type, match_pattern_subset, session_query_core,
            input_validator=partial(valid_query_input, event_type),
            output_validator=partial(valid_query_output, event_type),
        )
