"""Mechanical retrieval helpers for Persist Session.

These helpers only unwrap transport envelopes and select already-authored UT
metadata.  They never summarize user intent or decide what should be stored.
"""

from __future__ import annotations

import json
from typing import Any


_USER_ENVELOPE_KEYS = frozenset(
    {"content", "origin_kind", "preferred_response_language", "source", "type"}
)


def _is_user_envelope(payload: dict[str, Any]) -> bool:
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return False
    if len(_USER_ENVELOPE_KEYS.intersection(payload)) < 3:
        return False
    if payload.get("origin_kind") == "external_user_authored":
        return True
    return str(payload.get("type") or "").strip().casefold() == "user input"


def extract_user_text(value: Any) -> str:
    """Return the user-authored text from a known message envelope.

    A plain string is preserved verbatim (apart from surrounding whitespace).
    JSON is unwrapped only when it has the structural markers emitted by the
    WorkSwarm user-prompt builder, so a user-authored JSON document is not
    accidentally reinterpreted as transport metadata.
    """
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        for key in ("query", "text", "message"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        return ""
    if isinstance(value, (list, tuple)):
        parts = [extract_user_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    payload_start = text.find("{")
    if payload_start < 0:
        return text
    try:
        payload = json.loads(text[payload_start:])
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    if _is_user_envelope(payload):
        return str(payload["content"]).strip()
    return text


def user_text_query(events: list[dict[str, Any]], *, limit: int = 4) -> str:
    """Build a bounded query from direct-user events only, newest last."""
    parts: list[str] = []
    for event in reversed(events):
        if event.get("type") not in {"user-message", "task-started"}:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            candidate = payload.get("parts") or payload.get("query") or payload
        else:
            candidate = payload
        text = extract_user_text(candidate)
        if text and text not in parts:
            parts.append(text[:1200])
        if len(parts) == limit:
            break
    return " ".join(reversed(parts))[:3600] or "recent conversation"


def constraint_candidates(matches: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    """Select active constraint-shaped search hits without semantic inference."""
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in matches:
        item_id = str(item.get("id") or "").strip()
        tags = {str(tag).strip().casefold() for tag in item.get("tags") or []}
        if not item_id or item_id in seen:
            continue
        if str(item.get("status") or "active") != "active":
            continue
        if not tags.intersection({"constraint", "user-requirement", "commitment"}):
            continue
        seen.add(item_id)
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


__all__ = ["constraint_candidates", "extract_user_text", "user_text_query"]
