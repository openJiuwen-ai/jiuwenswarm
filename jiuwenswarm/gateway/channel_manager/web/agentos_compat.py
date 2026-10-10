"""Project Session modes and model keys at the frozen AgentBox Web wire boundary.

    Never recurse through arbitrary tool arguments, member modes or user content.
    The input frame and its Session/history objects remain owned by the Runtime.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from jiuwenswarm.common.mode_matrix import DEPRECATION_MAP, is_single_agent_mode, is_team_mode


def _mode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    canonical = DEPRECATION_MAP.get(value.strip().lower(), value.strip().lower())
    if is_team_mode(canonical):
        return "team"
    if is_single_agent_mode(canonical):
        return "agent"
    return value


def _record(value: Any) -> Any:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        projected = _record(decoded)
        return json.dumps(projected, ensure_ascii=False) if projected != decoded else value
    if not isinstance(value, dict) or "mode" not in value:
        return value
    projected_mode = _mode(value["mode"])
    if projected_mode == value["mode"]:
        return value
    return {**value, "mode": projected_mode}


def _models(entries: list[Any]) -> list[Any]:
    """Keep agent_os's modelSelectKey format through alias-or-name clients.

    The frozen client sends alias || model_name, discarding origin_index for
    unaliased entries. A response-only alias retains the existing global-index
    protocol for ambiguous backups; it never becomes persisted configuration.
    """
    counts = Counter(item.get("model_name") for item in entries
                     if isinstance(item, dict) and isinstance(item.get("model_name"), str))
    result = []
    for item in entries:
        if isinstance(item, dict):
            name, index = item.get("model_name"), item.get("origin_index")
            if (isinstance(name, str) and name and counts[name] > 1
                    and item.get("is_agentos") is True and not item.get("alias")
                    and type(index) is int and index >= 0):
                item = {**item, "alias": f"{name}#{index}"}
        result.append(item)
    return result


def project_agentos_web_frame(frame: Any) -> Any:
    """Copy only known Session and model-list fields in response/event frames."""
    if not isinstance(frame, dict) or frame.get("type") not in {"res", "event"}:
        return frame
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return frame
    event = str(frame.get("event") or "")
    projected = dict(payload)
    if frame.get("type") == "res" and isinstance(payload.get("models"), list):
        projected["models"] = _models(payload["models"])
    if (frame.get("type") == "res" or event.startswith(("session.", "chat."))
            or event == "plan.mode_exited"):
        if "session_id" in payload or "mode" in payload:
            projected = _record(projected)
        for key in ("session", "metadata"):
            if isinstance(payload.get(key), dict) and "session_id" in payload[key]:
                projected[key] = _record(payload[key])
        if isinstance(payload.get("sessions"), list):
            projected["sessions"] = [_record(item) for item in payload["sessions"]]
    if event == "history.message":
        for key in ("message", "content"):
            if key in payload:
                projected[key] = _record(payload[key])
    return {**frame, "payload": projected}
