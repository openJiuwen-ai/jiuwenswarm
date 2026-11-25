# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read structured tool calls out of the committed transcript.

The public message view drops a nested ``tool_call``. Codex file edits still
live on that row as ``tool_name`` / ``tool_args``, so the panel reads them here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def merge_observed_tools(messages: object, records: object) -> list:
    """Prefer tool calls stored on the transcript when the message view has none."""
    observed = list(messages) if isinstance(messages, list) else []
    if _has_tool_uses(observed):
        return observed
    recovered = tool_uses_from_records(records)
    if not recovered:
        return observed
    return [*observed, {"role": "assistant", "text": "", "toolUses": recovered}]


def _has_tool_uses(messages: list) -> bool:
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        uses = message.get("toolUses")
        if isinstance(uses, list) and any(isinstance(item, Mapping) for item in uses):
            return True
    return False


def tool_uses_from_records(records: object) -> list[dict]:
    """Return the newest request's structured tool calls. Free text is ignored."""
    if not isinstance(records, list):
        return []
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for record in records:
        if not isinstance(record, Mapping) or record.get("event_type") != "chat.tool_call":
            continue
        if record.get("subagent_id") or record.get("mode") == "subagent":
            continue
        calls = _calls(record)
        if not calls:
            continue
        request_id = record.get("request_id")
        key = request_id if isinstance(request_id, str) else ""
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].extend(calls)
    if not order:
        return []
    return grouped[order[-1]]


def read_tool_records(session_id: str) -> list:
    """Return committed history rows, or an empty list when the transcript cannot be read."""
    try:
        from jiuwenswarm.server.runtime.session.session_history import read_committed_main_history

        records = read_committed_main_history(session_id)
    except (OSError, ValueError, TimeoutError, TypeError):
        logger.warning("[harness-workbench] transcript unreadable: %s", session_id, exc_info=True)
        return []
    return records if isinstance(records, list) else []


def _calls(record: Mapping) -> list[dict]:
    listed = record.get("tool_calls")
    nested = record.get("tool_call")
    if isinstance(listed, list) and listed:
        sources = [item for item in listed if isinstance(item, Mapping)]
    elif isinstance(nested, Mapping):
        sources = [nested]
    else:
        sources = [record]
    found: list[dict] = []
    for source in sources:
        call = _normalize_call(source)
        if call is not None:
            found.append(call)
    return found


def _normalize_call(source: Mapping) -> dict | None:
    function = source.get("function")
    function = function if isinstance(function, Mapping) else source
    name = function.get("name") or source.get("tool_name") or source.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    if "arguments" in function:
        raw = function.get("arguments")
    elif "tool_args" in function:
        raw = function.get("tool_args")
    else:
        raw = source.get("input", source.get("tool_args"))
    arguments = _arguments(raw)
    if not isinstance(arguments, Mapping):
        return None
    identifier = (
        source.get("id") or source.get("tool_call_id") or source.get("tool_use_id") or source.get("call_id")
    )
    if not isinstance(identifier, str) or not identifier.strip():
        identifier = name.strip()
    return {"tool_use_id": identifier.strip(), "tool": name.strip(), "input": dict(arguments)}


def _arguments(raw: object) -> object:
    if not isinstance(raw, str):
        return raw
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed
    if _looks_like_patch(raw):
        return {"patch": raw}
    return {"command": raw}


def _looks_like_patch(raw: str) -> bool:
    markers = ("*** Begin Patch", "diff --git", "\n*** Add File:", "\n*** Update File:")
    return raw.startswith(markers) or any(marker in raw for marker in markers)
