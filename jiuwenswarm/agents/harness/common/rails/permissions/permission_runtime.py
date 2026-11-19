# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sole swarm entry points for Manager config update and permission checks.

Business code must not call ``get_permission_manager`` for these operations —
use the helpers below.  Key assembly and one-shot overlay merge live in
``permission_key`` / ``temp_permission_config``; rail construction lives in
``build_permission_rail``.

  - ``update_permission_config`` — hot-update an existing rail (Manager key)
  - ``check_permission_by_key`` — check against a registered user-level key
  - ``check_permission_by_temp_config`` — one-shot check with ``config=``
  - ``install_meta_tool_expanders`` — register forwarder expansion contracts

Both check helpers delegate to core's ``check_tool_permission`` so fetch URL
expansion and result merging stay in one place.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_shield.tool_permission import (
    PermissionLevel,
    PermissionResult,
    get_permission_manager,
    register_meta_tool,
)
from openjiuwen.harness.security.permission_checks import check_tool_permission

__all__ = [
    "PermissionLevel",
    "PermissionResult",
    "update_permission_config",
    "check_permission_by_key",
    "check_permission_by_temp_config",
    "install_meta_tool_expanders",
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


def install_meta_tool_expanders() -> None:
    """Register jiuwenswarm's forwarder expansion contracts with agent_shield.

    invoke_tool 把目标调用包在 ``{"tool_name": T, "arguments": A}`` 里。权限
    引擎只看外层时对内层命令失明：外层名不在 ``tools`` 配置走 ``defaults``
    放行，转发即可绕过命令级判定（issue #6499 的 PowerShell 绕过路径）。
    这里把展开契约注册进 agent_shield——引擎对注册过的转发器按内层
    ``(T, A)`` 递归判定并与外层合并取最严。

    rail 装配期（``build_permission_rail``）调用；同名重复注册原地替换，
    幂等。
    """

    def _extract_invoke_tool(
        tool_args: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, Any]] | None:
        inner_name = tool_args.get("tool_name")
        inner_args = tool_args.get("arguments")
        if not isinstance(inner_name, str) or not inner_name.strip():
            return None
        if not isinstance(inner_args, Mapping):
            return None
        # 与非空判据同一口径：返回 strip 后的名字。空白填充的内层名若原样
        # 透传，引擎按精确键匹配 tools 配置（tiered._tools.get）必然失配，
        # 落回 defaults 放行——判据与返回值不一致会重新漏开一条旁路。
        return inner_name.strip(), inner_args

    register_meta_tool("invoke_tool", _extract_invoke_tool)
