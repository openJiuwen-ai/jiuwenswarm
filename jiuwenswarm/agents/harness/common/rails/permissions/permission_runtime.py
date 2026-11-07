# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sole swarm entry points for Manager config update and permission checks.

Business code must not call ``get_permission_manager`` for these operations —
use the helpers below.  Key assembly and one-shot overlay merge live in
``permission_key`` / ``temp_permission_config``; rail construction lives in
``build_permission_rail``.

  - ``update_permission_config`` — hot-update an existing rail (Manager key)
  - ``check_permission_by_key`` — check against a registered user-level key
  - ``check_permission_by_temp_config`` — one-shot check with ``config=``
"""

from __future__ import annotations

from typing import Any, Mapping

from agent_shield.tool_permission import (
    PermissionLevel,
    PermissionResult,
    get_permission_manager,
)

__all__ = [
    "PermissionLevel",
    "PermissionResult",
    "update_permission_config",
    "check_permission_by_key",
    "check_permission_by_temp_config",
]

# Host fetch tools take a URL list; Manager contract is one scalar URL per check.
_FETCH_TOOLS = frozenset({"mcp_fetch_webpage", "fetch_webpage", "web_fetch_webpage"})
_RANK = {
    PermissionLevel.ALLOW: 0,
    PermissionLevel.UNDETERMINED: 1,
    PermissionLevel.ASK: 2,
    PermissionLevel.DENY: 3,
}


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


def _merge_permission_results(results: list[PermissionResult]) -> PermissionResult:
    strongest = max(results, key=lambda result: _RANK[result.permission])
    return PermissionResult(
        strongest.permission,
        strongest.matched_rule,
        strongest.reason,
        warnings=sorted({w for r in results for w in r.warnings or []}),
        external_paths=sorted({p for r in results for p in r.external_paths or []}),
        ask_accesses=sorted({a for r in results for a in (r.ask_accesses or [])}),
    )


async def _check_with_fetch_expand(
    tool_name: str,
    tool_args: Mapping[str, Any],
    *,
    key: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> PermissionResult:
    manager = get_permission_manager()
    if tool_name in _FETCH_TOOLS:
        urls = tool_args.get("url")
        if isinstance(urls, list):
            if not urls or any(
                not isinstance(url, str) or not url.strip() for url in urls
            ):
                return PermissionResult(
                    PermissionLevel.DENY,
                    "host:invalid_urls",
                    "Invalid fetch URL list",
                )
            results = [
                await manager.check_permission(
                    tool_name,
                    {**tool_args, "url": url},
                    key,
                    config=config,
                )
                for url in urls
            ]
            return _merge_permission_results(results)
    return await manager.check_permission(
        tool_name, tool_args, key, config=config
    )


async def check_permission_by_key(
    tool_name: str,
    tool_args: Mapping[str, Any],
    key: str,
) -> PermissionResult | None:
    """Evaluate one tool call with the config registered under ``key``.

    Returns ``None`` when ``key`` is not registered (caller should treat as
    not-allowed / skip).  Otherwise returns the Manager ``PermissionResult``.

    Fetch URL lists go through the same ``_check_with_fetch_expand`` path as
    ``check_permission_by_temp_config`` (scalar URL contract).
    """
    manager = get_permission_manager()
    if not manager.has_config(key):
        return None
    return await _check_with_fetch_expand(tool_name, tool_args, key=key)


async def check_permission_by_temp_config(
    tool_name: str,
    tool_args: Mapping[str, Any],
    config: Mapping[str, Any],
) -> PermissionResult:
    """Evaluate one tool call with a one-shot ``config=`` (never registered).

    Shares fetch URL expansion with ``check_permission_by_key``.
    """
    return await _check_with_fetch_expand(
        tool_name, tool_args, config=config
    )
