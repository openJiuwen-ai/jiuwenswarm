# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""heartbeat_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.heartbeat.repository import HeartbeatConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: HeartbeatConfigRepository | None = None


def set_heartbeat_config_repository(repo: HeartbeatConfigRepository | None) -> None:
    global _repo
    _repo = repo


def get_heartbeat_config_repository() -> HeartbeatConfigRepository | None:
    return _repo


def clear_heartbeat_config_repository() -> None:
    set_heartbeat_config_repository(None)


def schedule_heartbeat_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def get_heartbeat_body_in_config() -> dict[str, Any]:
    repo = get_heartbeat_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("heartbeat")
        return dict(raw) if isinstance(raw, dict) else {}
    return await repo.get_body()


async def update_heartbeat_in_config(payload: dict[str, Any]) -> None:
    """合并 heartbeat 字段并写回（every / target / active_hours）。"""
    if not isinstance(payload, dict):
        raise ValueError("heartbeat payload must be an object")
    repo = get_heartbeat_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_heartbeat_in_config as _legacy,
        )

        _legacy(payload)
        return
    await repo.merge_heartbeat_fields(payload)


async def replace_heartbeat_in_config(body: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        raise ValueError("heartbeat body must be an object")
    repo = get_heartbeat_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["heartbeat"] = dict(body)
            return data

        update_config(_mutate)
        return
    await repo.replace(body)


__all__ = [
    "clear_heartbeat_config_repository",
    "get_heartbeat_body_in_config",
    "get_heartbeat_config_repository",
    "replace_heartbeat_in_config",
    "schedule_heartbeat_config",
    "set_heartbeat_config_repository",
    "update_heartbeat_in_config",
]
