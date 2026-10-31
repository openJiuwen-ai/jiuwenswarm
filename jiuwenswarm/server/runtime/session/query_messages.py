# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project a committed transcript into Claude-shaped detached message views."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import json
from typing import Any

from jiuwenswarm.server.runtime.session.history_message import message_from_record, message_text
from jiuwenswarm.server.runtime.session.session_history import read_committed_main_history
from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError, require_session_id

MAX_SESSION_MESSAGES = 4096
_NON_MESSAGES = frozenset({
    "context.usage", "chat.usage_summary", "chat.reasoning", "chat.notice", "system.notice",
    "context.compact_boundary", "context.compact_summary", "context.rewind_summary",
    "chat.subagent_activity", "chat.subtask_update", "chat.error",
})


def _tool_use(call: Mapping[str, Any]) -> dict[str, Any] | None:
    identifier = call.get("id") or call.get("tool_call_id") or call.get("tool_use_id")
    function = call.get("function")
    function = function if isinstance(function, Mapping) else call
    name = function.get("name") or call.get("tool_name") or call.get("tool")
    arguments = function.get("arguments", call.get("input"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name:
        return None
    if not isinstance(arguments, Mapping):
        return None
    return {"tool_use_id": identifier, "tool": name, "input": copy.deepcopy(dict(arguments))}


def _block_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content
                       if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str))
    return ""


def _pair_result(calls: dict, request_id: object, result: dict[str, Any]) -> None:
    for call in calls.get((request_id, result["tool_use_id"]), []):
        call["text"] = result["text"]
        if "result" in result:
            call["result"] = copy.deepcopy(result["result"])
        if result["isError"]:
            call["isError"] = True


def read_session_messages_view(session_id: str) -> list[dict[str, Any]]:
    """Read newest 4096 user/assistant messages, returned oldest first.

    Tool-result rows become user messages with toolResults. Their outcomes are
    also paired with retained assistant toolUses by request and call identity.
    UI progress, usage, notices and child transcripts are not model messages.
    """
    require_session_id(session_id)
    try:
        records = read_committed_main_history(session_id)
    except (OSError, ValueError, TimeoutError) as error:
        raise SessionHistoryError("HISTORY_IO_FAILED") from error
    if records is None:
        return []
    messages: list[dict[str, Any]] = []
    calls: dict[tuple[object, str], list[dict[str, Any]]] = {}
    for record in records:
        if (
            record.get("event_type") in _NON_MESSAGES or record.get("subagent_id")
            or record.get("mode") == "subagent" or record.get("depth", 0) != 0
            or record.get("parent_invocation_id") or record.get("member_name") or record.get("team_id")
            or record.get("session_id") not in (None, "", session_id)
            or record.get("role") not in ("user", "human", "assistant", "tool")
        ):
            continue
        try:
            view = message_from_record(record)
        except (TypeError, ValueError) as error:
            raise SessionHistoryError("INVALID_TRANSCRIPT_MESSAGE") from error
        if view.type in ("system", "attachment"):
            continue
        request_id = record.get("request_id")
        text = message_text(view)
        if view.type == "tool":
            identifier = view.tool_call_id
            if not isinstance(identifier, str) or not identifier:
                continue
            result: dict[str, Any] = {
                "tool_use_id": identifier, "text": text,
                "isError": record.get("is_error") is True or record.get("isError") is True,
            }
            if "result" in record:
                result["result"] = copy.deepcopy(record["result"])
            messages.append({"role": "user", "text": "", "toolUses": [], "toolResults": [result]})
            _pair_result(calls, request_id, result)
            continue
        role = "assistant" if view.type == "assistant" else "user"
        message: dict[str, Any] = {"role": role, "text": text, "toolUses": []}
        if role == "assistant":
            raw_calls = [*(view.tool_calls or []), *(block for block in view.content if block.get("type") == "tool_use")]
            seen_ids = set()
            for raw_call in raw_calls:
                call = _tool_use(raw_call)
                if call is not None and call["tool_use_id"] not in seen_ids:
                    seen_ids.add(call["tool_use_id"])
                    message["toolUses"].append(call)
                    calls.setdefault((request_id, call["tool_use_id"]), []).append(call)
        else:
            for block in view.content:
                identifier = block.get("tool_use_id")
                if block.get("type") != "tool_result" or not isinstance(identifier, str) or not identifier:
                    continue
                result = {"tool_use_id": identifier, "text": _block_text(block.get("content")),
                          "isError": block.get("is_error") is True or block.get("isError") is True}
                if "result" in block:
                    result["result"] = copy.deepcopy(block["result"])
                message.setdefault("toolResults", []).append(result)
                _pair_result(calls, request_id, result)
        messages.append(message)
    return copy.deepcopy(messages[-MAX_SESSION_MESSAGES:])
