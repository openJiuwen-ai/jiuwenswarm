# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""把定时任务接到 Gateway 的持久化接口上。

一条仓库实例只看同一个群、机器人和用户。身份不再用服务编号和 Agent 编号。
个人版启动时不创建它；企业库表存储走 ``GatewayDbCronJobStore``。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from gateway.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.runtime.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    ensure_proactive_deletable,
    sort_cron_jobs,
)
from gateway.cron.models import CronJob, normalize_tenant_id
from gateway.storage.protocols.persistent import PersistentStore

logger = logging.getLogger(__name__)

CRON_JOB_STORE_NAME = "cron_job"


class CronJobCodec:
    @staticmethod
    def identity(job_id: str) -> dict[str, Any]:
        return {"job_id": str(job_id)}

    @staticmethod
    def from_record(record: dict[str, Any]) -> CronJob | None:
        try:
            data = dict(record)
            if not str(data.get("id") or "").strip() and data.get("job_id"):
                data["id"] = data.get("job_id")
            return CronJob.from_dict(data)
        except Exception:
            logger.debug("[CronJobRepository] skip invalid record", exc_info=True)
            return None

    @staticmethod
    def to_record(job: CronJob) -> dict[str, Any]:
        data = job.to_dict()
        data["job_id"] = job.id
        data["group_id"] = normalize_tenant_id(job.group_id)
        data["bot_id"] = normalize_tenant_id(job.bot_id)
        data["user_id"] = normalize_tenant_id(job.user_id)
        return data

    @staticmethod
    def to_updates(job: CronJob) -> dict[str, Any]:
        data = CronJobCodec.to_record(job)
        data.pop("id", None)
        data.pop("job_id", None)
        return data


class CronJobRepository:
    """按群、机器人、用户限定范围的定时任务读写。"""

    supports_watch = False

    def __init__(
        self,
        store: PersistentStore,
        *,
        group_id: str,
        bot_id: str,
        user_id: str,
        codec: CronJobCodec | None = None,
    ) -> None:
        self._store = store
        self._codec = codec or CronJobCodec()
        self._group_id = normalize_tenant_id(group_id)
        self._bot_id = normalize_tenant_id(bot_id)
        self._user_id = normalize_tenant_id(user_id)
        self._revision = 0

    @property
    def path(self) -> Path:
        return Path(
            f"persistent://{CRON_JOB_STORE_NAME}/"
            f"{self._group_id}/{self._bot_id}/{self._user_id}"
        )

    def _scope(self) -> dict[str, Any]:
        return {
            "group_id": self._group_id,
            "bot_id": self._bot_id,
            "user_id": self._user_id,
        }

    def _owns(self, job: CronJob) -> bool:
        return (
            normalize_tenant_id(job.group_id) == self._group_id
            and normalize_tenant_id(job.bot_id) == self._bot_id
            and normalize_tenant_id(job.user_id) == self._user_id
        )

    def _bump_revision(self) -> None:
        self._revision = int(time.time() * 1_000_000)

    async def list_jobs(self, *, filters: dict[str, Any] | None = None) -> list[CronJob]:
        query = dict(self._scope())
        for key in ("group_id", "bot_id", "user_id"):
            val = (filters or {}).get(key)
            if isinstance(val, str) and val.strip():
                query[key] = val.strip()
        rows = await self._store.list(CRON_JOB_STORE_NAME, filters=query)
        jobs: list[CronJob] = []
        for row in rows:
            job = self._codec.from_record(row)
            if job is not None and self._owns(job):
                jobs.append(job)
        return sort_cron_jobs(jobs)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        row = await self._store.get(
            CRON_JOB_STORE_NAME,
            self._codec.identity(job_id),
        )
        if row is None:
            return None
        job = self._codec.from_record(row)
        if job is None or not self._owns(job):
            return None
        return job

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
        model_selection: dict[str, str] | None = None,
        mcp: list[str] | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        user_id: str | None = None,
        group_id: str | None = None,
        bot_id: str | None = None,
        credential_ref: str = "",
    ) -> CronJob:
        del user_id, group_id, bot_id
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
            model_selection=model_selection,
            mcp=mcp,
            app_id=app_id,
            work_mode=work_mode,
            credential_ref=credential_ref,
            group_id=self._group_id,
            bot_id=self._bot_id,
            user_id=self._user_id,
        )
        job = replace(
            job,
            group_id=self._group_id,
            bot_id=self._bot_id,
            user_id=self._user_id,
        )
        await self._upsert_job(job)
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")
        updated = apply_cron_job_patch(existing, patch)
        updated = replace(
            updated,
            group_id=self._group_id,
            bot_id=self._bot_id,
            user_id=self._user_id,
        )
        await self._upsert_job(updated)
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        existing = await self.get_job(job_id)
        if existing is None:
            return False
        ensure_proactive_deletable(existing, force=force)
        deleted = await self._store.delete(
            CRON_JOB_STORE_NAME,
            self._codec.identity(job_id),
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

    async def watch(self, callback: Any) -> None:
        del callback
        raise NotImplementedError("CronJobRepository does not support etcd watch")

    async def aclose(self) -> None:
        return None

    async def _upsert_job(self, job: CronJob) -> None:
        key = self._codec.identity(job.id)
        updated = await self._store.update(
            CRON_JOB_STORE_NAME,
            key,
            self._codec.to_updates(job),
        )
        if updated is None:
            await self._store.create(CRON_JOB_STORE_NAME, self._codec.to_record(job))
        self._bump_revision()


__all__ = [
    "CRON_JOB_STORE_NAME",
    "CronJobCodec",
    "CronJobRepository",
]
