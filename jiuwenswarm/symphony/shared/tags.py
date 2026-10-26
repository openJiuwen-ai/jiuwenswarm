from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from typing import Any


def normalize_tags(*values: Any) -> tuple[str, ...]:
    """Normalize candidate tags for stable indexing and case-insensitive matching."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _iter_tag_values(value):
            tag = str(item if item is not None else "").strip().casefold()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
    return tuple(normalized)


def _iter_tag_values(value: Any, *, _seen: set[int] | None = None) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _iter_tag_values(bytes(value).decode("utf-8", errors="replace"), _seen=_seen)
    if isinstance(value, str):
        return _parse_tag_text(value)
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Iterable):
        return _flatten_tag_values(value, seen=_seen)
    return (str(value),)


def _parse_tag_text(value: str) -> tuple[str, ...]:
    text = value.strip()
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not text:
        return ()
    if "," in text:
        return tuple(part.strip().strip("\"'") for part in text.split(","))
    return (text.strip("\"'"),)


def _flatten_tag_values(values: Iterable[Any], *, seen: set[int] | None) -> tuple[str, ...]:
    active_containers = seen if seen is not None else set()
    container_id = id(values)
    if container_id in active_containers:
        return ()

    active_containers.add(container_id)
    flattened: list[str] = []
    try:
        items = sorted(values, key=repr) if isinstance(values, Set) else values
        for item in items:
            flattened.extend(_iter_tag_values(item, _seen=active_containers))
        return tuple(flattened)
    finally:
        active_containers.remove(container_id)


__all__ = ["normalize_tags"]
