# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Vendor match table for thinking control (no skill/role knowledge).

Only explicitly allowlisted model families participate in thinking toggle.
Currently: GLM-5 / GLM-5.1 / GLM-5.2 / DeepSeek (keyword match, all versions).
"""

from __future__ import annotations

import re

# Ordered: first match wins. GLM pattern is intentionally narrow to avoid
# false positives (e.g. glm-4, glm-50, glm-5.3); DeepSeek is matched by
# keyword across versions (v3.x / v4 / chat) — non-thinking variants rely
# on the executor's bare-retry fallback for rejected thinking params.
_VENDOR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:^|[^a-z0-9])glm[-_]?5(?:\.(?:1|2))?(?:$|[^0-9.])", re.IGNORECASE),
        "extra_body_thinking_type",
    ),
    (
        re.compile(r"(?:^|[^a-z0-9])deepseek", re.IGNORECASE),
        "extra_body_thinking_type",
    ),
)


def match_vendor_style(model_name: str) -> str | None:
    """Return vendor style id for model_name, or None if unsupported."""
    name = (model_name or "").strip()
    if not name:
        return None
    for pattern, style in _VENDOR_PATTERNS:
        if pattern.search(name):
            return style
    return None


def style_to_kwargs(style: str, *, enabled: bool) -> dict:
    """Map vendor style + on/off to physical llm_call_kwargs."""
    if style == "extra_body_thinking_type":
        return {
            "extra_body": {
                "thinking": {"type": "enabled" if enabled else "disabled"},
            }
        }
    if style == "extra_body_enable_thinking":
        # Kept for forward-compat / tests; not matched by current allowlist.
        return {"extra_body": {"enable_thinking": bool(enabled)}}
    raise ValueError(f"unknown thinking vendor style: {style}")
