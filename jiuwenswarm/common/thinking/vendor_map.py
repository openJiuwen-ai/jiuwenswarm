# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Vendor match table for thinking control (no skill/role knowledge).

Allowlisted families participate in thinking toggle / depth injection.

Styles:
- ``glm53_reasoning_effort``: GLM-5.3（始终思考）— ``reasoning_effort``
  ``low|high|max`` + ``thinking.type=enabled``（禁止 disabled）。
- ``glm52_tool_stream``: GLM-5.2 — ``thinking.type`` 开关 + ``tool_stream=True``；
  default 档仅注入 ``tool_stream``（不携带 thinking，保持 default 不改思考配置）。
- ``effort_only``: Kimi K3（始终思考）— **仅**顶层 ``reasoning_effort``
  ``low|high|max``，禁止携带 ``thinking``（厂商会拒参）。
- ``extra_body_thinking_type``: GLM-5 / 5.1 / DeepSeek — ``thinking.type``
  开关；深度档可附带 ``reasoning_effort``。

``tool_stream=True``（GLM 专属，随 extra_body 注入）：GLM-5.2 / 5.3 的工具调用
参数流式输出默认 **False（服务端缓冲，生成完 arguments 才一次性下发）**，大参数
生成期零帧会撞应用层 idle 超时（issue 20261023 流式超时）；必须显式开启才会以
增量 delta 逐帧下发。DeepSeek / Kimi / Qwen 等厂商无此参数、默认即增量，禁止
跟随注入。GLM-5 / 5.1 是否支持该参数未经官方文档确认，保守不注入。

Product depth semantics (``low|medium|high``) are mapped per style.

:data:`DEFAULT_THINKING_DEPTH` is the depth used when GLM-5.3 / Kimi K3
auto-inject on ``default`` / omit. Explicit ``low|medium|high`` still map
for every allowlisted style.
"""

from __future__ import annotations

import re
from typing import Any

# Depth applied when GLM-5.3 / Kimi K3 auto-inject on ``default`` / omit.
# Future: user-configurable depth; keep a single module-level knob for now.
DEFAULT_THINKING_DEPTH = "low"

# Ordered: first match wins. GLM-5.3 must precede the broader GLM-5 pattern;
# GLM-5.2 likewise precedes it (it needs tool_stream, GLM-5 / 5.1 do not).
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
            r"(?:^|[^a-z0-9])glm[-_]?5\.2(?:$|[^0-9.])",
            re.IGNORECASE,
        ),
        "glm52_tool_stream",
    ),
    (
        re.compile(
            r"(?:^|[^a-z0-9])kimi[-_]?k3(?:$|[^0-9])",
            re.IGNORECASE,
        ),
        "effort_only",
    ),
    # GLM-5 / 5.1 only: 5.2 / 5.3 are intercepted by the earlier entries.
    (
        re.compile(
            r"(?:^|[^a-z0-9])glm[-_]?5(?:\.1)?(?:$|[^0-9.])",
            re.IGNORECASE,
        ),
        "extra_body_thinking_type",
    ),
    (
        re.compile(r"(?:^|[^a-z0-9])deepseek", re.IGNORECASE),
        "extra_body_thinking_type",
    ),
)

# Always-on thinking models: product depth → vendor effort (no medium literal).
_ALWAYS_ON_EFFORT = {
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
        effort = _ALWAYS_ON_EFFORT.get(level, "low")
        return {
            "extra_body": {
                "thinking": {"type": "enabled"},
                # GLM-5.3 默认 False：工具参数由服务端缓冲生成完才整包下发，
                # 大参数生成期零帧会撞应用层 idle 超时（20261023 流式超时根因）。
                "tool_stream": True,
            },
            "reasoning_effort": effort,
        }
    if style == "glm52_tool_stream":
        kwargs = _extra_body_thinking_kwargs(level)
        kwargs["extra_body"]["tool_stream"] = True
        return kwargs
    if style == "effort_only":
        # Kimi K3: never send thinking.* — only top-level reasoning_effort.
        effort = _ALWAYS_ON_EFFORT.get(level, "low")
        return {"reasoning_effort": effort}
    if style == "extra_body_thinking_type":
        return _extra_body_thinking_kwargs(level)
    if style == "extra_body_enable_thinking":
        # Forward-compat / tests; not matched by current allowlist.
        return {"extra_body": {"enable_thinking": level != "off"}}
    raise ValueError(f"unknown thinking vendor style: {style}")


def _extra_body_thinking_kwargs(level: str) -> dict[str, Any]:
    """Shared ``thinking.type`` switch kwargs (GLM-5.x base, no tool_stream).

    DeepSeek shares this shape; callers must NOT bolt on GLM-only params here.
    """
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


def default_tool_stream_kwargs(style: str) -> dict[str, Any] | None:
    """Kwargs injected on ``default`` for styles needing tool_stream.

    ``default`` semantics = leave vendor thinking config untouched; GLM-5.2
    still needs ``tool_stream=True`` on that tier (its large-argument buffering
    is independent of the thinking switch). Return ``None`` for non-GLM styles.
    """
    if style == "glm52_tool_stream":
        return {"extra_body": {"tool_stream": True}}
    return None


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
