"""Dotted-path helpers for structured file mediums."""

from __future__ import annotations

from typing import Any


def get_dotted(data: Any, field: str) -> Any:
    cur = data
    for part in field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_dotted(data: dict, field: str, value: Any) -> None:
    parts = field.split(".")
    cur: dict = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def delete_dotted(data: dict, field: str) -> None:
    parts = field.split(".")
    cur: dict = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return
        cur = nxt
    cur.pop(parts[-1], None)
