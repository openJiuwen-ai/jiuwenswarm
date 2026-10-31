# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""多台机器一起跑时，用数据库里的「下一趟时间」抢这一趟归谁执行。

抢到的机器才跑。没抢到就跳过，避免同一条任务被跑两遍。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

from gateway.enterprise import is_enterprise
from gateway.cron.models import CronJob
from jiuwenswarm.runtime.cron.cron_expr import (
    is_croniter_no_next_date,
    next_cron_datetime,
)

logger = logging.getLogger(__name__)

_CRON_JOB_TABLE = "cron_job"


def should_use_db_schedule() -> bool:
    """企业版用库表认领。个人版不走这里。

    企业版没配数据库地址时，由工厂在启动时直接报错，不会退回文件。
    """
    return is_enterprise()


def is_oneshot_job(job: CronJob) -> bool:
    return bool(getattr(job, "delete_after_run", False))


def compute_next_push_dt(
    cron_expr: str,
    timezone: str,
    *,
    base: datetime | None = None,
) -> datetime:
    tz = ZoneInfo(timezone or "Asia/Shanghai")
    base_dt = base if base is not None else datetime.now(tz=tz)
    if base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=tz)
    return next_cron_datetime(cron_expr, base_dt)


def compute_next_push_after_fire(job: CronJob, fire_at: datetime) -> datetime | None:
    """由这一趟的触发时刻推下一趟。算不出下一趟时返回空（一次性任务）。

    如果这一趟的时刻已经比现在还早，就从现在起算，跳过中间积压的趟，避免补跑一长串。
    """
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=tz)
    else:
        fire_at = fire_at.astimezone(tz)
    base = max(fire_at, datetime.now(tz))
    try:
        return compute_next_push_dt(job.cron_expr, job.timezone, base=base)
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
    """任务还没有下一趟时间，或者已经过点时，算出下一趟写回数据库。"""
    existing = next_run_at_to_datetime(job)
    tz = ZoneInfo(job.timezone or "Asia/Shanghai")
    if existing is not None and existing > datetime.now(tz):
        return existing
    push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
    ok = await _update_next_run_at_only(job.id, push_dt)
    if ok:
        job.next_run_at = push_dt.timestamp()
    return push_dt


async def refresh_job_next_run_at(job: CronJob) -> datetime | None:
    """创建或改了表达式之后，按新表达式重算下一趟并写库。"""
    push_dt = compute_next_push_dt(job.cron_expr, job.timezone)
    ok = await _update_next_run_at_only(job.id, push_dt)
    if ok:
        job.next_run_at = push_dt.timestamp()
    return push_dt


async def claim_periodic_run(job: CronJob, fire_at: datetime) -> bool:
    """周期任务：库里的下一趟正好是这一趟时，推进到再下一趟。改成功才算抢到。"""
    next_push = compute_next_push_after_fire(job, fire_at)
    if next_push is None:
        return await claim_oneshot_run(job, fire_at)
    return await _claim_update(
        job_id=job.id,
        fire_at=fire_at,
        next_run_at=next_push,
    )


async def claim_oneshot_run(job: CronJob, fire_at: datetime) -> bool:
    """一次性任务：抢到后标成过期并停用，不删掉这一行。"""
    return await _claim_expire(job_id=job.id, fire_at=fire_at)


async def _claim_update(
    *,
    job_id: str,
    fire_at: datetime,
    next_run_at: datetime,
) -> bool:
    fire_db = datetime_to_db_value(fire_at)
    next_db = datetime_to_db_value(next_run_at)
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET next_run_at = :next_run_at,
            updated_at = :updated_at
        WHERE job_id = :job_id
          AND next_run_at = :fire_at
          AND enabled = :enabled
          AND expired = :expired
    """
    params = {
        "job_id": job_id,
        "fire_at": fire_db,
        "next_run_at": next_db,
        "updated_at": now_db,
        "enabled": True,
        "expired": False,
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
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET expired = :set_expired,
            enabled = :set_enabled,
            updated_at = :updated_at
        WHERE job_id = :job_id
          AND next_run_at = :fire_at
          AND enabled = :enabled
          AND expired = :expired
    """
    params = {
        "job_id": job_id,
        "fire_at": fire_db,
        "updated_at": now_db,
        "set_expired": True,
        "set_enabled": False,
        "enabled": True,
        "expired": False,
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


async def _update_next_run_at_only(job_id: str, next_run_at: datetime) -> bool:
    next_db = datetime_to_db_value(next_run_at)
    now_db = datetime_to_db_value(datetime.now(dt_timezone.utc))
    params: dict[str, Any] = {
        "job_id": job_id,
        "next_run_at": next_db,
        "updated_at": now_db,
    }
    sql = f"""
        UPDATE {_CRON_JOB_TABLE}
        SET next_run_at = :next_run_at,
            updated_at = :updated_at
        WHERE job_id = :job_id
    """
    rows = await _execute_claim_sql(sql, params)
    return rows == 1


async def _execute_claim_sql(sql: str, params: dict[str, Any]) -> int:
    from sqlalchemy import text

    from gateway.storage.backends.db.bool_codec import (
        normalize_boolean_columns_for_write,
    )
    from gateway.storage.backends.db.datetime_codec import (
        normalize_datetime_columns_for_write,
    )
    from gateway.enterprise.db.database import Database

    handler = await Database.current().ensure_ready(log_prefix="cron_db_schedule")
    params = normalize_datetime_columns_for_write(handler, _CRON_JOB_TABLE, params)
    params = normalize_boolean_columns_for_write(handler, _CRON_JOB_TABLE, params)
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
