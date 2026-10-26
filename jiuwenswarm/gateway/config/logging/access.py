# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""logging_config 进程入口。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.logging.repository import (
    LOGGING_LEVEL_FIELDS,
    LoggingConfigRepository,
)
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: LoggingConfigRepository | None = None


def set_logging_config_repository(repo: LoggingConfigRepository | None) -> None:
    global _repo
    _repo = repo


def get_logging_config_repository() -> LoggingConfigRepository | None:
    return _repo


def clear_logging_config_repository() -> None:
    set_logging_config_repository(None)


def schedule_logging_config(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


def _apply_runtime_levels(payload: dict[str, Any] | None) -> None:
    from jiuwenswarm.common.utils import apply_logging_config_payload

    apply_logging_config_payload(payload)


def _legacy_persist_logging_levels(updates: dict[str, Any]) -> None:
    from jiuwenswarm.common.config import update_config

    cleaned = {
        key: value
        for key, value in updates.items()
        if key in LOGGING_LEVEL_FIELDS and value is not None
    }
    if not cleaned:
        return

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = data.get("logging")
        if not isinstance(section, dict):
            section = {}
            data["logging"] = section
        section.update(cleaned)
        return data

    update_config(_mutate)


def _runtime_kwargs_from_updates(updates: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if updates.get("level") is not None:
        kwargs["log_level"] = str(updates["level"])
    for key in ("console_level", "gateway", "channel", "agent_server", "full"):
        if updates.get(key) is not None:
            kwargs[key] = str(updates[key])
    return kwargs


async def get_logging_body_in_config() -> dict[str, Any]:
    repo = get_logging_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import get_config

        raw = (get_config() or {}).get("logging")
        return dict(raw) if isinstance(raw, dict) else {}
    return await repo.get_body()


def get_logging_config_section_sync() -> dict[str, Any]:
    """同步读取 ``logging`` 段（Repository 注入时走 PersistentStore）。"""
    body = run_awaitable(get_logging_body_in_config())
    return dict(body) if isinstance(body, dict) else {}


async def merge_logging_levels_in_config(updates: dict[str, Any]) -> None:
    """合并 logging 级别字段并持久化，同时热更新本进程级别。"""
    if not isinstance(updates, dict):
        raise ValueError("logging updates must be an object")
    repo = get_logging_config_repository()
    if repo is None:
        from jiuwenswarm.common.utils import update_log_levels

        _legacy_persist_logging_levels(updates)
        kwargs = _runtime_kwargs_from_updates(updates)
        if kwargs:
            update_log_levels(**kwargs)
        return

    document = await repo.merge_levels(updates)
    _apply_runtime_levels(document.body)


async def replace_logging_in_config(body: dict[str, Any]) -> None:
    """整段替换 ``logging``（EE Manager upsert 语义）。"""
    if not isinstance(body, dict):
        raise ValueError("logging body must be an object")
    cleaned = {
        key: value
        for key, value in body.items()
        if key in LOGGING_LEVEL_FIELDS and value is not None
    }
    # 保留非级别字段（如 preview_user_content）
    for key, value in body.items():
        if key not in LOGGING_LEVEL_FIELDS:
            cleaned[key] = value

    repo = get_logging_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data["logging"] = dict(cleaned)
            return data

        update_config(_mutate)
        _apply_runtime_levels(cleaned)
        return

    document = await repo.replace(cleaned)
    _apply_runtime_levels(document.body)


async def delete_logging_in_config() -> bool:
    """删除 ``logging`` 段 / 企业行，并恢复代码默认级别。"""
    repo = get_logging_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_config

        found = {"value": False}

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            if "logging" in data:
                del data["logging"]
                found["value"] = True
            return data

        update_config(_mutate)
        _apply_runtime_levels({"op": "delete"})
        return bool(found["value"])

    deleted = await repo.delete()
    _apply_runtime_levels({"op": "delete"})
    return deleted


__all__ = [
    "clear_logging_config_repository",
    "delete_logging_in_config",
    "get_logging_body_in_config",
    "get_logging_config_repository",
    "get_logging_config_section_sync",
    "merge_logging_levels_in_config",
    "replace_logging_in_config",
    "schedule_logging_config",
    "set_logging_config_repository",
]
