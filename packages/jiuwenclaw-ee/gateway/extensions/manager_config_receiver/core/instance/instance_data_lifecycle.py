# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Gateway 实例生命周期：响应 Manager purge 指令并清理本机库全部实例数据。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository

from ...infrastructure.repository_access import (
    require_channel_repository,
    require_cron_job_enterprise_repository,
    require_enterprise_repository,
    require_logging_repository,
)
from ..template.a2a_outbound_template import A2AOutboundTemplateService

logger = logging.getLogger(__name__)

_LIST_ALL_CAP = 10_000

INSTANCE_PURGE_TABLES: tuple[str, ...] = (
    "model_template",
    "embedding_template",
    "extension_config_template",
    "skill_prebuilt_template",
    "mcp_template",
    "permissions_template",
    "a2a_outbound_template",
    "a2a_access_policy_template",
    "a2a_outbound_user_state",
    "a2a_outbound_runtime_state",
    "a2a_outbound_dispatch",
    "agent_template",
    "instance_agent_resource",
    "log_masking_rule",
    "logging_config",
    "task_memory_config",
    "memory_config",
    "cron_job",
)

_MANAGER_SIGN_PUBKEY_TABLE = "manager_sign_pubkey"

_EXCLUDED_FROM_ENTERPRISE_BULK_PURGE: frozenset[str] = frozenset({
    "channel_config",
    "logging_config",
    "cron_job",
    "task_memory_config",
})

_enterprise_purge_tables: list[str] = []
for _table in INSTANCE_PURGE_TABLES:
    if _table not in _EXCLUDED_FROM_ENTERPRISE_BULK_PURGE:
        _enterprise_purge_tables.append(_table)
_ENTERPRISE_PURGE_TABLES: frozenset[str] = frozenset(_enterprise_purge_tables)


async def _purge_cron_job_table() -> int:
    return await _purge_enterprise_repository(
        require_cron_job_enterprise_repository()
    )


async def _purge_enterprise_table(table: str) -> int:
    if table == "a2a_outbound_template":
        return await _purge_a2a_outbound_templates()
    return await _purge_enterprise_repository(require_enterprise_repository(table))


async def _purge_a2a_outbound_templates() -> int:
    repo = require_enterprise_repository("a2a_outbound_template")
    service = A2AOutboundTemplateService()
    deleted = 0
    for row in await repo.list(limit=_LIST_ALL_CAP):
        template_id = str(row.get("template_id") or "").strip()
        if not template_id:
            continue
        await service.delete(template_id)
        deleted += 1
    return deleted


async def _purge_enterprise_repository(repo: EnterpriseRecordRepository) -> int:
    key_fields = repo.key_fields
    if not key_fields:
        return 1 if await repo.delete() else 0

    deleted = 0
    for row in await repo.list(limit=_LIST_ALL_CAP):
        key_parts = {field: row[field] for field in key_fields if field in row}
        if len(key_parts) != len(key_fields):
            continue
        if await repo.delete(key_parts):
            deleted += 1
    return deleted


async def _purge_channel_config() -> int:
    repo = require_channel_repository()
    deleted = 0
    for config in await repo.list(limit=_LIST_ALL_CAP):
        if await repo.delete(config.channel_id):
            deleted += 1
    return deleted


async def purge_gateway_instance_data() -> dict[str, int]:
    """清空本 Gateway 本地库中的全部实例级数据（幂等）。

    前提：每网关独立数据库，无跨实例行级隔离。此操作不可逆，将删除本库内
    模板 / 资源 / 应用配置 / channel / cron / Manager 公钥等实例数据，
    而非按 ``jiuwenclaw_id`` 过滤单实例。若未来改为多实例共享同一库，
    需重新引入按实例范围清理，否则会误删其它实例数据。
    """
    deleted_counts: dict[str, int] = {}

    for table in _ENTERPRISE_PURGE_TABLES:
        count = await _purge_enterprise_table(table)
        if count:
            deleted_counts[table] = count

    channel_count = await _purge_channel_config()
    if channel_count:
        deleted_counts["channel_config"] = channel_count

    if await require_logging_repository().delete():
        deleted_counts["logging_config"] = 1

    if await require_enterprise_repository("task_memory_config").delete():
        deleted_counts["task_memory_config"] = 1

    cron_count = await _purge_cron_job_table()
    if cron_count:
        deleted_counts["cron_job"] = cron_count

    if await require_enterprise_repository(_MANAGER_SIGN_PUBKEY_TABLE).delete():
        deleted_counts[_MANAGER_SIGN_PUBKEY_TABLE] = 1

    logger.info(
        "[instance_data_lifecycle] purged gateway instance data counts=%s",
        deleted_counts,
    )
    return deleted_counts


class InstanceDataLifecycleService:

    async def purge(self) -> dict[str, Any]:
        counts = await purge_gateway_instance_data()
        logger.info(
            "[ManagerConfigReceiver] instance_data_lifecycle purge counts=%s",
            counts,
        )
        return {"purged": counts}


__all__ = (
    "INSTANCE_PURGE_TABLES",
    "InstanceDataLifecycleService",
    "purge_gateway_instance_data",
)
