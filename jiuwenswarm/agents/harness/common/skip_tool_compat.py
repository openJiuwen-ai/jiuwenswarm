# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compatibility helpers for the tool-call veto marker.

agent-core moved the one-shot veto marker off the invoke-shared
``extra["_skip_tool"]`` and onto the per-call ``AgentCallbackContext``
(``mark_tool_skipped()`` / ``tool_skipped``). Rails must go through these
helpers so the host keeps working across both core versions:

- ``mark_skipped(ctx)`` marks the call; with a new core it sets the per-call
  marker, with an older core it falls back to the legacy shared key;
- ``was_skipped(ctx)`` reads both channels (marker + legacy key). The legacy
  read is read-only and is the only way a veto raised by an un-migrated rail
  stays visible to the rest.

Why not keep writing ``extra["_skip_tool"]`` directly: that dict is shared by
every tool call of one model turn, so a sibling call can consume the marker
before the vetoed call reaches the executor (openJiuwen/agent-core#2413).
"""
from __future__ import annotations

from typing import Any

try:  # agent-core with per-call markers
    from openjiuwen.core.single_agent.rail.base import (
        mark_tool_skipped as _mark_tool_skipped,
    )
    from openjiuwen.core.single_agent.rail.base import (
        tool_call_skipped as _tool_call_skipped,
    )
except ImportError:  # older core: only the legacy extra flag exists
    _mark_tool_skipped = None  # type: ignore[assignment]
    _tool_call_skipped = None  # type: ignore[assignment]

LEGACY_SKIP_KEY = "_skip_tool"


def has_marker_support() -> bool:
    """Whether the installed agent-core exposes the per-call marker API."""
    return _mark_tool_skipped is not None


def mark_skipped(ctx: Any) -> None:
    """Veto the tool call ``ctx`` belongs to."""
    if _mark_tool_skipped is not None:
        _mark_tool_skipped(ctx)
        return
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        extra[LEGACY_SKIP_KEY] = True


def was_skipped(ctx: Any) -> bool:
    """Whether a rail vetoed ``ctx``'s tool call."""
    if _tool_call_skipped is not None:
        return _tool_call_skipped(ctx)
    extra = getattr(ctx, "extra", None)
    return bool(extra.get(LEGACY_SKIP_KEY)) if isinstance(extra, dict) else False
