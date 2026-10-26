# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Channel 配置 Repository 进程入口。业务侧不判断 edition。"""

from __future__ import annotations

import asyncio
from typing import Any

from jiuwenswarm.gateway.config.channel.repository import ChannelConfigRepository
from jiuwenswarm.gateway.storage.async_bridge import run_awaitable

_repo: ChannelConfigRepository | None = None


def set_channel_config_repository(repo: ChannelConfigRepository | None) -> None:
    """Gateway 启动时注入；shutdown 时传 None 解除。"""
    global _repo
    _repo = repo


def get_channel_config_repository() -> ChannelConfigRepository | None:
    return _repo


def clear_channel_config_repository() -> None:
    set_channel_config_repository(None)


def schedule_channel_config(awaitable: Any) -> None:
    """同步调用方把持久化丢到当前 loop；没有 loop 时阻塞跑完。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        run_awaitable(awaitable)
        return
    loop.create_task(awaitable)


async def update_channel_in_config(channel_id: str, conf: dict[str, Any]) -> None:
    """合并 ``channels[channel_id]`` 顶层字段。"""
    repo = get_channel_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_channel_in_config as _yaml

        _yaml(channel_id, conf)
        return
    await repo.merge_body(channel_id, conf)


async def replace_channel_subsection_with_cleanup(
    channel_id: str,
    subsection_id: str,
    conf: dict[str, Any] | list[Any] | Any,
    keep_keys: set[str],
) -> None:
    """整段替换 subsection，并丢掉不在 ``keep_keys`` 里的旧字段。"""
    repo = get_channel_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            replace_channel_subsection_with_cleanup as _yaml,
        )

        _yaml(channel_id, subsection_id, conf, keep_keys)
        return
    await repo.replace_subsection_with_cleanup(
        channel_id, subsection_id, conf, keep_keys
    )


async def update_channel_subsection_in_config(
    channel_id: str,
    subsection_id: str,
    conf: dict[str, Any] | list[Any] | Any,
) -> None:
    """更新 ``channels[channel_id][subsection_id]``。dict 合并，其它类型整段替换。"""
    repo = get_channel_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import (
            update_channel_subsection_in_config as _yaml,
        )

        _yaml(channel_id, subsection_id, conf)
        return
    await repo.merge_or_replace_subsection(channel_id, subsection_id, conf)


async def update_channel_app_field(
    channel_id: str,
    app_identifier: str,
    field_values: dict[str, Any],
    *,
    app_id_key: str = "app_id",
) -> bool:
    """更新 ``apps[]`` 里匹配 ``app_id`` 的那一项。"""
    repo = get_channel_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_channel_app_field as _yaml

        return _yaml(
            channel_id, app_identifier, field_values, app_id_key=app_id_key
        )
    return await repo.update_app_fields(
        channel_id, app_identifier, field_values, app_id_key=app_id_key
    )


async def update_xiaoyi_runtime_in_config(
    conf: dict[str, Any],
    *,
    api_id: str = "",
    agent_id: str = "",
) -> None:
    """更新 ``channels.xiaoyi`` 运行时身份，必要时同步 ``apps[].push_id``。"""
    repo = get_channel_config_repository()
    if repo is None:
        from jiuwenswarm.common.config import update_xiaoyi_runtime_in_config as _yaml

        _yaml(conf, api_id=api_id, agent_id=agent_id)
        return
    await repo.update_xiaoyi_runtime(conf, api_id=api_id, agent_id=agent_id)


__all__ = [
    "clear_channel_config_repository",
    "get_channel_config_repository",
    "replace_channel_subsection_with_cleanup",
    "schedule_channel_config",
    "set_channel_config_repository",
    "update_channel_app_field",
    "update_channel_in_config",
    "update_channel_subsection_in_config",
    "update_xiaoyi_runtime_in_config",
]
