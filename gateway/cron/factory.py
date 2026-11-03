"""Gateway 启动时选定一次定时任务的存法。

企业版并且配了数据库地址：用数据库表。企业版没配地址就报错。
个人版交给 runtime：本地文件，或配置指定的 etcd。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from gateway.enterprise import is_enterprise
from gateway.runtime.cron.store_base import CronJobStoreBackend

logger = logging.getLogger(__name__)


def _require_enterprise_database_host() -> None:
    """企业版必须配了数据库地址。连没连上留给后面真正读写时再报。"""
    if not os.getenv("GATEWAY_DB_HOST", "").strip():
        raise RuntimeError(
            "企业版定时任务必须使用数据库，但没有配置数据库地址（GATEWAY_DB_HOST）"
        )


async def create_gateway_cron_store(
    config: dict[str, Any] | None = None,
) -> CronJobStoreBackend:
    """进程启动时选一次，之后不再在文件和数据库之间来回换。"""
    if is_enterprise():
        _require_enterprise_database_host()
        from gateway.cron.db_store import GatewayDbCronJobStore

        logger.info("[Cron] gateway cron store: enterprise database")
        return GatewayDbCronJobStore()

    from gateway.runtime.cron.factory import (
        create_gateway_cron_store as create_runtime_cron_store,
    )

    store = await create_runtime_cron_store(config)
    logger.info("[Cron] gateway cron store: runtime file or etcd")
    return store
