"""Xiaoyi ``AgentEvent.ClawSelfEvolutionState`` / ``ClawSelfEvolutionStateGet`` bridge.

1:1 translation of ``xy_channel-openclaw`` ``src/self-evolution-handler.ts``:

- ``handleSelfEvolutionEvent``        -> ``extract_self_evolution_set`` +
  ``celia.runtime_state.set_self_evolution_state`` (device reports state, persist
  to ``.xiaoyiruntime``, reply an empty final ACK)
- ``handleSelfEvolutionStateGetEvent`` -> ``extract_self_evolution_get`` +
  ``build_self_evolution_state_get_command`` (read persisted state, emit a
  ``Common/Action`` intent back to the device)

The persisted key ``selfEvolutionState=<state>`` lives in the same ``.xiaoyiruntime``
file as ``MEMORYSTATE`` (handled by ``memory_query``), under a different key.
``runtime_state`` already reads/writes arbitrary keys, so no storage layer is added.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SelfEvolutionContext:
    state: str
    session_id: str
    task_id: str
    message_id: str


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

def iter_agent_event_headers(message: dict[str, Any]) -> list[str]:
    """Yield every ``AgentEvent`` header ``name`` found in ``message``.

    Walks the message (direct or ``msgDetail``-wrapped A2A) and collects all
    ``header.namespace == "AgentEvent"`` names. Used for inbound diagnostics so
    we can see *every* command the device sends — including ones not yet handled
    (MemoryQuery / SelfEvolution / CronQuery / others).
    """
    names: list[str] = []
    for candidate in _walk(message):
        header = candidate.get("header")
        if isinstance(header, dict) and header.get("namespace") == "AgentEvent":
            names.append(str(header.get("name") or ""))
    return names


def _find_agent_event(message: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Walk message (direct or msgDetail-wrapped A2A) for an AgentEvent of ``name``."""
    for candidate in _walk(message):
        header = candidate.get("header")
        if (
            isinstance(header, dict)
            and header.get("namespace") == "AgentEvent"
            and header.get("name") == name
        ):
            return candidate
    return None


def _resolve_ids(message: dict[str, Any], command: dict[str, Any]):
    """Resolve session/task/message ids — same scheme as memory_query.extract_memory_query."""
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
    session_id = str(
        message.get("sessionId") or params.get("sessionId") or correlation.get("sessionId") or ""
    )
    task_id = str(
        message.get("taskId") or params.get("id") or correlation.get("taskId") or ""
    )
    message_id = str(
        correlation.get("id") or message.get("id") or params.get("messageId") or ""
    )
    return payload, session_id, task_id, message_id

def extract_self_evolution_set(message: dict[str, Any]) -> SelfEvolutionContext | None:
    """Detect ``AgentEvent.ClawSelfEvolutionState`` (device-reported state to persist).

    Returns None when the event is absent or the payload lacks a string
    ``selfEvolutionState`` — matching TS handleSelfEvolutionEvent's rejection of
    non-string payloads.
    """
    command = _find_agent_event(message, "ClawSelfEvolutionState")
    if command is None:
        return None
    payload, session_id, task_id, message_id = _resolve_ids(message, command)
    state = payload.get("selfEvolutionState")
    if not isinstance(state, str):
        return None
    return SelfEvolutionContext(
        state=state, session_id=session_id, task_id=task_id, message_id=message_id
    )


def extract_self_evolution_get(message: dict[str, Any]) -> SelfEvolutionContext | None:
    """Detect ``AgentEvent.ClawSelfEvolutionStateGet`` (request to sync state to device).

    ``state`` is left empty here — the persisted value is read at dispatch time.
    """
    command = _find_agent_event(message, "ClawSelfEvolutionStateGet")
    if command is None:
        return None
    _payload, session_id, task_id, message_id = _resolve_ids(message, command)
    return SelfEvolutionContext(
        state="", session_id=session_id, task_id=task_id, message_id=message_id
    )


def build_self_evolution_state_get_command(state: str) -> dict[str, Any]:
    """Build the ``Common/Action`` command — 1:1 with self-evolution-handler.ts:103-132."""
    return {
        "header": {
            "namespace": "Common",
            "name": "Action",
        },
        "payload": {
            "cardParam": {},
            "executeParam": {
                "executeMode": "background",
                "intentName": "ClawSelfEvolutionStateGet",
                "bundleName": "com.huawei.hmos.vassistant",
                "needUnlock": True,
                "actionResponse": True,
                "timeOut": 5,
                "intentParam": {
                    "selfEvolutionState": state,
                },
                "permissionId": [],
                "achieveType": "INTENT",
            },
            "responses": [
                {
                    "resultCode": "",
                    "displayText": "",
                    "ttsText": "",
                }
            ],
            "needUploadResult": True,
            "noHalfPage": False,
            "pageControlRelated": False,
        },
    }
