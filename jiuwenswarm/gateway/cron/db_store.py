# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway DB-backed cron job store（企业就绪路径权威存储，经 PersistentStore）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.gateway.cron.cron_expr import _DEFAULT_WAKE_OFFSET_SECONDS
from jiuwenswarm.gateway.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    sort_cron_jobs,
)
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

logger = logging.getLogger(__name__)

_TABLE = "cron_job"

_EXTRA_DATA_KEYS = (
    "project_id",
    "work_mode",
    "model_name",
    "app_id",
    "timeout_seconds",
    "last_session_id",
)


def _utc_now() -> datetime:
    return datetime.now(dt_timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _epoch_to_dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
    except Exception:
        return None


def _dt_to_epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=dt_timezone.utc)
        return float(dt.timestamp())
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_timezone.utc)
            return float(parsed.timestamp())
        except Exception:
            return None
    return None


def _compute_next_run_at(job: CronJob) -> str | None:
    from jiuwenswarm.gateway.cron.db_schedule import (
        compute_next_push_dt,
        datetime_to_db_value,
    )

    try:
        push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
        return datetime_to_db_value(push_dt).isoformat(sep=" ")
    except Exception:
        return None


def _next_run_at_db_value(job: CronJob) -> str | None:
    """序列化 ``job.next_run_at``（库表调度权威值），不做重算。

    ``next_run_at`` 是库表调度的唯一权威：它只能由认领 SQL（``claim_periodic_run`` /
    ``claim_oneshot_run``）原子推进，或在新建/改调度时按 cron 表达式计算一次。
    任何其它 ``update_job``（如回写 ``last_session_id`` / ``expired`` / ``enabled``）
    都不允许重算它——否则会在认领刚推进到下一趟后，又把它按“当前时刻”倒推回去，
    导致多副本重复触发，或跳档“罢工”。

    仅当 ``next_run_at`` 为空（新建任务、或调度被修改后主动清空）时才按表达式计算。
    """
    from jiuwenswarm.gateway.cron.db_schedule import datetime_to_db_value

    raw = getattr(job, "next_run_at", None)
    if raw is not None:
        try:
            if isinstance(raw, datetime):
                dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=dt_timezone.utc)
            else:
                dt = datetime.fromtimestamp(float(raw), tz=dt_timezone.utc)
            return datetime_to_db_value(dt).isoformat(sep=" ")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CronDbStore] 序列化 next_run_at 失败，回退按 cron 表达式重算 "
                "job_id=%s raw=%r: %s",
                getattr(job, "id", None), raw, exc,
            )
    return _compute_next_run_at(job)


def _record_to_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    model_dump = getattr(row, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, dict):
            return dumped
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dumped
    return dict(row) if hasattr(row, "keys") else {}


def _extra_from_job(job: CronJob) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key in _EXTRA_DATA_KEYS:
        value = getattr(job, key, None)
        if value is not None and value != "":
            extra[key] = value
    return extra


def _row_to_job(row: Any) -> CronJob | None:
    try:
        data = _record_to_mapping(row)
        if not data:
            raise ValueError("empty cron_job row mapping")

        extra = data.get("data") if isinstance(data.get("data"), dict) else {}
        if isinstance(data.get("data"), str) and data.get("data", "").strip():
            try:
                parsed = json.loads(data["data"])
                if isinstance(parsed, dict):
                    extra = parsed
            except json.JSONDecodeError:
                extra = {}

        job_dict: dict[str, Any] = {
            "id": str(data.get("job_id") or "").strip(),
            "name": str(data.get("name") or "").strip(),
            "enabled": bool(data.get("enabled", False)),
            "expired": bool(data.get("expired", False)),
            "cron_expr": str(data.get("cron_expr") or "").strip(),
            "timezone": str(data.get("timezone") or "").strip(),
            "wake_offset_seconds": int(
                data.get("wake_offset_seconds")
                if data.get("wake_offset_seconds") is not None
                else _DEFAULT_WAKE_OFFSET_SECONDS
            ),
            "description": str(data.get("description") or ""),
            "targets": str(data.get("targets") or "").strip(),
            "session_id": data.get("session_id"),
            "chat_type": data.get("chat_type"),
            "mode": data.get("mode") or "agent",
            "delete_after_run": bool(data.get("delete_after_run", False)),
            "group_id": data.get("group_id"),
            "bot_id": data.get("bot_id"),
            "user_id": data.get("user_id"),
            "created_at": _dt_to_epoch(data.get("created_at")),
            "updated_at": _dt_to_epoch(data.get("updated_at")),
            "next_run_at": _dt_to_epoch(data.get("next_run_at")),
            "last_run_at": _dt_to_epoch(data.get("last_run_at")),
        }
        for key in _EXTRA_DATA_KEYS:
            if key in extra and extra.get(key) is not None:
                job_dict[key] = extra[key]
        return CronJob.from_dict(job_dict)
    except Exception as exc:
        logger.debug("[GatewayDbCronJobStore] skip invalid row: %s", exc)
        return None


class GatewayDbCronJobStore:
    """企业 cron 权威：``cron_job`` 表经 ``PersistentStore``（按 ``job_id`` 定位）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._revision = 0

    @property
    def path(self) -> Path:
        """Compatibility marker for CronTenantRegistry logging."""
        return Path("db://cron_job")

    @staticmethod
    async def _require_store() -> PersistentStore:
        from jiuwenswarm.gateway.storage.access import require_persistent_store

        return await require_persistent_store()

    @staticmethod
    def _job_identity(*, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id}

    async def get_revision(self) -> int:
        jobs = await self.list_jobs()
        if not jobs:
            return int(self._revision or 0)
        stamp = max(float(j.updated_at or 0) for j in jobs)
        db_rev = int(stamp * 1_000_000)
        if self._revision:
            return max(int(self._revision), db_rev)
        return db_rev

    def _bump_revision(self) -> None:
        self._revision = int(time.time() * 1_000_000)

    async def list_jobs(self, *, filters: dict[str, Any] | None = None) -> list[CronJob]:
        store = await self._require_store()
        query: dict[str, Any] = {}
        filters = dict(filters or {})
        for key in ("group_id", "bot_id", "user_id"):
            val = filters.get(key)
            if isinstance(val, str) and val.strip():
                query[key] = val.strip()
        rows = await store.list(
            _TABLE,
            filters=query or None,
            order_by="updated_at DESC",
        )
        jobs: list[CronJob] = []
        for row in rows or []:
            job = _row_to_job(row)
            if job is not None:
                jobs.append(job)
        return sort_cron_jobs(jobs)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        store = await self._require_store()
        rows = await store.list(
            _TABLE,
            filters=self._job_identity(job_id=job_id),
            limit=1,
        )
        if not rows:
            return None
        return _row_to_job(rows[0])

    @staticmethod
    def _job_to_row(job: CronJob) -> dict[str, Any]:
        now_iso = _utc_now_iso()
        created_iso = (
            _epoch_to_dt(job.created_at).astimezone(dt_timezone.utc).isoformat()
            if job.created_at is not None
            else now_iso
        )
        extra = _extra_from_job(job)
        return {
            "service_id": job.service_id or "default",
            "agent_id": job.agent_id or "default",
            "job_id": job.id,
            "group_id": job.group_id,
            "bot_id": job.bot_id,
            "user_id": job.user_id,
            "name": job.name,
            "description": job.description or None,
            "cron_expr": job.cron_expr,
            "timezone": job.timezone,
            "wake_offset_seconds": int(
                job.wake_offset_seconds
                if job.wake_offset_seconds is not None
                else _DEFAULT_WAKE_OFFSET_SECONDS
            ),
            "enabled": 1 if job.enabled else 0,
            "expired": 1 if job.expired else 0,
            "delete_after_run": 1 if job.delete_after_run else 0,
            "mode": job.mode or "agent",
            "targets": job.targets,
            "session_id": job.session_id,
            "chat_type": job.chat_type,
            "next_run_at": _next_run_at_db_value(job),
            "created_at": created_iso,
            "updated_at": now_iso,
            "data": json.dumps(extra, ensure_ascii=False) if extra else None,
        }

    async def create_job(self, **kwargs: Any) -> CronJob:
        store = await self._require_store()
        tenant_sid = str(kwargs.pop("service_id", None) or "default").strip() or "default"
        tenant_aid = str(kwargs.pop("agent_id", None) or "default").strip() or "default"
        job = replace(
            build_new_cron_job(**kwargs),
            service_id=tenant_sid,
            agent_id=tenant_aid,
        )
        row_data = self._job_to_row(job)
        identity = self._job_identity(job_id=job.id)
        async with self._lock:
            existing_rows = await store.list(_TABLE, filters=identity, limit=1)
            if existing_rows:
                raise ValueError(f"cron job already exists: {job.id}")
            await store.create(_TABLE, row_data)
            self._bump_revision()
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        store = await self._require_store()
        identity = self._job_identity(job_id=job_id)
        async with self._lock:
            existing = await self.get_job(job_id)
            if existing is None:
                raise KeyError("job not found")
            updated = apply_cron_job_patch(existing, patch)
            if "cron_expr" in patch or "timezone" in patch:
                # 调度表达式/时区变化：next_run_at 需按新表达式重算一次。
                # 清空后由 _job_to_row → _next_run_at_db_value 回退到 _compute_next_run_at。
                updated.next_run_at = None
            row_data = self._job_to_row(updated)
            row_data.pop("job_id", None)
            row_data.pop("created_at", None)
            if "last_run_at" in patch:
                row_data["last_run_at"] = patch.get("last_run_at")
            result = await store.update(_TABLE, identity, row_data)
            if result is None:
                raise KeyError("job not found")
            self._bump_revision()
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:  # noqa: ARG002
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        store = await self._require_store()
        identity = self._job_identity(job_id=job_id)
        async with self._lock:
            deleted = await store.delete(_TABLE, identity)
            if deleted:
                self._bump_revision()
            return bool(deleted)
