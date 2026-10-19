# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Adapt semantic thinking (default|off|on|low|medium|high) to frozen vendor kwargs."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.thinking.types import (
    ThinkingProfile,
    freeze_llm_call_kwargs,
    normalize_thinking,
)
from jiuwenswarm.common.thinking.vendor_map import (
    DEFAULT_THINKING_DEPTH,
    depth_to_kwargs,
    match_vendor_style,
)
from jiuwenswarm.common.utils import logger

# Temporary auto-inject style: only GLM-5.3 family maps ``default`` → product depth.
# Broader default-depth layering is deferred; do not expand this set casually.
_AUTO_DEFAULT_DEPTH_STYLES = frozenset({"glm53_reasoning_effort"})


def _resolve_model_name(model: Any) -> str:
    """Best-effort model name from openjiuwen Model / config objects."""
    if model is None:
        return ""
    if isinstance(model, str):
        return model.strip()
    cfg = getattr(model, "model_config", None)
    if cfg is not None:
        for attr in ("model_name", "model"):
            val = getattr(cfg, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
    client_cfg = getattr(model, "model_client_config", None)
    if client_cfg is not None:
        val = getattr(client_cfg, "model_name", None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def resolve_effective_depth(thinking: str | None) -> tuple[str, bool]:
    """Normalize thinking; leave ``default`` as ``default``.

    Auto-inject call sites (GLM-5.3) map ``default`` →
    :data:`DEFAULT_THINKING_DEPTH` inside :func:`adapt_thinking`.

    Returns:
        (effective_depth, was_invalid)
    """
    level, invalid = normalize_thinking(thinking)
    if invalid:
        return "default", True
    return level, False


def adapt_thinking(
    thinking: str | None,
    model: Any = None,
    *,
    model_name: str = "",
) -> ThinkingProfile:
    """Build a frozen ThinkingProfile for one call / subagent lifetime.

    ``default`` / empty is a no-op for most models (leave vendor config as-is).
    Temporary exception: GLM-5.3 family (``glm53_reasoning_effort``, including
    names like ``glm-5.3-test``) auto-injects :data:`DEFAULT_THINKING_DEPTH`
    (currently ``low``). Explicit ``off|on|low|medium|high`` still inject for
    all allowlisted styles.

    Never raises: unsupported / invalid → empty kwargs + degraded flag.
    Nested kwargs are deep-copied before freeze so later inject mutations
    cannot write through into the profile.
    """
    name = (model_name or "").strip() or _resolve_model_name(model)
    try:
        level, invalid = resolve_effective_depth(thinking)
        if invalid:
            return ThinkingProfile.empty(
                thinking=level,
                degraded=True,
                reason="invalid_value",
                model_name=name,
            )

        style = match_vendor_style(name)

        if level == "default":
            if style not in _AUTO_DEFAULT_DEPTH_STYLES:
                return ThinkingProfile.empty(thinking="default", model_name=name)
            effective = DEFAULT_THINKING_DEPTH
        else:
            effective = level

        if style is None:
            logger.info(
                "[Thinking] unsupported model for thinking depth model=%r thinking=%s",
                name,
                effective,
            )
            return ThinkingProfile.empty(
                thinking=effective,
                degraded=True,
                reason="unsupported_model",
                model_name=name,
            )

        kwargs = depth_to_kwargs(style, effective)
        return ThinkingProfile(
            thinking=effective,
            llm_call_kwargs=freeze_llm_call_kwargs(kwargs),
            injected=True,
            degraded=False,
            reason=None,
            vendor_style=style,
            model_name=name,
        )
    except Exception as exc:
        logger.warning("[Thinking] adapt failed: %s", exc)
        return ThinkingProfile.empty(
            thinking="default",
            degraded=True,
            reason="adapter_error",
            model_name=name,
        )
