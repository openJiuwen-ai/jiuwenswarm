# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版定时任务的数据库存储。

一条任务用群、机器人、用户标明是谁的。没填的身份写成 ``default``，
和文件、etcd 同一条规则。不读写服务编号、Agent 编号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.runtime.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    ensure_proactive_deletable,
    sort_cron_jobs,
)
from gateway.cron.models import CronJob, normalize_tenant_id
from gateway.storage.protocols.persistent import PersistentStore

logger = logging.getLogger(__name__)

_TABLE = "cron_job"
# 没填提前唤醒时按到点执行，和个人版默认一样。
_DEFAULT_WAKE_OFFSET_SECONDS = 0

_EXTRA_DATA_KEYS = (
    "project_id",
    "work_mode",
    "model_name",
    "model_selection",
    "mcp",
    "app_id",
    "timeout_seconds",
    "last_session_id",
    "credential_ref",
)


def _is_duplicate_job_id_error(exc: BaseException) -> bool:
    """判断是否为 ``job_id`` 唯一键冲突（含驱动/外层包装）。"""
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur)
        if "ix_cron_job_job_id" in text or (
            "Duplicate entry" in text and "job_id" in text
        ):
            return True
        if type(cur).__name__ in {"IntegrityError", "DuplicateEntryError"}:
            if "1062" in text or "Duplicate" in text or "unique" in text.lower():
                return True
        cur = cur.__cause__ or cur.__context__
    return False


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
    from gateway.cron.db_schedule import (
        compute_next_push_dt,
        datetime_to_db_value,
    )

    try:
        push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
        return datetime_to_db_value(push_dt).isoformat(sep=" ")
    except Exception:
        return None


def _next_run_at_db_value(job: CronJob) -> str | None:
    """把任务上的下一趟时间写成数据库值。已经有值就不重算。

    下一趟时间只能由认领语句往前推，或在新建、改表达式时算一次。
    其它修改（比如回写会话、过期、开关）不能按「现在」重算，
    否则刚抢到的下一趟会被推回去，多台机器会重复跑或跳过。
    """
    from gateway.cron.db_schedule import datetime_to_db_value

    raw = getattr(job, "next_run_at", None)
    if raw is not None:
        try:
            if isinstance(raw, datetime):
                dt = raw if raw.tzinfo is not None else raw.replace(tzinfo=dt_timezone.utc)
            else:
                dt = datetime.fromtimestamp(float(raw), tz=dt_timezone.utc)
            return datetime_to_db_value(dt).isoformat(sep=" ")
        except Exception as exc:
            logger.warning(
                "[CronDbStore] 序列化 next_run_at 失败，回退按表达式重算 "
                "job_id=%s raw=%r: %s",
                getattr(job, "id", None),
                raw,
                exc,
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
        if value is None or value == "":
            continue
        extra[key] = value
    return extra


def _row_to_job(row: Any) -> CronJob | None:
    try:
        data = _record_to_mapping(row)
        if not data:
            raise ValueError("empty cron_job row mapping")

        extra = data.get("data") if isinstance(data.get("data"), dict) else {}
        if isinstance(data.get("data"), str) and str(data.get("data") or "").strip():
            try:
                parsed = json.loads(data["data"])
                if isinstance(parsed, dict):
                    extra = parsed
            except json.JSONDecodeError:
                extra = {}

        job_dict: dict[str, Any] = {
            "id": str(data.get("job_id") or data.get("id") or "").strip(),
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
            "created_at": _dt_to_epoch(data.get("created_at")),
            "updated_at": _dt_to_epoch(data.get("updated_at")),
            "next_run_at": _dt_to_epoch(data.get("next_run_at")),
            "group_id": data.get("group_id", ""),
            "bot_id": data.get("bot_id", ""),
            "user_id": data.get("user_id", ""),
        }
        for key in _EXTRA_DATA_KEYS:
            if key in extra and extra.get(key) is not None:
                job_dict[key] = extra[key]
        return CronJob.from_dict(job_dict)
    except Exception as exc:
        logger.debug("[GatewayDbCronJobStore] skip invalid row: %s", exc)
        return None


class GatewayDbCronJobStore:
    """企业 cron 权威：``cron_job`` 表，按 ``job_id`` 定位。"""

    supports_watch = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._revision = 0

    @property
    def path(self) -> Path:
        return Path("db://cron_job")

    @staticmethod
    async def _require_store() -> PersistentStore:
        from gateway.storage.access import require_persistent_store

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

    async def list_jobs(
        self,
        *,
        filters: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> list[CronJob]:
        # ``strict`` 与文件/etcd store 协议对齐；DB 查询本身即严格，参数保留兼容。
        _ = strict
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
        created = _epoch_to_dt(job.created_at)
        created_iso = (
            created.astimezone(dt_timezone.utc).isoformat()
            if created is not None
            else now_iso
        )
        extra = _extra_from_job(job)
        return {
            "job_id": job.id,
            "group_id": normalize_tenant_id(job.group_id),
            "bot_id": normalize_tenant_id(job.bot_id),
            "user_id": normalize_tenant_id(job.user_id),
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
        kwargs.pop("service_id", None)
        kwargs.pop("agent_id", None)
        job = build_new_cron_job(**kwargs)
        row_data = self._job_to_row(job)
        identity = self._job_identity(job_id=job.id)
        async with self._lock:
            existing_rows = await store.list(_TABLE, filters=identity, limit=1)
            if existing_rows:
                # 同一 job_id 再创建：runtime 模式下多 Gateway 可能同时收到同一条
                # cron push；或工具超时后带着旧 id 重试。已存在则原样返回，视为成功。
                existing = _row_to_job(existing_rows[0])
                if existing is not None:
                    logger.info(
                        "[CronDbStore] create idempotent hit job_id=%s",
                        job.id,
                    )
                    return existing
            try:
                await store.create(_TABLE, row_data)
            except Exception as exc:
                if not _is_duplicate_job_id_error(exc):
                    raise
                raced = await self.get_job(job.id)
                if raced is not None:
                    logger.info(
                        "[CronDbStore] create race resolved job_id=%s",
                        job.id,
                    )
                    return raced
                raise
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
                updated.next_run_at = None
            row_data = self._job_to_row(updated)
            row_data.pop("job_id", None)
            row_data.pop("created_at", None)
            result = await store.update(_TABLE, identity, row_data)
            if result is None:
                raise KeyError("job not found")
            self._bump_revision()
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        store = await self._require_store()
        identity = self._job_identity(job_id=job_id)
        async with self._lock:
            if not force:
                ensure_proactive_deletable(await self.get_job(job_id), force=False)
            deleted = await store.delete(_TABLE, identity)
            if deleted:
                self._bump_revision()
            return bool(deleted)

    async def watch(self, callback: Any) -> None:
        del callback
        raise NotImplementedError("GatewayDbCronJobStore does not support etcd watch")

    async def aclose(self) -> None:
        return None
