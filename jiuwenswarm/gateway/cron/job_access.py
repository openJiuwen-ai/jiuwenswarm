# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cron PersistentStore entry: tenant store factory with FileCronJobStore fallback."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.gateway.cron.job_repository import CronJobRepository
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

_store: PersistentStore | None = None


def set_cron_persistent_store(store: PersistentStore | None) -> None:
    global _store
    _store = store


def get_cron_persistent_store() -> PersistentStore | None:
    return _store


def clear_cron_persistent_store() -> None:
    set_cron_persistent_store(None)


def create_tenant_cron_store(
    service_id: str,
    agent_id: str,
) -> Any:
    """Return cron store for tenant scope.

    企业实例已绑定且连远程库时，走 ``GatewayDbCronJobStore``（与 ``cron_job`` 表结构一致）；
    否则 PersistentStore 可用时用 ``CronJobRepository``；再否则文件 store。
    """
    import os

    from jiuwenswarm.gateway.cron.enterprise_gate import enterprise_cron_enabled

    if enterprise_cron_enabled() and os.getenv("GATEWAY_DB_HOST", "").strip():
        from jiuwenswarm.gateway.cron.db_store import GatewayDbCronJobStore

        return GatewayDbCronJobStore()

    store = get_cron_persistent_store()
    if store is None:
        from jiuwenswarm.common.utils import resolve_gateway_cron_jobs_path
        from jiuwenswarm.gateway.cron.store import CronJobStore

        return CronJobStore(path=resolve_gateway_cron_jobs_path(service_id, agent_id))
    return CronJobRepository(
        store,
        service_id=service_id,
        agent_id=agent_id,
    )


__all__ = [
    "clear_cron_persistent_store",
    "create_tenant_cron_store",
    "get_cron_persistent_store",
    "set_cron_persistent_store",
]
