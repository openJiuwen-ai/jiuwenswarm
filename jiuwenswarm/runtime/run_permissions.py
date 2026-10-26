# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped permission levels layered over the installed Host policy."""

from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from jiuwenswarm.common.permission_tools import normalize_permission_tool_name
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT


RUN_PERMISSIONS: ContextVar[dict[str, str] | None] = ContextVar(
    "jiuwenswarm_run_permissions", default=None
)


def overlay_run_permissions(config: dict[str, Any]) -> dict[str, Any]:
    """Apply per-tool choices while retaining all installed safety guards."""

    levels = RUN_PERMISSIONS.get()
    if levels is None:
        context = CURRENT_RUN_CONTEXT.get()
        if context is not None and context.tool_permissions is not None:
            # Runtime workers can predate the request's RUN_PERMISSIONS binding.
            levels = {}
            for name, level in context.tool_permissions:
                canonical = normalize_permission_tool_name(name)
                if not canonical or canonical in levels:
                    raise ValueError("Run permission tool names are ambiguous.")
                levels[canonical] = level
    if not levels:
        return config
    result = deepcopy(config)
    tools = result.get("tools")
    tools = dict(tools) if isinstance(tools, dict) else {}
    denied = set(result.get("deny_tools") or ())
    asked = set(result.get("ask_tools") or ())
    allowed = set(result.get("allow_tools") or ())
    for name, level in levels.items():
        if name in denied or tools.get(name) == "deny":
            continue
        tools[name] = level
        denied.discard(name)
        asked.discard(name)
        allowed.discard(name)
        {"deny": denied, "ask": asked, "allow": allowed}[level].add(name)
    result["tools"] = tools
    result["deny_tools"] = sorted(denied)
    result["ask_tools"] = sorted(asked)
    result["allow_tools"] = sorted(allowed)
    return result
