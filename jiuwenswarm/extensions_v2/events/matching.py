# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared match-pattern helper for host event types."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openjiuwen.core.events import MatchPattern


def match_pattern_subset(event_input: Any, match_pattern: MatchPattern) -> bool:
    """Return True when every key/value in match_pattern matches the event input."""

    if not match_pattern:
        return True
    for key, expected in match_pattern.items():
        if _lookup(event_input, key) != expected:
            return False
    return True


def _lookup(value: Any, key: str) -> Any:
    """Resolve a dotted path such as ``target.slot_id`` on mappings or objects."""

    current = value
    for part in key.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current
