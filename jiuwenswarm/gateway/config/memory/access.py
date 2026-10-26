# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""memory_config 进程入口。

覆盖 ``config.yaml`` 的 ``/memory`` 段（含 ``forbidden_memory_definition``）。
``modes.claw.*.memory`` 与顶层 ``auto_memory_enabled`` 不在本 store，仍走
``common.config``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.memory.repository import MemoryConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: MemoryConfigRepository | None = None


def set_memory_config_repository(repo: MemoryConfigRepository | None) -> None:
    global _repo
    _repo = repo


def get_memory_config_repository() -> MemoryConfigRepository | None:
    return _repo


def clear_memory_config_repository() -> None:
    set_memory_config_repository(None)


def schedule_memory_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def get_memory_body_in_config() -> dict[str, Any]:
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("memory")
        return dict(raw) if isinstance(raw, dict) else {}
    return await repo.get_body()


async def replace_memory_in_config(body: dict[str, Any]) -> None:
    """整段替换 ``memory``（EE Manager upsert 语义）。"""
    if not isinstance(body, dict):
        raise ValueError("memory body must be an object")
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["memory"] = dict(body)
            return data

        update_config(_mutate)
        return
    await repo.replace(body)


async def merge_memory_in_config(updates: dict[str, Any]) -> None:
    """浅合并 ``memory`` 顶层字段。"""
    if not isinstance(updates, dict):
        raise ValueError("memory updates must be an object")
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            section = data.get("memory")
            if not isinstance(section, dict):
                section = {}
                data["memory"] = section
            section.update(updates)
            return data

        update_config(_mutate)
        return
    await repo.merge(updates)


async def delete_memory_in_config() -> bool:
    """删除 ``memory`` 段 / 企业行（EE Manager delete 语义）。"""
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        found = {"value": False}

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            if "memory" in data:
                del data["memory"]
                found["value"] = True
            return data

        update_config(_mutate)
        return bool(found["value"])
    return await repo.delete()


async def update_memory_forbidden_enabled_in_config(value: bool) -> None:
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_memory_forbidden_enabled_in_config as _legacy,
        )

        _legacy(value)
        return
    await repo.set_forbidden_enabled(value)


async def update_memory_forbidden_description_in_config(
    description: dict[str, str],
) -> None:
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_memory_forbidden_description_in_config as _legacy,
        )

        _legacy(description)
        return
    await repo.merge_forbidden_description(description)


async def update_memory_forbidden_in_config(updates: dict[str, Any]) -> None:
    repo = get_memory_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_memory_forbidden_in_config as _legacy,
        )

        _legacy(updates)
        return
    await repo.merge_forbidden(updates)


__all__ = [
    "clear_memory_config_repository",
    "delete_memory_in_config",
    "get_memory_body_in_config",
    "get_memory_config_repository",
    "merge_memory_in_config",
    "replace_memory_in_config",
    "schedule_memory_config",
    "set_memory_config_repository",
    "update_memory_forbidden_description_in_config",
    "update_memory_forbidden_enabled_in_config",
    "update_memory_forbidden_in_config",
]
