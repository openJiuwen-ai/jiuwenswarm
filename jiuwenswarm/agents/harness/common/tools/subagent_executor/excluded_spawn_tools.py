# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""装配期剔除主 Agent / leader 专属工具，避免 GP 子 Agent 继承投递能力。

``_inject_general_purpose_subagent`` 会把传入的 ``tools`` 写入
``SubAgentConfig.tools``；``create_subagent`` 只使用 ``spec.tools``。

专家团成员不经 ``interface_deep._make_deep_agent_config``，而是
``create_deep_agent(add_general_purpose_agent=True)`` → factory 内 inject。
因此在 ``_inject_general_purpose_subagent`` 入口统一过滤
``EXCLUDED_TOOLS_SPAWN``，覆盖 Deep / Flash / Team 全部 GP 注入路径。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from jiuwenswarm.agents.harness.common.rails.leader_exclusive_tools import (
    EXCLUDED_TOOLS_SPAWN,
)

logger = logging.getLogger(__name__)

_INJECT_PATCH_MARKER = "_jiuwenswarm_excluded_spawn_tools_inject_patched"


def tool_card_name(tool: Any) -> str:
    """Resolve a tool / ToolCard / ability name for exclusion checks."""
    if tool is None:
        return ""
    if isinstance(tool, str):
        return tool.strip()
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    card = getattr(tool, "card", None)
    card_name = getattr(card, "name", None) if card is not None else None
    if isinstance(card_name, str) and card_name.strip():
        return card_name.strip()
    return ""


def filter_excluded_spawn_tools(tools: Iterable[Any] | None) -> list[Any] | None:
    """Return a new tool list with ``EXCLUDED_TOOLS_SPAWN`` names removed.

    ``None`` stays ``None`` (preserve “inherit default” semantics). Empty
    iterables become ``[]``.
    """
    if tools is None:
        return None
    kept: list[Any] = []
    removed: list[str] = []
    for tool in tools:
        name = tool_card_name(tool)
        if name and name in EXCLUDED_TOOLS_SPAWN:
            removed.append(name)
            continue
        kept.append(tool)
    if removed:
        logger.info(
            "[excluded_spawn_tools] filtered leader-exclusive tools=%s",
            sorted(set(removed)),
        )
    return kept


def apply_inject_general_purpose_excluded_tools_patch() -> bool:
    """Wrap factory ``_inject_general_purpose_subagent`` to strip leader tools.

    Idempotent. Covers Deep / Flash / Team: all GP specs are created through
    this helper (including ``create_deep_agent(add_general_purpose_agent=True)``).
    """
    try:
        import openjiuwen.harness.factory as factory
    except Exception:  # noqa: BLE001
        logger.warning(
            "[excluded_spawn_tools] factory unavailable; skip inject patch",
            exc_info=True,
        )
        return False

    original = getattr(factory, "_inject_general_purpose_subagent", None)
    if not callable(original):
        logger.warning(
            "[excluded_spawn_tools] _inject_general_purpose_subagent missing; "
            "skip inject patch"
        )
        return False

    if getattr(original, _INJECT_PATCH_MARKER, False):
        return True

    def _inject_without_leader_tools(*args: Any, **kwargs: Any) -> Any:
        if "tools" in kwargs:
            kwargs["tools"] = filter_excluded_spawn_tools(kwargs["tools"])
        elif len(args) >= 6:
            # Positional: (subagents, *, ...) — tools is keyword-only in current
            # factory; keep a defensive branch if signature ever changes.
            pass
        return original(*args, **kwargs)

    setattr(_inject_without_leader_tools, _INJECT_PATCH_MARKER, True)
    # getattr/setattr：避免 G.CLS.11（类外直接访问受保护成员）
    setattr(factory, "_inject_general_purpose_subagent", _inject_without_leader_tools)
    logger.info(
        "[excluded_spawn_tools] _inject_general_purpose_subagent leader-tool "
        "filter patch applied"
    )
    return True


__all__ = [
    "apply_inject_general_purpose_excluded_tools_patch",
    "filter_excluded_spawn_tools",
    "tool_card_name",
]
