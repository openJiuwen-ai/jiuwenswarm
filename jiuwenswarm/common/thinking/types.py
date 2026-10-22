# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Types for subagent thinking control (semantic tiers)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from jiuwenswarm.common.utils import logger

# Public contract for spawn/fork / depth parameter ``thinking``.
# ``default`` is usually a no-op; GLM-5.3 family maps it to
# :data:`DEFAULT_THINKING_DEPTH` at adapt time (temporary scope).
# Depth tiers ``low|medium|high`` are product semantics (vendor-mapped).
# Legacy toggle ``off|on`` remains for subagent / DisableThinkingMixin.
THINKING_VALUES = frozenset({"", "default", "off", "on", "low", "medium", "high"})


def _deep_freeze(obj: Any) -> Any:
    """Recursively freeze mappings/lists so nested values are immutable."""
    if isinstance(obj, Mapping):
        return MappingProxyType({k: _deep_freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_deep_freeze(v) for v in obj)
    if isinstance(obj, tuple):
        return tuple(_deep_freeze(v) for v in obj)
    return obj


def freeze_llm_call_kwargs(kwargs: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Deep-copy then recursively freeze so nested dicts cannot be mutated."""
    return _deep_freeze(deepcopy(dict(kwargs or {})))


def thaw_llm_call_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep mutable copy of frozen kwargs (safe for per-call inject)."""

    def _thaw(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            return {k: _thaw(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_thaw(v) for v in obj]
        return obj

    return _thaw(kwargs or {})


@dataclass(frozen=True)
class ThinkingProfile:
    """Frozen per-subagent thinking injection profile."""

    thinking: str
    llm_call_kwargs: Mapping[str, Any]
    injected: bool
    degraded: bool
    reason: str | None = None
    vendor_style: str | None = None
    model_name: str = ""

    @staticmethod
    def empty(
        *,
        thinking: str = "default",
        degraded: bool = False,
        reason: str | None = None,
        vendor_style: str | None = None,
        model_name: str = "",
    ) -> ThinkingProfile:
        return ThinkingProfile(
            thinking=thinking,
            llm_call_kwargs=freeze_llm_call_kwargs({}),
            injected=False,
            degraded=degraded,
            reason=reason,
            vendor_style=vendor_style,
            model_name=model_name,
        )


def normalize_thinking(raw: Any) -> tuple[str, bool]:
    """Normalize tool input to default|off|on|low|medium|high.

    Returns:
        (normalized_value, was_invalid)
        Invalid / unknown values become ``default`` with was_invalid=True.
    """
    if raw is None:
        return "default", False
    if not isinstance(raw, str):
        logger.warning(
            "[Thinking] invalid thinking type=%s; treat as default",
            type(raw).__name__,
        )
        return "default", True
    value = raw.strip().lower()
    if value in ("", "default"):
        return "default", False
    if value in ("off", "on", "low", "medium", "high"):
        return value, False
    logger.warning(
        "[Thinking] invalid thinking=%r; treat as default",
        raw,
    )
    return "default", True


def kwargs_digest(kwargs: Mapping[str, Any] | None) -> str:
    """Compact, log-safe summary of injected kwargs (no secrets expected)."""
    if not kwargs:
        return "{}"
    try:
        extra_body = kwargs.get("extra_body") if hasattr(kwargs, "get") else None
        if isinstance(extra_body, dict):
            thinking = extra_body.get("thinking")
            if isinstance(thinking, dict) and "type" in thinking:
                return f"extra_body.thinking.type={thinking.get('type')!r}"
            if "enable_thinking" in extra_body:
                return f"extra_body.enable_thinking={extra_body.get('enable_thinking')!r}"
            if "reasoning_effort" in extra_body:
                return f"extra_body.reasoning_effort={extra_body.get('reasoning_effort')!r}"
        if "reasoning_effort" in kwargs:
            return f"reasoning_effort={kwargs.get('reasoning_effort')!r}"
        return f"keys={sorted(kwargs.keys())}"
    except Exception:
        return "<unrepr>"
