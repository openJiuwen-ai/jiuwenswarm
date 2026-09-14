# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""preferred_language_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.locale.repository import (
    PreferredLanguageConfigRepository,
    normalize_preferred_language,
)
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: PreferredLanguageConfigRepository | None = None


def set_preferred_language_config_repository(
    repo: PreferredLanguageConfigRepository | None,
) -> None:
    global _repo
    _repo = repo


def get_preferred_language_config_repository() -> (
    PreferredLanguageConfigRepository | None
):
    return _repo


def clear_preferred_language_config_repository() -> None:
    set_preferred_language_config_repository(None)


def schedule_preferred_language_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def get_preferred_language_in_config() -> str:
    repo = get_preferred_language_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("preferred_language", "zh")
        return normalize_preferred_language(raw)
    return await repo.get_language()


async def update_preferred_language_in_config(lang: str) -> None:
    """更新顶层 preferred_language 并写回。

    企业版无 preferred_language 同构表，且 config.yaml 为只读/全局共享挂载，
    禁止回写（会失败或污染整实例所有用户的语言）。企业版 UI 语言由前端
    localStorage 持久化。
    """
    from jiuwenswarm.edition import is_enterprise

    if is_enterprise():
        return
    repo = get_preferred_language_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_preferred_language_in_config as _legacy,
        )

        _legacy(lang)
        return
    await repo.set_language(lang)


__all__ = [
    "clear_preferred_language_config_repository",
    "get_preferred_language_config_repository",
    "get_preferred_language_in_config",
    "schedule_preferred_language_config",
    "set_preferred_language_config_repository",
    "update_preferred_language_in_config",
]
