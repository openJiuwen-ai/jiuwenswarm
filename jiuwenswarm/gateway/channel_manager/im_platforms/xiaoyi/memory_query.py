"""Xiaoyi ``AgentEvent.MemoryQuery`` memory state commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import (
    read_memory_state,
    set_memory_state,
)


@dataclass(frozen=True)
class MemoryQueryContext:
    action: str
    params: dict[str, Any]
    session_id: str
    task_id: str
    message_id: str


def configured_runtime_state_path() -> str:
    """Read the configured Xiaoyi memory state file override."""
    try:
        from jiuwenswarm.agents.harness.common.memory.config import _load_config
        from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
            get_external_memory_config,
        )

        celia = get_external_memory_config(_load_config()).get("celia") or {}
        return str(celia.get("runtime_state_path") or "").strip()
    except Exception:
        return ""


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            yield from _walk(json.loads(value))
        except json.JSONDecodeError:
            return


def extract_memory_query(message: dict[str, Any]) -> MemoryQueryContext | None:
    command = None
    for candidate in _walk(message):
        header = candidate.get("header")
        if (
            isinstance(header, dict)
            and header.get("namespace") == "AgentEvent"
            and header.get("name") == "MemoryQuery"
        ):
            command = candidate
            break
    if command is None:
        return None
    nested = None
    detail = message.get("msgDetail")
    if isinstance(detail, str):
        try:
            decoded = json.loads(detail)
            nested = decoded if isinstance(decoded, dict) else None
        except json.JSONDecodeError:
            nested = None
    correlation = nested or message
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    params = correlation.get("params") if isinstance(correlation.get("params"), dict) else {}
    command_params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    return MemoryQueryContext(
        action=str(payload.get("action") or ""),
        params=command_params,
        session_id=str(message.get("sessionId") or params.get("sessionId") or correlation.get("sessionId") or ""),
        task_id=str(message.get("taskId") or params.get("id") or correlation.get("taskId") or ""),
        message_id=str(correlation.get("id") or message.get("id") or params.get("messageId") or ""),
    )


def handle_memory_query(
    context: MemoryQueryContext,
    *,
    runtime_state_path: str = "",
) -> dict[str, Any]:
    action = context.action
    if action == "MemoryStateSet":
        value = context.params.get("memoryState")
        # Match OpenClaw wire behavior for malformed input: code=0, no mutation.
        if isinstance(value, bool):
            set_memory_state(value, runtime_state_path)
        return {"code": 0}
    if action == "MemoryStateGet":
        return {"memoryState": read_memory_state(runtime_state_path)}
    return {"error": f"Unknown action: {action}"}


def memory_query_command(action: str, answer: Any) -> dict[str, Any]:
    return {
        "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
        "payload": {"action": action, "ans": answer},
    }
