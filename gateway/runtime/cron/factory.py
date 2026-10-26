"""Factory: personal edition file store vs HA etcd store.

配置解析（CronStoreSettings / load_cron_store_settings）由
``jiuwenswarm.common.cron_settings`` 提供（gateway/runtime/server 三方
共用），此处 re-export——与母仓 factory 同款（减少分叉面）。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.cron_settings import (  # noqa: F401
    CronStoreSettings,
    load_cron_store_settings,
)
from gateway.runtime.cron.etcd_store import EtcdCronJobStore
from gateway.runtime.cron.store import FileCronJobStore
from gateway.runtime.cron.store_base import CronJobStoreBackend

logger = logging.getLogger(__name__)



async def create_gateway_cron_store(
    config: dict[str, Any] | None = None,
) -> CronJobStoreBackend:
    """Return the configured cron job store.

    Default is the personal-edition file store. etcd is opt-in via
    ``gateway.cron.store_backend=etcd`` and is **not** inferred from
    AgentOS routing or from a non-empty endpoints list.
    """
    if config is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
        except Exception:
            config = {}
    settings = load_cron_store_settings(config)
    path = settings.file_path or get_cron_jobs_path()
    if settings.backend != "etcd":
        return FileCronJobStore(path=path)

    if not settings.endpoints:
        logger.error(
            "[Cron] gateway.cron.store_backend=etcd requires etcd_endpoints; "
            "not falling back to local file"
        )
    store = EtcdCronJobStore(
        endpoints=list(settings.endpoints),
        prefix=settings.prefix,
    )
    return store
