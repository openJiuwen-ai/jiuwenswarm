# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapt AgentSSAS lifecycle events to AgentMoss runtime events.

The adapter is deliberately structural: it maps event lifecycle semantics and
explicit correlation identifiers. It does not infer dependencies from natural
language content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


ADAPTER_VERSION = "1.0"
MODEL_TYPE = "agent_behavior_model"

_EVENT_TYPE_MAP = {
    "invoke_start": "chat_request",
    "invoke_end": "model_output",
    "llm_input": "model_call",
    "llm_output": "model_output",
    "tool_input": "tool_call",
    "tool_output": "tool_result",
}


def adapt_event_desc(event_desc: dict[str, Any]) -> dict[str, Any]:
    """Convert an AgentSSAS event description to the AgentMoss input model."""
    if not isinstance(event_desc, dict):
        raise TypeError(
            f"event_desc 必须为 dict，实际类型: {type(event_desc).__name__}"
        )

    event_node = _mapping(event_desc.get("event_node"))
    aux_ids = _mapping(event_desc.get("aux_ids"))
    trace = _mapping(event_desc.get("trace"))

    source_event_type = _text(
        event_node.get("event_type") or event_desc.get("event_type")
    )
    event_class = _text(
        event_node.get("event_class") or event_desc.get("event_class")
    )
    action_name = _text(
        event_node.get("action_name")
        or event_desc.get("action_name")
    )
    input_content = _first(
        event_node.get("input_content"), event_desc.get("input_content")
    )
    output_content = _first(
        event_node.get("output_content"), event_desc.get("output_content")
    )
    node_id = _text(event_node.get("node_id") or event_desc.get("node_id"))
    mapped_event_type = _EVENT_TYPE_MAP.get(source_event_type, "")
    tool_call_id = _tool_call_id(
        source_event_type=source_event_type,
        event_node=event_node,
        aux_ids=aux_ids,
        node_id=node_id,
    )

    session_id = _text(
        aux_ids.get("session_id") or event_node.get("session_id")
    )
    trace_id = _text(aux_ids.get("trace_id") or trace.get("trace_id"))
    agent_name = _text(
        aux_ids.get("agent_id") or event_node.get("agent_id")
    )
    timestamp = _float(event_node.get("timestamp") or event_desc.get("end_time"))
    event_id = _text(event_desc.get("event_id")) or _fallback_event_id(
        source_event_type=source_event_type,
        node_id=node_id,
        session_id=session_id,
        timestamp=timestamp,
        input_content=input_content,
        output_content=output_content,
    )

    agentmoss_event = {
        "event_type": mapped_event_type,
        "subject": _subject_for(source_event_type, action_name),
        "payload": _payload_for(
            mapped_event_type=mapped_event_type,
            input_content=input_content,
            output_content=output_content,
            tool_call_id=tool_call_id,
        ),
        "session_id": session_id,
        "request_id": trace_id,
        "agent_name": agent_name,
        "source": _text(event_node.get("source")) or "agent_ssas",
        "event_id": event_id,
        "timestamp": timestamp,
    }

    return {
        "model_type": MODEL_TYPE,
        "adapter_version": ADAPTER_VERSION,
        "supported": bool(mapped_event_type),
        # Compatibility fields retained for existing AgentSSAS consumers.
        "event_type": source_event_type,
        "event_class": event_class,
        "action_name": action_name,
        "input_content": input_content,
        "output_content": output_content,
        "node_type": _text(
            event_node.get("node_type") or event_desc.get("node_type")
        ),
        "aux_ids": aux_ids,
        "trace": trace,
        "event_id": event_id,
        "agentmoss_event": agentmoss_event,
        "correlation": {
            "trace_id": trace_id,
            "session_id": session_id,
            "interaction_seq": _integer(
                aux_ids.get("interaction_seq"), default=-1
            ),
            "llm_call_seq": _integer(aux_ids.get("llm_call_seq"), default=-1),
            "tool_call_seq": _integer(
                aux_ids.get("tool_call_seq"), default=-1
            ),
            "tool_call_id": tool_call_id,
            "node_id": node_id,
        },
    }


def _payload_for(
    *,
    mapped_event_type: str,
    input_content: Any,
    output_content: Any,
    tool_call_id: str,
) -> dict[str, Any]:
    decoded_input = _decode_jsonish(input_content)
    decoded_output = _decode_jsonish(output_content)
    if mapped_event_type == "chat_request":
        return {"content": decoded_input}
    if mapped_event_type == "model_call":
        return {"messages": decoded_input}
    if mapped_event_type == "model_output":
        return {"response": decoded_output or decoded_input}
    if mapped_event_type == "tool_call":
        return {
            "tool_args": decoded_input,
            "tool_call_id": tool_call_id,
        }
    if mapped_event_type == "tool_result":
        return {
            "tool_result": decoded_output or decoded_input,
            "tool_call_id": tool_call_id,
        }
    return {}


def _subject_for(source_event_type: str, action_name: str) -> str:
    if source_event_type == "invoke_start":
        return "user"
    if source_event_type == "invoke_end":
        return "agent_response"
    if source_event_type in {"llm_input", "llm_output"}:
        return action_name or "llm_call"
    return action_name


def _tool_call_id(
    *,
    source_event_type: str,
    event_node: dict[str, Any],
    aux_ids: dict[str, Any],
    node_id: str,
) -> str:
    if source_event_type not in {"tool_input", "tool_output"}:
        return ""
    explicit = _text(
        event_node.get("tool_call_id") or aux_ids.get("tool_call_id")
    )
    if explicit:
        return explicit
    if node_id:
        return node_id
    session_id = _text(aux_ids.get("session_id"))
    interaction_seq = _integer(aux_ids.get("interaction_seq"), default=-1)
    tool_call_seq = _integer(aux_ids.get("tool_call_seq"), default=-1)
    if session_id and interaction_seq >= 0 and tool_call_seq >= 0:
        return f"{session_id}:{interaction_seq}:tool:{tool_call_seq}"
    return ""


def _fallback_event_id(
    *,
    source_event_type: str,
    node_id: str,
    session_id: str,
    timestamp: float,
    input_content: Any,
    output_content: Any,
) -> str:
    material = json.dumps(
        [
            source_event_type,
            node_id,
            session_id,
            timestamp,
            input_content,
            output_content,
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=repr,
    )
    return "ssas-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _decode_jsonish(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return _decode_jsonish(json.loads(stripped), depth + 1)
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
        return value
    if isinstance(value, dict):
        return {
            str(key): _decode_jsonish(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_decode_jsonish(item, depth + 1) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first(primary: Any, fallback: Any) -> Any:
    return primary if primary not in (None, "") else fallback or ""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
