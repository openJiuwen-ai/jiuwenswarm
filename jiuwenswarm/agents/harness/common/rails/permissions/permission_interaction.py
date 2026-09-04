# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Minimal permission-interaction presentation facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PERMISSION_RUNTIME_QUARANTINED_KEY = (
    "_jiuwenswarm_permission_runtime_quarantined_v1"
)


def contains_permission_interaction(value: Any) -> bool:
    """Return whether a bounded nested value carries a Host permission locator."""

    pending = [value]
    seen: set[int] = set()
    for _ in range(128):
        if not pending:
            return False
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, Mapping):
            metadata = item.get("metadata")
            if isinstance(metadata, Mapping) and (
                "tool_invocation_key" in metadata
                or metadata.get("manual_approval_supported") is True
            ):
                return True
            if "tool_invocation_key" in item:
                return True
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        else:
            metadata = getattr(item, "metadata", None)
            if isinstance(metadata, Mapping) and (
                "tool_invocation_key" in metadata
                or metadata.get("manual_approval_supported") is True
            ):
                return True
            for name in ("payload", "value", "state"):
                nested = getattr(item, name, None)
                if nested is not None:
                    pending.append(nested)
    return bool(pending)


__all__ = ["PERMISSION_RUNTIME_QUARANTINED_KEY", "contains_permission_interaction"]
