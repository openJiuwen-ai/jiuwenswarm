# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure helpers for the data-plane config: extraction, filtering, and merge.

No I/O, no config access. Only explicitly managed fields may reach the merge;
dicts then recurse, while scalar values replace their local counterparts.
"""

from __future__ import annotations

import math
from typing import Any

# Top-level keys starting with ``_`` are metadata and never merged.
METADATA_PREFIX = "_"

# Managed field paths, relative to the *extracted section* (not the raw etcd
# document). That section's keys mirror config.yaml's top-level keys -- e.g. the
# section is ``{"gateway": {"agentos": ...}, "sandbox": {...}}`` -- so a
# managed path reads like config.yaml itself. kind is "float" or "positive_int".
_MANAGED_FIELDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gateway", "agentos", "sandbox_idle_timeout_seconds"), "float"),
    (("sandbox", "cpu"), "positive_int"),
    (("sandbox", "memory"), "positive_int"),
)

_MISSING = object()
_LEAF = object()


def _managed_tree() -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for path, _kind in _MANAGED_FIELDS:
        current = tree
        for key in path[:-1]:
            current = current.setdefault(key, {})
        current[path[-1]] = _LEAF
    return tree


_MANAGED_TREE = _managed_tree()


def extract_section(
    remote_doc: Any,
    component: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a remote document into (component section, metadata).

    Returns empty dicts when the document or the component key is not a
    mapping. Never raises.
    """
    if not isinstance(remote_doc, dict):
        return {}, {}

    metadata = {
        key: value
        for key, value in remote_doc.items()
        if isinstance(key, str) and key.startswith(METADATA_PREFIX)
    }

    section = remote_doc.get(component)
    if not isinstance(section, dict):
        section = {}
    return section, metadata


def filter_managed_section(section: Any) -> tuple[dict[str, Any], list[str]]:
    """Return the managed-field projection and ignored field paths.

    Unknown fields and malformed parent nodes are ignored rather than merged
    into local config. Explicit values, including ``None``, are retained so
    validation can reject invalid attempts instead of treating them as absent.
    """
    if not isinstance(section, dict):
        return {}, ["gateway"] if section is not None else []

    managed: dict[str, Any] = {}
    ignored: list[str] = []

    def _visit(
        source: dict[str, Any],
        schema: dict[str, Any],
        target: dict[str, Any],
        prefix: tuple[str, ...],
    ) -> None:
        for key, value in source.items():
            path = (*prefix, str(key))
            expected = schema.get(key, _MISSING)
            if expected is _MISSING:
                ignored.append(".".join(path))
                continue
            if expected is _LEAF:
                target[key] = value
                continue
            if not isinstance(value, dict):
                ignored.append(".".join(path))
                continue
            child: dict[str, Any] = {}
            _visit(value, expected, child, path)
            if child:
                target[key] = child

    _visit(section, _MANAGED_TREE, managed, ())
    return managed, ignored


def merge_section(local: Any, remote: Any) -> dict[str, Any]:
    """Overlay ``remote`` onto ``local`` in place and return it.

    The caller supplies a private in-memory snapshot, so mutation is local to
    one candidate effective configuration.
    """
    if not isinstance(local, dict):
        local = {}
    if not isinstance(remote, dict):
        return local

    for key, remote_value in remote.items():
        local_value = local.get(key)
        if isinstance(remote_value, dict) and isinstance(local_value, dict):
            merge_section(local_value, remote_value)
        else:
            local[key] = remote_value
    return local


def validate_section(section: Any) -> list[str]:
    """Return validation errors for the managed fields (empty = ok).

    Callers must filter unknown fields before merging. Missing managed fields
    are allowed; explicit null values are invalid.
    """
    if not isinstance(section, dict):
        return ["gateway section must be a mapping"] if section else []

    errors: list[str] = []
    for path, kind in _MANAGED_FIELDS:
        value = _get_path(section, path, _MISSING)
        if value is _MISSING:
            continue
        error = _validate_field(value, path, kind)
        if error is not None:
            errors.append(error)
    return errors


def normalize_section(section: dict[str, Any]) -> dict[str, Any]:
    """Coerce managed fields to numbers, in place. Assumes validation passed.

    Guards against numeric strings (e.g. values templated from env vars).
    """
    if not isinstance(section, dict):  # pragma: no cover - validated first
        return section
    for path, _kind in _MANAGED_FIELDS:
        value = _get_path(section, path, _MISSING)
        if value is _MISSING:
            continue
        _set_path(section, path, _coerce(value))
    return section


def _get_path(
    data: dict[str, Any],
    path: tuple[str, ...],
    default: Any = None,
) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _set_path(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = data
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _validate_field(value: Any, path: tuple[str, ...], kind: str) -> str | None:
    field = ".".join(path)
    if isinstance(value, bool):
        # bool is an int subclass; a boolean here is almost certainly a typo.
        return f"{field} must be a number, not a boolean"
    number = _as_number(value)
    if number is None:
        return f"{field} must be a number, got {value!r}"
    if not math.isfinite(number):
        return f"{field} must be finite, got {value!r}"
    if kind == "positive_int":
        if not number.is_integer():
            return f"{field} must be an integer, got {value!r}"
        if number <= 0:
            return f"{field} must be > 0, got {value!r}"
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce(value: Any) -> Any:
    number = _as_number(value)
    if number is None:  # pragma: no cover - validation runs first
        return value
    # Keep whole numbers as int so ``600`` is not rewritten as ``600.0``.
    return int(number) if number.is_integer() else number


__all__ = [
    "METADATA_PREFIX",
    "extract_section",
    "filter_managed_section",
    "merge_section",
    "normalize_section",
    "validate_section",
]
