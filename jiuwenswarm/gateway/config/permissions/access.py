# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permissions_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.permissions.repository import PermissionsConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: PermissionsConfigRepository | None = None


def set_permissions_config_repository(
    repo: PermissionsConfigRepository | None,
) -> None:
    global _repo
    _repo = repo


def get_permissions_config_repository() -> PermissionsConfigRepository | None:
    return _repo


def clear_permissions_config_repository() -> None:
    set_permissions_config_repository(None)


def schedule_permissions_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def update_permissions_enabled_in_config(value: bool) -> None:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_enabled_in_config as _legacy,
        )

        _legacy(value)
        return
    await repo.set_enabled(value)


async def update_permissions_file_guard_workspace_rw_enabled_in_config(
    value: bool,
) -> None:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_file_guard_workspace_rw_enabled_in_config as _legacy,
        )

        _legacy(value)
        return
    await repo.set_file_guard_workspace_rw_enabled(value)


async def update_permissions_owner_scopes_in_config(
    owner_scopes: Any,
    deny_guidance_message: str | None = None,
) -> None:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_owner_scopes_in_config as _legacy,
        )

        _legacy(owner_scopes, deny_guidance_message=deny_guidance_message)
        return
    await repo.set_owner_scopes(
        owner_scopes, deny_guidance_message=deny_guidance_message
    )


async def update_permissions_deny_guidance_in_config(msg: str) -> None:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_deny_guidance_in_config as _legacy,
        )

        _legacy(msg)
        return
    await repo.set_deny_guidance(msg)


async def replace_permissions_tools_in_config(tools: Any) -> None:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            replace_permissions_tools_in_config as _legacy,
        )

        _legacy(tools)
        return
    await repo.replace_tools(tools)


async def update_permissions_tool_in_config(
    tool_name: str, level: Any
) -> dict[str, Any]:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_tool_in_config as _legacy,
        )

        return _legacy(tool_name, level)
    return await repo.update_tool(tool_name, level)


async def delete_permissions_tool_in_config(tool_name: str) -> bool:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            delete_permissions_tool_in_config as _legacy,
        )

        return _legacy(tool_name)
    return await repo.delete_tool(tool_name)


async def create_permissions_rule_in_config(rule: dict[str, Any]) -> dict[str, Any]:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            create_permissions_rule_in_config as _legacy,
        )

        return _legacy(rule)
    return await repo.create_rule(rule)


async def update_permissions_rule_in_config(
    rule_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_permissions_rule_in_config as _legacy,
        )

        return _legacy(rule_id, patch)
    return await repo.update_rule(rule_id, patch)


async def delete_permissions_rule_in_config(rule_id: str) -> bool:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            delete_permissions_rule_in_config as _legacy,
        )

        return _legacy(rule_id)
    return await repo.delete_rule(rule_id)


async def delete_permissions_approval_override_in_config(override_id: str) -> bool:
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            delete_permissions_approval_override_in_config as _legacy,
        )

        return _legacy(override_id)
    return await repo.delete_approval_override(override_id)


async def get_permissions_body_in_config() -> dict[str, Any]:
    """读取全局 ``permissions`` body（剥掉保留键 ``agents``）。"""
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        strip_permissions_agents,
    )

    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("permissions")
        return strip_permissions_agents(raw if isinstance(raw, dict) else {})
    body = await repo.get_body()
    return strip_permissions_agents(body if isinstance(body, dict) else {})


async def replace_permissions_in_config(body: dict[str, Any]) -> None:
    """整段替换 ``permissions`` 全局字段，保留已有 ``agents`` 表。"""
    if not isinstance(body, dict):
        raise ValueError("permissions body must be an object")
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        PERMISSIONS_AGENTS_KEY,
        strip_permissions_agents,
    )
    from jiuwenswarm.edition import is_enterprise

    incoming = strip_permissions_agents(body)
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            existing = data.get("permissions")
            agents_table = None
            if (
                not is_enterprise()
                and isinstance(existing, dict)
                and isinstance(existing.get(PERMISSIONS_AGENTS_KEY), dict)
            ):
                agents_table = existing[PERMISSIONS_AGENTS_KEY]
            stored = dict(incoming)
            if agents_table is not None:
                stored[PERMISSIONS_AGENTS_KEY] = agents_table
            data["permissions"] = stored
            return data

        update_config(_mutate)
        return

    if not is_enterprise():
        existing = await repo.get_body()
        if isinstance(existing, dict) and isinstance(existing.get(PERMISSIONS_AGENTS_KEY), dict):
            incoming[PERMISSIONS_AGENTS_KEY] = existing[PERMISSIONS_AGENTS_KEY]
    await repo.replace(incoming)


async def delete_permissions_in_config() -> bool:
    """删除 ``permissions`` 段 / 企业行（EE Manager delete 语义）。"""
    repo = get_permissions_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        found = {"value": False}

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            if "permissions" in data:
                del data["permissions"]
                found["value"] = True
            return data

        update_config(_mutate)
        return bool(found["value"])
    return await repo.delete()


__all__ = [
    "clear_permissions_config_repository",
    "create_permissions_rule_in_config",
    "delete_permissions_approval_override_in_config",
    "delete_permissions_in_config",
    "delete_permissions_rule_in_config",
    "delete_permissions_tool_in_config",
    "get_permissions_body_in_config",
    "get_permissions_config_repository",
    "replace_permissions_in_config",
    "replace_permissions_tools_in_config",
    "schedule_permissions_config",
    "set_permissions_config_repository",
    "update_permissions_deny_guidance_in_config",
    "update_permissions_enabled_in_config",
    "update_permissions_file_guard_workspace_rw_enabled_in_config",
    "update_permissions_owner_scopes_in_config",
    "update_permissions_rule_in_config",
    "update_permissions_tool_in_config",
]
