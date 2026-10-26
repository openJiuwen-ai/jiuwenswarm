from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from jiuwenswarm.extensions.redis.redis_keys import cron_jobs_hash_rel
from jiuwenswarm.extensions.redis.redis_client import RedisClient
from jiuwenswarm.gateway.cron.cron_job_mutations import apply_cron_job_patch, build_new_cron_job, sort_cron_jobs
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE

logger = logging.getLogger(__name__)

_PROACTIVE_TICK_MODE = "proactive.tick"
_PROACTIVE_UPDATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"cron_expr", "timezone", "expired", "updated_at"}
)


class _ProactiveJobProtected(RuntimeError):
    """proactive.tick job 受保护，禁止手动删除。"""


class RedisCronJobStore:
    """Cron jobs in Redis Hash (active-standby Gateway, scoped by ``gateway.instance_id``).

    变更由本机 ``CronController`` 写后 ``scheduler.reload()`` 感知；不维护 rev / Pub/Sub。
    """

    def __init__(self, client: RedisClient, *, gateway_instance_id: str) -> None:
        self._client = client
        self._gateway_instance_id = str(gateway_instance_id or "").strip()
        self._jobs_hash_rel = cron_jobs_hash_rel(self._gateway_instance_id)
        self._lock = asyncio.Lock()

    async def get_revision(self) -> int:
        """主备单活：不维护 Redis revision，轮询不会据此 reload。"""
        return 0

    async def list_jobs(self) -> list[CronJob]:
        raw = await self._client.hgetall(self._jobs_hash_rel)
        jobs: list[CronJob] = []
        for field, value in (raw or {}).items():
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                item = json.loads(value)
                if isinstance(item, dict):
                    jobs.append(CronJob.from_dict(item))
            except Exception:
                logger.debug("[RedisCronJobStore] skip invalid field=%s", field)
                continue
        return sort_cron_jobs(jobs)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        raw = await self._client.hget(self._jobs_hash_rel, job_id)
        if not raw or not str(raw).strip():
            return None
        try:
            data = json.loads(str(raw))
            if isinstance(data, dict):
                return CronJob.from_dict(data)
        except Exception:
            return None
        return None

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
        )
        async with self._lock:
            await self._client.hset(
                self._jobs_hash_rel,
                job.id,
                json.dumps(job.to_dict(), ensure_ascii=False),
            )
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")
        patch = dict(patch or {})
        if str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE:
            dropped = [k for k in patch if k not in _PROACTIVE_UPDATE_ALLOWED_KEYS]
            if dropped:
                logger.warning(
                    "[RedisCronJobStore] reject proactive.tick update fields on job=%s: %s",
                    job_id,
                    ", ".join(dropped),
                )
                patch = {k: v for k, v in patch.items() if k in _PROACTIVE_UPDATE_ALLOWED_KEYS}
        updated = apply_cron_job_patch(existing, patch)
        async with self._lock:
            await self._client.hset(
                self._jobs_hash_rel,
                job_id,
                json.dumps(updated.to_dict(), ensure_ascii=False),
            )
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        existing = await self.get_job(job_id)
        if existing is None:
            return False
        if not force and str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE:
            raise _ProactiveJobProtected(
                "主动推荐定时任务由设置→主动推荐开关控制，不能删除；请到设置关闭开关。"
            )
        async with self._lock:
            await self._client.hdel(self._jobs_hash_rel, job_id)
            return True
