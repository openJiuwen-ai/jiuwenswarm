# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Manager Config Receiver 显式 Repository 入口（须在 Gateway 启动时装配）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jiuwenswarm.gateway.config.channel.repository import ChannelConfigRepository
    from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
    from jiuwenswarm.gateway.config.logging.repository import LoggingConfigRepository
    from jiuwenswarm.gateway.config.memory.repository import MemoryConfigRepository


def require_enterprise_repository(store_name: str) -> EnterpriseRecordRepository:
    from jiuwenswarm.gateway.config.enterprise.access import (
        get_enterprise_record_repository,
    )
    from jiuwenswarm.gateway.config.enterprise.repository import (
        EnterpriseRecordRepository,
    )

    repo = get_enterprise_record_repository(store_name)
    if repo is None:
        raise RuntimeError(
            f"EnterpriseRecordRepository {store_name!r} is not wired; "
            "ensure setup_gateway_storage_repositories / "
            "wire_enterprise_persistent_repositories_async ran at Gateway startup"
        )
    return repo


def require_cron_job_enterprise_repository() -> EnterpriseRecordRepository:
    """``cron_job`` 企业 Repository（catalog 登记，启动时随 enterprise repos 注入）。"""
    from jiuwenswarm.gateway.config.enterprise.access import (
        get_enterprise_record_repository,
    )

    repo = get_enterprise_record_repository("cron_job")
    if repo is not None:
        return repo
    raise RuntimeError(
        "Enterprise cron_job repository is not wired; "
        "ensure wire_enterprise_persistent_repositories_async ran at Gateway startup"
    )


def require_channel_repository() -> ChannelConfigRepository:
    from jiuwenswarm.gateway.config.channel.access import get_channel_config_repository
    from jiuwenswarm.gateway.config.channel.repository import ChannelConfigRepository

    repo = get_channel_config_repository()
    if repo is None:
        raise RuntimeError(
            "ChannelConfigRepository is not wired; "
            "ensure gateway.storage.repositories is enabled at startup"
        )
    return repo


def require_logging_repository() -> LoggingConfigRepository:
    from jiuwenswarm.gateway.config.logging.access import get_logging_config_repository
    from jiuwenswarm.gateway.config.logging.repository import LoggingConfigRepository

    repo = get_logging_config_repository()
    if repo is None:
        raise RuntimeError("LoggingConfigRepository is not wired")
    return repo


def require_memory_repository() -> MemoryConfigRepository:
    from jiuwenswarm.gateway.config.memory.access import get_memory_config_repository
    from jiuwenswarm.gateway.config.memory.repository import MemoryConfigRepository

    repo = get_memory_config_repository()
    if repo is None:
        raise RuntimeError("MemoryConfigRepository is not wired")
    return repo


__all__ = [
    "require_channel_repository",
    "require_cron_job_enterprise_repository",
    "require_enterprise_repository",
    "require_logging_repository",
    "require_memory_repository",
]
