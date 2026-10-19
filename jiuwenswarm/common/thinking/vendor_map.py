# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Vendor match table for thinking control (no skill/role knowledge).

Allowlisted families participate in thinking toggle / depth injection.

Styles:
- ``glm53_reasoning_effort``: GLM-5.3（始终思考）— 只用 ``reasoning_effort``
  ``low|high|max``，禁止 ``thinking.type=disabled``。
- ``extra_body_thinking_type``: GLM-5 / 5.1 / 5.2 / DeepSeek — ``thinking.type``
  开关；深度档可附带 ``reasoning_effort``。

Product depth semantics (``low|medium|high``) are mapped per style.

:data:`DEFAULT_THINKING_DEPTH` is the depth used when GLM-5.3 family auto-
injects on ``default`` (temporary; broader layered defaults TBD). Explicit
``low|medium|high`` still map for every allowlisted style.
"""

from __future__ import annotations

import re
from typing import Any

# Depth applied when GLM-5.3 auto-injects on ``default`` / omit.
# Future: layered config; keep a single module-level knob for now.
DEFAULT_THINKING_DEPTH = "low"

# Ordered: first match wins. GLM-5.3 must precede the broader GLM-5 pattern.
_VENDOR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:^|[^a-z0-9])glm[-_]?5\.3(?:$|[^0-9.])",
            re.IGNORECASE,
        ),
        "glm53_reasoning_effort",
    ),
    (
        re.compile(
            r"(?:^|[^a-z0-9])glm[-_]?5(?:\.(?:1|2))?(?:$|[^0-9.])",
            re.IGNORECASE,
        ),
        "extra_body_thinking_type",
    ),
    (
        re.compile(r"(?:^|[^a-z0-9])deepseek", re.IGNORECASE),
        "extra_body_thinking_type",
    ),
)

# GLM-5.3 vendor effort levels (no medium literal).
_GLM53_EFFORT = {
    "off": "low",  # cannot disable — fall back to lightest effort
    "on": "high",
    "low": "low",
    "medium": "high",
    "high": "max",
}


def match_vendor_style(model_name: str) -> str | None:
    """Return vendor style id for model_name, or None if unsupported."""
    name = (model_name or "").strip()
    if not name:
        return None
    for pattern, style in _VENDOR_PATTERNS:
        if pattern.search(name):
            return style
    return None


def depth_to_kwargs(style: str, depth: str) -> dict[str, Any]:
    """Map vendor style + product depth to physical ``llm_call_kwargs``."""
    level = (depth or DEFAULT_THINKING_DEPTH).strip().lower()
    if style == "glm53_reasoning_effort":
        effort = _GLM53_EFFORT.get(level, "low")
        return {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": effort,
        }
    if style == "extra_body_thinking_type":
        if level == "off":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        kwargs: dict[str, Any] = {
            "extra_body": {"thinking": {"type": "enabled"}},
        }
        if level == "low":
            kwargs["reasoning_effort"] = "low"
        elif level == "high":
            kwargs["reasoning_effort"] = "high"
        # medium / on → enabled without effort (vendor default depth while on)
        return kwargs
    if style == "extra_body_enable_thinking":
        # Forward-compat / tests; not matched by current allowlist.
        return {"extra_body": {"enable_thinking": level != "off"}}
    raise ValueError(f"unknown thinking vendor style: {style}")


def style_to_kwargs(style: str, *, enabled: bool) -> dict[str, Any]:
    """Map vendor style + on/off to physical llm_call_kwargs (legacy)."""
    return depth_to_kwargs(style, "on" if enabled else "off")


def llm_call_kwargs_to_extension(kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """Convert ``llm_call_kwargs`` into DeepResearch / Model ``extension`` shape."""
    raw = dict(kwargs or {})
    extension: dict[str, Any] = {}
    extra_body = raw.get("extra_body")
    if isinstance(extra_body, dict) and extra_body:
        extension["extra_body"] = dict(extra_body)
    if "reasoning_effort" in raw:
        extension["reasoning_effort"] = raw["reasoning_effort"]
    return extension
