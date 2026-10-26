# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CronJob PersistentStore Repository implementing CronJobStoreBackend."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.gateway.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    sort_cron_jobs,
)
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

logger = logging.getLogger(__name__)

CRON_JOB_STORE_NAME = "cron_job"
_PROACTIVE_TICK_MODE = "proactive.tick"
_PROACTIVE_UPDATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"cron_expr", "timezone", "updated_at"}
)


class _ProactiveJobProtected(RuntimeError):
    pass


class CronJobCodec:
    @staticmethod
    def identity(
        job_id: str,
        *,
        service_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        return {
            "id": str(job_id),
            "service_id": str(service_id),
            "agent_id": str(agent_id),
        }

    @staticmethod
    def from_record(record: dict[str, Any]) -> CronJob | None:
        try:
            return CronJob.from_dict(dict(record))
        except Exception:
            return None

    @staticmethod
    def to_record(job: CronJob) -> dict[str, Any]:
        return job.to_dict()

    @staticmethod
    def to_updates(job: CronJob) -> dict[str, Any]:
        data = job.to_dict()
        data.pop("id", None)
        return data


class CronJobRepository:
    """Tenant-scoped cron_job CRUD via PersistentStore; no edition branching."""

    def __init__(
        self,
        store: PersistentStore,
        *,
        service_id: str = "default",
        agent_id: str = "default",
        codec: CronJobCodec | None = None,
    ) -> None:
        self._store = store
        self._codec = codec or CronJobCodec()
        self._service_id = str(service_id or "default").strip() or "default"
        self._agent_id = str(agent_id or "default").strip() or "default"
        self._revision = 0

    @property
    def path(self) -> Path:
        """Compatibility marker for CronTenantRegistry logging."""
        return Path(
            f"persistent://{CRON_JOB_STORE_NAME}/"
            f"{self._service_id}/{self._agent_id}"
        )

    def _scope(self) -> dict[str, Any]:
        return {
            "service_id": self._service_id,
            "agent_id": self._agent_id,
        }

    def _bump_revision(self) -> None:
        self._revision = int(time.time() * 1_000_000)

    async def list_jobs(self, *, filters: dict[str, Any] | None = None) -> list[CronJob]:
        # 与 GatewayDbCronJobStore / EnterpriseAwareCronJobStore 对齐：企业路径
        # CronController.list_jobs 会传 filters={group_id, bot_id, user_id}。
        query = dict(self._scope())
        for key in ("group_id", "bot_id", "user_id"):
            val = (filters or {}).get(key)
            if isinstance(val, str) and val.strip():
                query[key] = val.strip()
        rows = await self._store.list(CRON_JOB_STORE_NAME, filters=query)
        jobs: list[CronJob] = []
        for row in rows:
            job = self._codec.from_record(row)
            if job is not None:
                jobs.append(job)
        return sort_cron_jobs(jobs)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        row = await self._store.get(
            CRON_JOB_STORE_NAME,
            self._codec.identity(
                job_id,
                service_id=self._service_id,
                agent_id=self._agent_id,
            ),
        )
        if row is None:
            return None
        return self._codec.from_record(row)

    async def create_job(
        self,
        *,
        job_id: str | None = None,
        name: str,
        cron_expr: str,
        timezone: str,
        description: str,
        targets: str,
        enabled: bool = True,
        wake_offset_seconds: int | None = None,
        session_id: str | None = None,
        chat_type: str | None = None,
        mode: str | None = None,
        delete_after_run: bool | None = None,
        timeout_seconds: int | None = None,
        project_id: str = "",
        model_name: str | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        service_id: str | None = None,
        agent_id: str | None = None,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> CronJob:
        job = build_new_cron_job(
            job_id=job_id,
            name=name,
            cron_expr=cron_expr,
            timezone=timezone,
            description=description,
            targets=targets,
            enabled=enabled,
            wake_offset_seconds=wake_offset_seconds,
            session_id=session_id,
            chat_type=chat_type,
            mode=mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
            model_name=model_name,
            app_id=app_id,
            work_mode=work_mode,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
        tenant_sid = str(service_id or self._service_id).strip() or self._service_id
        tenant_aid = str(agent_id or self._agent_id).strip() or self._agent_id
        job = replace(job, service_id=tenant_sid, agent_id=tenant_aid)
        await self._upsert_job(job)
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        patch = dict(patch or {})
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")

        if str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE:
            dropped = [k for k in patch if k not in _PROACTIVE_UPDATE_ALLOWED_KEYS]
            if dropped:
                logger.warning(
                    "[CronJobRepository] reject proactive.tick update fields on job=%s: %s",
                    job_id,
                    ", ".join(dropped),
                )
                patch = {
                    k: v for k, v in patch.items() if k in _PROACTIVE_UPDATE_ALLOWED_KEYS
                }

        updated = apply_cron_job_patch(existing, patch)
        updated = replace(
            updated,
            service_id=self._service_id,
            agent_id=self._agent_id,
        )
        await self._upsert_job(updated)
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        if not force:
            existing = await self.get_job(job_id)
            if (
                existing is not None
                and str(getattr(existing, "mode", "") or "").strip().lower()
                == _PROACTIVE_TICK_MODE
            ):
                raise _ProactiveJobProtected(
                    "主动推荐定时任务由设置→主动推荐开关控制，不能删除；请到设置关闭开关。"
                )
        deleted = await self._store.delete(
            CRON_JOB_STORE_NAME,
            self._codec.identity(
                job_id,
                service_id=self._service_id,
                agent_id=self._agent_id,
            ),
        )
        if deleted:
            self._bump_revision()
        return deleted

    async def get_revision(self) -> int:
        jobs = await self.list_jobs()
        if not jobs:
            return int(self._revision or 0)
        stamp = max(float(j.updated_at or 0) for j in jobs)
        db_rev = int(stamp * 1_000_000)
        if self._revision:
            return max(int(self._revision), db_rev)
        return db_rev

    async def upsert_from_dict(self, data: dict[str, Any]) -> CronJob:
        job = CronJob.from_dict(dict(data))
        job = replace(
            job,
            service_id=self._service_id,
            agent_id=self._agent_id,
        )
        await self._upsert_job(job)
        return job

    async def _upsert_job(self, job: CronJob) -> None:
        key = self._codec.identity(
            job.id,
            service_id=self._service_id,
            agent_id=self._agent_id,
        )
        record = self._codec.to_record(job)
        record["service_id"] = self._service_id
        record["agent_id"] = self._agent_id
        updated = await self._store.update(
            CRON_JOB_STORE_NAME,
            key,
            self._codec.to_updates(job),
        )
        if updated is None:
            await self._store.create(CRON_JOB_STORE_NAME, record)
        self._bump_revision()


__all__ = [
    "CRON_JOB_STORE_NAME",
    "CronJobCodec",
    "CronJobRepository",
]
