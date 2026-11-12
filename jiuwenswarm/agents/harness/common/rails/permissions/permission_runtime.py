# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sole swarm entry points for Manager config update and permission checks.

Business code must not call ``get_permission_manager`` for these operations —
use the helpers below.  Key assembly and one-shot overlay merge live in
``permission_key`` / ``temp_permission_config``; rail construction lives in
``build_permission_rail``.

  - ``update_permission_config`` — hot-update an existing rail (Manager key)
  - ``check_permission_by_key`` — check against a registered user-level key
  - ``check_permission_by_temp_config`` — one-shot check with ``config=``

Both check helpers delegate to core's ``check_tool_permission`` so fetch URL
expansion and result merging stay in one place.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_shield.tool_permission import (
    PermissionLevel,
    PermissionResult,
    get_permission_manager,
)
from openjiuwen.harness.security.permission_checks import check_tool_permission

__all__ = [
    "PermissionLevel",
    "PermissionResult",
    "update_permission_config",
    "check_permission_by_key",
    "check_permission_by_temp_config",
]


def update_permission_config(
    rail: Any,
    config: Mapping[str, Any],
    tool_names: Any = None,
) -> None:
    """Hot-update published permissions on an existing rail.

    This is the only place business code should refresh a rail after
    ``build_permission_rail``.  The rail publishes to the Manager key it owns.
    """
    if tool_names is None:
        rail.update_config(config)
    else:
        rail.update_config(config, tool_names)


async def check_permission_by_key(
    tool_name: str,
    tool_args: Mapping[str, Any],
    key: str,
) -> PermissionResult | None:
    """Evaluate one tool call with the config registered under ``key``.

    Returns ``None`` when ``key`` is not registered (caller should treat as
    not-allowed / skip).  Otherwise returns the Manager ``PermissionResult``.
    """
    manager = get_permission_manager()
    if not manager.has_config(key):
        return None
    return await check_tool_permission(tool_name, tool_args, key)


async def check_permission_by_temp_config(
    tool_name: str,
    tool_args: Mapping[str, Any],
    config: Mapping[str, Any],
) -> PermissionResult:
    """Evaluate one tool call with a one-shot ``config=`` (never registered)."""
    return await check_tool_permission(tool_name, tool_args, config=config)
