# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Translate native messages and product history without losing protected data."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import replace
import json
from typing import Any

from jiuwenswarm.extensions_v2.event_types.session_history import SessionAppendMessage, SessionAppendOrigin
from jiuwenswarm.server.runtime.session.session_history import PreparedHistoryRecord, prepare_history_record

MAX_MESSAGE_BYTES = 1024 * 1024
MESSAGE_TYPES = ("user", "assistant", "tool", "attachment", "system")


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _same_json(left: object, right: object) -> bool:
    """Compare protected values without Python bool/int equality coercion."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def content_blocks(value: object) -> list[dict[str, Any]]:
    """Keep native text/list content as detached JSON blocks, never str(list)."""
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        raise ValueError("Message content must be text or an array of blocks")
    blocks = []
    for block in value:
        if isinstance(block, str):
            blocks.append({"type": "text", "text": block})
        elif isinstance(block, Mapping) and isinstance(block.get("type"), str):
            item = copy.deepcopy(dict(block))
            if item["type"] == "text" and not isinstance(item.get("text"), str):
                raise ValueError("Text blocks require text")
            _json_size(item)
            blocks.append(item)
        else:
            raise ValueError("Invalid content block")
    return blocks


def message_text(message: SessionAppendMessage) -> str:
    """Return the legacy text rendering without serializing media as text."""
    return "".join(block["text"] for block in message.content if block.get("type") == "text")


def message_from_base_message(message: Any) -> SessionAppendMessage:
    """Adapt a native BaseMessage; private metadata stays with the platform.

    This converter does not install another persistence or model-input hook.
    """
    from openjiuwen.core.foundation.llm import BaseMessage

    if not isinstance(message, BaseMessage):
        raise ValueError("A native BaseMessage is required")
    role = message.role
    if role not in ("user", "assistant", "tool", "system"):
        raise ValueError("Unsupported native message role")
    calls = getattr(message, "tool_calls", None)
    return SessionAppendMessage(
        type=role, content=content_blocks(message.content), role=role, name=message.name,
        tool_call_id=getattr(message, "tool_call_id", None),
        tool_calls=[call.model_dump(mode="json") if hasattr(call, "model_dump") else copy.deepcopy(call) for call in calls] if calls is not None else None,
    )


def prepare_platform_history(record_kwargs: Mapping[str, Any]) -> tuple[PreparedHistoryRecord | None, SessionAppendMessage | None]:
    """Accept native BaseMessage content at the existing platform write seam."""
    from openjiuwen.core.foundation.llm import BaseMessage

    native = record_kwargs.get("content")
    if not isinstance(native, BaseMessage):
        return prepare_history_record(**record_kwargs), None
    view = message_from_base_message(native)
    kwargs = dict(record_kwargs)
    extra = copy.deepcopy(kwargs.get("extra") or {})
    extra["native_message"] = native.model_dump(mode="json")
    if view.type == "tool":
        kwargs["event_type"] = "chat.tool_result"
        extra.update({"tool_call_id": view.tool_call_id, "name": view.name, "result": copy.deepcopy(native.content),
                      "rendered_result": message_text(view)})
    elif view.type == "assistant" and view.tool_calls:
        kwargs["event_type"] = "chat.tool_call"
        extra["tool_calls"] = [{**copy.deepcopy(call), "tool_call_id": call.get("id")} for call in view.tool_calls]
    elif view.type == "system":
        kwargs["event_type"] = "system.notice"
    elif view.type == "assistant":
        if not kwargs.get("event_type"):
            kwargs["event_type"] = "chat.final"
    kwargs.update({"role": "user" if view.type == "user" else "assistant", "content": message_text(view), "extra": extra})
    return prepare_history_record(**kwargs), view


def message_from_dict(value: object) -> SessionAppendMessage:
    """Parse the public JSON message schema with explicit field validation."""
    if not isinstance(value, Mapping) or not {"type", "content"} <= set(value):
        raise ValueError("A message requires type and content")
    fields = {"type", "content", "role", "name", "is_meta", "tool_call_id", "tool_calls"}
    if set(value) - fields or value["type"] not in MESSAGE_TYPES:
        raise ValueError("Unsupported message fields or type")
    kind = value["type"]
    role = value.get("role", kind if kind != "attachment" else None)
    if role not in (None, "user", "assistant", "tool", "system"):
        raise ValueError("Unsupported message role")
    name, tool_id, calls = value.get("name"), value.get("tool_call_id"), value.get("tool_calls")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 256):
        raise ValueError("Invalid message name")
    if tool_id is not None and (not isinstance(tool_id, str) or not tool_id):
        raise ValueError("Invalid tool call id")
    if calls is not None and (not isinstance(calls, list) or any(not isinstance(x, dict) for x in calls)):
        raise ValueError("Invalid tool calls")
    if not isinstance(value.get("is_meta", False), bool):
        raise ValueError("Invalid is_meta")
    result = SessionAppendMessage(kind, content_blocks(value["content"]), role, name, value.get("is_meta", False), tool_id, copy.deepcopy(calls))
    _json_size(result.to_dict())
    return result


def message_from_record(record: Mapping[str, Any], *, prefer_stored: bool = True) -> SessionAppendMessage:
    """Project a product row, retaining a committed structured view if present."""
    if prefer_stored and isinstance(record.get("transcript_message"), Mapping):
        return message_from_dict(record["transcript_message"])
    event_type = record.get("event_type", "")
    kind = "assistant" if record.get("role") == "assistant" else "user"
    name = None
    content = record.get("content_blocks", record.get("content", ""))
    calls, tool_id = None, None
    if event_type == "chat.tool_result":
        kind, name, tool_id = "tool", record.get("tool_name") or record.get("name"), record.get("tool_call_id")
        rendered = record.get("rendered_result")
        if rendered is None:
            rendered = record.get("result", content)
        if isinstance(rendered, list):
            try:
                content = content_blocks(rendered)
            except (TypeError, ValueError):
                content = json.dumps(rendered, ensure_ascii=False)
        else:
            content = rendered if isinstance(rendered, str) else json.dumps(rendered, ensure_ascii=False)
    elif event_type == "chat.tool_call":
        calls = copy.deepcopy(record.get("tool_calls"))
        if calls is None:
            calls = [{key: copy.deepcopy(record[key]) for key in ("tool_call_id", "tool_name", "name", "arguments") if key in record}]
    elif event_type in ("context.compact_boundary", "chat.notice", "system.notice"):
        kind, name = "system", event_type
    elif event_type in ("chat.attachment", "attachment"):
        kind, name = "attachment", event_type
    return SessionAppendMessage(
        kind, content_blocks(content), None if kind == "attachment" else kind, name,
        bool(record.get("transcript_only")), tool_id, calls,
    )


def classify_record(record: Mapping[str, Any], source: str, model_name: str | None = None) -> tuple[str, SessionAppendOrigin]:
    """Separate the row's semantic door from producer and adapter entrance."""
    if source == "extension":
        return "note", SessionAppendOrigin("extension", record.get("extension_package_id"), "ops.session.append")
    event_type = record.get("event_type", "")
    if event_type == "chat.tool_result":
        door, kind, name = "tool-result", "tool", record.get("tool_name") or record.get("name")
    elif event_type == "chat.tool_call":
        door, kind, name = "tool-call", "model", model_name
    elif event_type in ("chat.attachment", "attachment"):
        door, kind, name = "attachment", "platform", None
    elif event_type in ("context.compact_boundary", "chat.notice", "system.notice"):
        door, kind, name = "notice", "platform", None
    elif record.get("role") == "user":
        door, kind, name = "prompt", "user", None
    else:
        door, kind, name = "response", "model", model_name
    if source == "history.append_record":
        kind, name = "platform", "history.append_record"
    return door, SessionAppendOrigin(kind, name, source)


def valid_message_rewrite(original: SessionAppendMessage, value: object) -> bool:
    """Allow text edits while pinning every other field and block position."""
    if not isinstance(value, SessionAppendMessage):
        return False
    try:
        old, new = original.to_dict(), value.to_dict()
        old_content, new_content = old.pop("content"), new.pop("content")
        if not _same_json(old, new) or not isinstance(new_content, list) or len(old_content) != len(new_content):
            return False
        for before, after in zip(old_content, new_content):
            if not isinstance(after, dict):
                return False
            if before.get("type") == "text":
                if not isinstance(after.get("text"), str):
                    return False
                if not _same_json({k: v for k, v in before.items() if k != "text"}, {k: v for k, v in after.items() if k != "text"}):
                    return False
            elif not _same_json(before, after):
                return False
        if message_text(original).strip() and not message_text(value).strip():
            return False
        return _json_size(value.to_dict()) <= max(MAX_MESSAGE_BYTES, _json_size(original.to_dict()))
    except (TypeError, ValueError, UnicodeError):
        return False


def apply_message(prepared: PreparedHistoryRecord, message: SessionAppendMessage, original: SessionAppendMessage) -> PreparedHistoryRecord:
    """Merge allowed content into the original row, preserving private fields."""
    if message == original:
        return replace(prepared, record={**copy.deepcopy(prepared.record), "transcript_message": message.to_dict()})
    content = message_text(message)
    record = {**copy.deepcopy(prepared.record), "content": content, "transcript_message": message.to_dict()}
    extra = copy.deepcopy(prepared.metadata.get("extra"))
    if prepared.record.get("event_type") == "chat.tool_result":
        record["rendered_result"] = content
        if isinstance(extra, dict):
            extra["rendered_result"] = content
    if "content_blocks" in prepared.record:
        record["content_blocks"] = copy.deepcopy(message.content)
    if isinstance(record.get("native_message"), dict):
        native = record["native_message"]
        native["content"] = content if isinstance(native.get("content"), str) else copy.deepcopy(message.content)
    return replace(prepared, record=record, metadata={**prepared.metadata, "content": content, "extra": extra})
