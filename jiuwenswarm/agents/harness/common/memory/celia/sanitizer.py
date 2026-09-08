"""Conversation and prompt sanitization for Celia memory."""

from __future__ import annotations

import json
import re
from typing import Any

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_MARKER_RE = re.compile(r"<\|[^>]{0,200}\|>")


def sanitize_prompt_text(value: Any) -> str:
    text = str(value or "")
    text = _MARKER_RE.sub("", text)
    text = _CONTROL_RE.sub(" ", text)
    return " ".join(text.split()).strip()


def _forbidden(value: str) -> bool:
    try:
        from ..forbidden import _get_memory_forbidden_config

        config = _get_memory_forbidden_config()
        if not config.get("enabled"):
            return False
        for pattern in config.get("patterns") or []:
            try:
                if re.search(str(pattern), value, flags=re.IGNORECASE):
                    return True
            except re.error:
                continue
    except Exception:
        return False
    return False


def sanitize_memory_text(value: Any) -> str | None:
    text = sanitize_prompt_text(value)
    if not text or _forbidden(text):
        return None
    return text


def _clean_tool_call(value: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(value, list):
        calls = [_clean_tool_call(item) for item in value]
        return [item for item in calls if isinstance(item, dict)]
    if not isinstance(value, dict):
        return None
    result = dict(value)
    for key in ("id", "tool_id", "toolCallId", "tool_call_id", "thinkingSignature", "thinking_signature"):
        result.pop(key, None)
    args = result.get("arguments")
    if isinstance(args, dict):
        clean_args: dict[str, Any] = {}
        for key, item in args.items():
            if isinstance(item, str) and len(item) > 1024:
                clean_args[str(key)] = item[:200] + f"...<truncated length={len(item)}>"
            else:
                clean_args[str(key)] = item
        result["arguments"] = clean_args
    return result


def clean_turn_events(
    events: list[dict[str, Any]], *, text_limit: int | None = 4000,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    memory_tools = {
        "memory_open",
        "memory_add",
        "memory_store",
        "memory_global_load",
        "memory_scene_search",
        "memory_backup",
        "memory_restore",
        "memory_update_config",
        "memory_forget",
        "memory_scene_load",
        "memory_record_search",
        "memory_chat_history_search",
        "memory_scene_list_load",
        "memory_get_global_summary",
        "memory_flush",
        "memory_list",
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        role = str(event.get("role") or "")
        if role == "system" or role not in {"user", "assistant", "tool", "tool_call"}:
            continue
        if event.get("startup") or str(event.get("event_type") or "").lower() in {"startup", "session_startup"}:
            continue
        tool_name = str(event.get("name") or event.get("tool_name") or "")
        if role == "tool" and (tool_name in memory_tools or event.get("success", True)):
            continue
        item: dict[str, Any] = {"role": role}
        text = event.get("text", event.get("content"))
        if role == "user" and isinstance(text, str) and text.strip().lower() in {"/new", "/reset"}:
            continue
        if isinstance(text, str):
            clean = sanitize_memory_text(text)
            if clean:
                item["text"] = clean[:text_limit] if text_limit is not None else clean
        if role in {"assistant", "tool_call"}:
            if event.get("thinking"):
                clean_thinking = sanitize_memory_text(event["thinking"])
                if clean_thinking:
                    item["thinking"] = clean_thinking[:4000]
            tool_call = _clean_tool_call(event.get("toolCall") or event.get("tool_call"))
            if tool_call:
                item["toolCall"] = tool_call
        if role == "tool" and not event.get("success", True):
            if "text" in item:
                item["text"] = item["text"][:300]
        if len(item) > 1:
            cleaned.append(item)
    return cleaned


def serialize_turn(events: list[dict[str, Any]]) -> str:
    return json.dumps(clean_turn_events(events), ensure_ascii=False, separators=(",", ":"))
