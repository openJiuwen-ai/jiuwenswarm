# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""browser_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.browser.repository import BrowserConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: BrowserConfigRepository | None = None


def set_browser_config_repository(repo: BrowserConfigRepository | None) -> None:
    global _repo
    _repo = repo


def get_browser_config_repository() -> BrowserConfigRepository | None:
    return _repo


def clear_browser_config_repository() -> None:
    set_browser_config_repository(None)


def schedule_browser_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def get_browser_body_in_config() -> dict[str, Any]:
    repo = get_browser_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("browser")
        return dict(raw) if isinstance(raw, dict) else {}
    return await repo.get_body()


async def update_browser_in_config(updates: dict[str, Any]) -> None:
    """浅合并 ``browser`` 顶层字段（如 chrome_path / headless）。"""
    if not isinstance(updates, dict):
        raise ValueError("browser updates must be an object")
    repo = get_browser_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_browser_in_config as _legacy,
        )

        _legacy(updates)
        return
    await repo.merge(updates)


async def replace_browser_in_config(body: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        raise ValueError("browser body must be an object")
    repo = get_browser_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["browser"] = dict(body)
            return data

        update_config(_mutate)
        return
    await repo.replace(body)


__all__ = [
    "clear_browser_config_repository",
    "get_browser_body_in_config",
    "get_browser_config_repository",
    "replace_browser_in_config",
    "schedule_browser_config",
    "set_browser_config_repository",
    "update_browser_in_config",
]
