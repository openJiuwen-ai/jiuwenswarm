# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 cron store 门面：file store 与 Gateway DB store 之间按门控切换。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.cron.enterprise_gate import enterprise_cron_enabled
from jiuwenswarm.gateway.cron.models import CronJob

logger = logging.getLogger(__name__)

_NON_ENTERPRISE_CREATE_KEYS = frozenset(
    {
        "job_id",
        "name",
        "cron_expr",
        "timezone",
        "description",
        "targets",
        "enabled",
        "wake_offset_seconds",
        "session_id",
        "chat_type",
        "mode",
        "delete_after_run",
        "timeout_seconds",
        "project_id",
        "model_name",
        "app_id",
        "work_mode",
    }
)


class EnterpriseAwareCronJobStore:
    """非企业 backend 为兜底；企业就绪后切换到 Gateway DB。"""

    def __init__(self, non_enterprise_store: Any) -> None:
        self._non_enterprise = non_enterprise_store
        self._db_store: Any | None = None

    def _db(self) -> Any:
        if self._db_store is None:
            from jiuwenswarm.gateway.cron.db_store import GatewayDbCronJobStore

            self._db_store = GatewayDbCronJobStore()
            logger.info("[Cron] switched to Gateway DB cron_job store (enterprise ready)")
        return self._db_store

    @staticmethod
    def _use_enterprise() -> bool:
        return enterprise_cron_enabled()

    async def list_jobs(self, *, filters: dict[str, Any] | None = None) -> list[CronJob]:
        if self._use_enterprise():
            return await self._db().list_jobs(filters=filters)
        return await self._non_enterprise.list_jobs()

    async def get_job(self, job_id: str) -> CronJob | None:
        if self._use_enterprise():
            return await self._db().get_job(job_id)
        return await self._non_enterprise.get_job(job_id)

    async def create_job(self, **kwargs: Any) -> CronJob:
        if self._use_enterprise():
            return await self._db().create_job(**kwargs)
        non_enterprise_kwargs = {
            k: v for k, v in kwargs.items() if k in _NON_ENTERPRISE_CREATE_KEYS
        }
        return await self._non_enterprise.create_job(**non_enterprise_kwargs)

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        if self._use_enterprise():
            return await self._db().update_job(job_id, patch)
        return await self._non_enterprise.update_job(job_id, patch)

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        if self._use_enterprise():
            return await self._db().delete_job(job_id, force=force)
        return await self._non_enterprise.delete_job(job_id, force=force)

    async def get_revision(self) -> int:
        if self._use_enterprise():
            return await self._db().get_revision()
        if hasattr(self._non_enterprise, "get_revision"):
            return await self._non_enterprise.get_revision()
        return 0
