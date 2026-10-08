# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enforce a root Agent tool allowlist at the final ability boundary.

Rails, packages and MCP servers can register tools after the Agent spec is
built. Filtering only the initial cards would leave those tools callable.
The per-instance boundary below filters discovery and rejects execution.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def install_tool_allowlist(manager: Any, tool_names: Iterable[str]) -> None:
    """Restrict one Agent's model-facing abilities and execution."""

    allowed = frozenset(tool_names)
    if not allowed or "*" in allowed:
        raise ValueError("an explicit tool allowlist must contain tool names")
    if getattr(manager, "jiuwenswarm_tool_allowlist", None) is not None:
        raise ValueError("tool allowlist is already installed")

    original_list = manager.list
    original_list_tool_info = manager.list_tool_info
    original_execute = manager.execute

    def limited_list() -> list[Any]:
        return [card for card in original_list() if card.name in allowed]

    async def limited_list_tool_info(*args: Any, **kwargs: Any) -> list[Any]:
        infos = await original_list_tool_info(*args, **kwargs)
        return [info for info in infos if info.name in allowed]

    async def limited_execute(ctx: Any, tool_call: Any, session: Any,
                              *args: Any, **kwargs: Any) -> Any:
        calls = tool_call if isinstance(tool_call, list) else [tool_call]
        denied = [call.name for call in calls if call.name not in allowed]
        if denied:
            raise ValueError(f"Agent tool allowlist denied: {', '.join(denied)}")
        return await original_execute(ctx, tool_call, session, *args, **kwargs)

    manager.list = limited_list
    manager.list_tool_info = limited_list_tool_info
    manager.execute = limited_execute
    manager.jiuwenswarm_tool_allowlist = allowed
