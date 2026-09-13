# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业多副本 Cron：以库表 ``next_run_at`` 为调度权威，条件 UPDATE 认领执行权。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.cron.calc import (
    cron_next_push_dt,
    is_croniter_no_next_date,
)

logger = logging.getLogger(__name__)

_CRON_JOB_TABLE = "cron_job"


def should_use_db_schedule() -> bool:
    """企业多副本 Cron：实例已绑定且 Gateway 连远程 MySQL/PostgreSQL 时启用库调度。

    不依赖 ``JIUWENSWARM_EDITION``（部署侧可能只配 ``GATEWAY_EDITION``）；
    与 ``enterprise_cron_enabled()`` + ``GATEWAY_DB_HOST`` 对齐实际连库路径。
    """
    import os

    from jiuwenswarm.gateway.cron.enterprise_gate import enterprise_cron_enabled

    if not enterprise_cron_enabled():
        return False
    return bool(os.getenv("GATEWAY_DB_HOST", "").strip())


def is_oneshot_job(job: CronJob) -> bool:
    return bool(getattr(job, "delete_after_run", False))


def compute_next_push_dt(cron_expr: str, timezone: str, *, base: datetime | None = None) -> datetime:
    tz = ZoneInfo(timezone or "Asia/Shanghai")
    base_dt = base if base is not None else datetime.now(tz=tz)
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=tz)
    return cron_next_push_dt(cron_expr, base_dt)


def compute_next_push_after_fire(job: CronJob, fire_at: datetime) -> datetime | None:
    """由本趟 push 时刻推算下一趟；无下一趟（一次性 cron）返回 None。

    防积压（存储语义，留在隔离层）：``next_run_at`` 是持久化状态，停机后会
    过期；若 fire_at 已晚于 now，则从 now 起算下一趟，跳过中间所有过期趟。
    """
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=tz)
    else:
        fire_at = fire_at.astimezone(tz)
    # 过期 fire_at 以当前时间为基准，避免链式补跑积压
    base = max(fire_at, datetime.now(tz))
    try:
        return compute_next_push_dt(
            job.cron_expr,
            job.timezone,
            base=base,
        )
    except Exception as exc:
        if is_croniter_no_next_date(exc):
            return None
        raise


def next_run_at_to_datetime(job: CronJob) -> datetime | None:
    raw = getattr(job, "next_run_at", None)
    if raw is None:
        return None
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=tz)
    if isinstance(raw, datetime):
        dt = raw
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
        except ValueError:
            return None
    return None


def datetime_to_db_value(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc).replace(tzinfo=None)


def fire_at_from_run_id(run_id: str, job: CronJob) -> datetime | None:
    try:
        push_ts = int(str(run_id).split(":")[-1])
    except (TypeError, ValueError):
        return next_run_at_to_datetime(job)
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    return datetime.fromtimestamp(push_ts, tz=tz)


async def ensure_job_next_run_at(job: CronJob) -> datetime | None:
    """任务缺少 ``next_run_at`` 时计算并写库；返回 push 时刻。"""
    existing = next_run_at_to_datetime(job)
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    if existing is not None and existing > datetime.now(tz):
        return existing
    push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
    ok = await _update_next_run_at_only(job.id, push_dt, push_dt)
    if ok:
        job.next_run_at = push_dt.timestamp()
    return push_dt


async def refresh_job_next_run_at(job: CronJob) -> datetime | None:
    """创建/修改 cron 表达式后重算下一趟并写库。"""
    push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
    ok = await _update_next_run_at_only(job.id, push_dt, push_dt)
    if ok:
        job.next_run_at = push_dt.timestamp()
    return push_dt


async def claim_periodic_run(job: CronJob, fire_at: datetime) -> bool:
    """周期任务：``next_run_at == fire_at`` 时推进到下一趟，成功即认领本趟。"""
    next_push = compute_next_push_after_fire(job, fire_at)
    if next_push is None:
        return await claim_oneshot_run(job, fire_at)
    return await _claim_update(
        job_id=job.id,
        fire_at=fire_at,
        next_run_at=next_push,
        last_run_at=fire_at,
    )


async def claim_oneshot_run(job: CronJob, fire_at: datetime) -> bool:
    """一次性任务：``next_run_at == fire_at`` 时标记过期并停用，成功即认领本趟（不删行）。"""
    return await _claim_expire(job_id=job.id, fire_at=fire_at)


async def _claim_update(
    *,
    job_id: str,
    fire_at: datetime,
    next_run_at: datetime,
    last_run_at: datetime,
) -> bool:
    fire_db = datetime_to_db_value(fire_at)
    next_db = datetime_to_db_value(next_run_at)
    last_db = datetime_to_db_value(last_run_at)
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET next_run_at = :next_run_at,
            last_run_at = :last_run_at,
            updated_at = :updated_at
        WHERE job_id = :job_id
          AND next_run_at = :fire_at
          AND enabled = 1
          AND expired = 0
    """
    params = {
        "job_id": job_id,
        "fire_at": fire_db,
        "next_run_at": next_db,
        "last_run_at": last_db,
        "updated_at": now_db,
    }
    rows = await _execute_claim_sql(sql, params)
    if rows == 1:
        logger.info(
            "[CronDbSchedule] periodic claim ok job_id=%s fire_at=%s next_run_at=%s",
            job_id,
            fire_at.isoformat(),
            next_run_at.isoformat(),
        )
        return True
    logger.warning(
        "[CronDbSchedule] periodic claim miss job_id=%s fire_at=%s rows=%s",
        job_id,
        fire_at.isoformat(),
        rows,
    )
    return False


async def _claim_expire(*, job_id: str, fire_at: datetime) -> bool:
    fire_db = datetime_to_db_value(fire_at)
    last_db = fire_db
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET expired = 1,
            enabled = 0,
            last_run_at = :last_run_at,
            updated_at = :updated_at
        WHERE job_id = :job_id
          AND next_run_at = :fire_at
          AND enabled = 1
          AND expired = 0
    """
    params = {
        "job_id": job_id,
        "fire_at": fire_db,
        "last_run_at": last_db,
        "updated_at": now_db,
    }
    rows = await _execute_claim_sql(sql, params)
    if rows == 1:
        logger.info(
            "[CronDbSchedule] oneshot claim ok job_id=%s fire_at=%s (marked expired)",
            job_id,
            fire_at.isoformat(),
        )
        return True
    logger.warning(
        "[CronDbSchedule] oneshot claim miss job_id=%s fire_at=%s rows=%s",
        job_id,
        fire_at.isoformat(),
        rows,
    )
    return False


async def _update_next_run_at_only(
    job_id: str,
    next_run_at: datetime,
    last_run_at: datetime | None,
) -> bool:
    next_db = datetime_to_db_value(next_run_at)
    last_db = (
        datetime_to_db_value(last_run_at)
        if last_run_at is not None
        else None
    )
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    sets = ["next_run_at = :next_run_at", "updated_at = :updated_at"]
    params: dict[str, Any] = {
        "job_id": job_id,
        "next_run_at": next_db,
        "updated_at": now_db,
    }
    if last_db is not None:
        sets.insert(1, "last_run_at = :last_run_at")
        params["last_run_at"] = last_db
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET {", ".join(sets)}
        WHERE job_id = :job_id
    """
    rows = await _execute_claim_sql(sql, params)
    return rows == 1


async def _execute_claim_sql(sql: str, params: dict[str, Any]) -> int:
    from sqlalchemy import text

    from jiuwenswarm.infrastructure.db.database import Database

    handler = await Database.current().ensure_ready(log_prefix="cron_db_schedule")
    engine = handler.get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return int(result.rowcount or 0)


__all__ = (
    "claim_oneshot_run",
    "claim_periodic_run",
    "compute_next_push_after_fire",
    "compute_next_push_dt",
    "ensure_job_next_run_at",
    "fire_at_from_run_id",
    "is_oneshot_job",
    "next_run_at_to_datetime",
    "refresh_job_next_run_at",
    "should_use_db_schedule",
)
