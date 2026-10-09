# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable public event fields added at the Process CLI transport boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

PROTOCOL_REVISION = 1
STABLE_EVENT_TYPES = (
    "chat.delta",
    "chat.final",
    "chat.tool_call",
    "chat.tool_result",
    "interaction.requested",
    "host_tool.requested",
)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Tool arguments must contain finite JSON numbers")
    return number


def _reject_constant(_value: str) -> None:
    raise ValueError("Tool arguments must contain JSON numbers")


def _text(payload: Mapping[str, Any]) -> str:
    for key in ("delta", "content", "text", "message", "answer"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _questions(interaction: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for index, question in enumerate(interaction.get("questions") or ()):
        if not isinstance(question, Mapping):
            continue
        options = []
        for option in question.get("options") or ():
            if isinstance(option, Mapping):
                value = str(option.get("value", option.get("label", "")))
                options.append(
                    {"value": value, "label": str(option.get("label", value))}
                )
            elif isinstance(option, str):
                options.append({"value": option, "label": option})
        card_id = question.get("card_id")
        result.append(
            {
                "question_id": f"q{index}",
                "question": str(
                    question.get("question") or question.get("title") or ""
                ),
                "options": options,
                "card_id": card_id if isinstance(card_id, str) else None,
                "allow_custom_input": bool(
                    question.get(
                        "allow_custom_input",
                        interaction.get("source") != "permission_interrupt",
                    )
                ),
            }
        )
    return result


def public_event_payload(event_type: str, payload: Any) -> Any:
    """Return a detached payload, preserving legacy fields for existing hosts."""
    if event_type not in STABLE_EVENT_TYPES:
        return payload
    result = deepcopy(dict(payload)) if isinstance(payload, Mapping) else {}
    if event_type in ("chat.delta", "chat.final"):
        result["text"] = _text(result)
    elif event_type == "interaction.requested":
        interaction = result.get("interaction")
        interaction = interaction if isinstance(interaction, Mapping) else {}
        source = interaction.get("source")
        result["kind"] = (
            "permission"
            if source == "permission_interrupt"
            else "confirmation"
            if source == "confirm_interrupt"
            else "question"
        )
        result["questions"] = _questions(interaction)
    elif event_type in ("chat.tool_call", "chat.tool_result"):
        call = result.get("tool_call")
        call = call if isinstance(call, Mapping) else result
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(
                    arguments,
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                )
            except ValueError:
                arguments = None
        if not isinstance(arguments, Mapping):
            arguments = None
        success = result.get("success")
        result["tool"] = {
            "call_id": str(
                call.get("tool_call_id") or result.get("tool_call_id") or ""
            ),
            "name": str(call.get("name") or result.get("tool_name") or ""),
            "arguments": dict(arguments) if arguments is not None else None,
            "status": "started"
            if event_type == "chat.tool_call"
            else "failed"
            if success is False
            else "completed",
            "result": result.get("result")
            if event_type == "chat.tool_result"
            else None,
        }
    return result
