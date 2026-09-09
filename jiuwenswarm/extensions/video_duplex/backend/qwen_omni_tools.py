"""Qwen Omni Realtime tool definitions and request validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


QWEN_OMNI_RESEARCH_TOOL_NAME = "jiuwen_research"
_MAX_CALL_ID_CHARS = 200
_MAX_QUERY_CHARS = 500


@dataclass(frozen=True)
class QwenOmniToolCall:
    name: str
    call_id: str
    arguments: dict[str, Any]
    query: str


def qwen_omni_tools() -> list[dict[str, Any]]:
    """Return fresh Qwen-compatible tool definitions for each session."""
    return [
        {
            "type": "function",
            "function": {
                "name": QWEN_OMNI_RESEARCH_TOOL_NAME,
                "description": (
                    "Use Jiuwen Core Agent to research external or time-sensitive facts, "
                    "such as weather, news, prices, company information, people, places, "
                    "or facts that are not established by the current video and conversation. "
                    "Resolve visual references such as 'this brand' in the query when possible."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "A self-contained search request including the resolved subject, "
                                "place, date, and other context needed for accurate research."
                            ),
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def parse_qwen_omni_tool_call(value: Any) -> QwenOmniToolCall:
    """Validate the only Qwen tool currently exposed by the video gateway."""
    if not isinstance(value, dict):
        raise ValueError("tool call must be an object")

    name = str(value.get("name") or "").strip()
    if name != QWEN_OMNI_RESEARCH_TOOL_NAME:
        raise ValueError(f"unsupported Qwen tool: {name or '<empty>'}")

    call_id = str(value.get("call_id") or "").strip()
    if not call_id or len(call_id) > _MAX_CALL_ID_CHARS:
        raise ValueError(f"call_id must contain 1-{_MAX_CALL_ID_CHARS} characters")

    raw_arguments = value.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments must be valid JSON") from exc
    elif isinstance(raw_arguments, dict):
        arguments = dict(raw_arguments)
    else:
        raise ValueError("arguments must be a JSON object")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    if set(arguments) != {"query"}:
        raise ValueError("arguments must contain only query")

    raw_query = arguments.get("query")
    if not isinstance(raw_query, str):
        raise ValueError("query must be a string")
    query = raw_query.strip()
    if not query or len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"query must contain 1-{_MAX_QUERY_CHARS} characters")
    return QwenOmniToolCall(
        name=name,
        call_id=call_id,
        arguments=arguments,
        query=query,
    )
