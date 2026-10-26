# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""a2ui_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.a2ui.repository import A2uiConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: A2uiConfigRepository | None = None


def set_a2ui_config_repository(repo: A2uiConfigRepository | None) -> None:
    global _repo
    _repo = repo


def get_a2ui_config_repository() -> A2uiConfigRepository | None:
    return _repo


def clear_a2ui_config_repository() -> None:
    set_a2ui_config_repository(None)


def schedule_a2ui_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def get_a2ui_body_in_config() -> dict[str, Any]:
    repo = get_a2ui_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("a2ui")
        return dict(raw) if isinstance(raw, dict) else {}
    return await repo.get_body()


async def update_a2ui_in_config(updates: dict[str, Any]) -> None:
    """浅合并 ``a2ui`` 顶层字段。"""
    if not isinstance(updates, dict):
        raise ValueError("a2ui updates must be an object")
    repo = get_a2ui_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_a2ui_in_config as _legacy,
        )

        _legacy(updates)
        return
    await repo.merge(updates)


async def replace_a2ui_in_config(body: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        raise ValueError("a2ui body must be an object")
    repo = get_a2ui_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["a2ui"] = dict(body)
            return data

        update_config(_mutate)
        return
    await repo.replace(body)


__all__ = [
    "clear_a2ui_config_repository",
    "get_a2ui_body_in_config",
    "get_a2ui_config_repository",
    "replace_a2ui_in_config",
    "schedule_a2ui_config",
    "set_a2ui_config_repository",
    "update_a2ui_in_config",
]
