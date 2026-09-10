# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from zoneinfo import ZoneInfo

from jiuwenswarm.gateway.cron.db_schedule import (
    claim_oneshot_run,
    claim_periodic_run,
    compute_next_push_dt,
    datetime_to_db_value,
    fire_at_from_run_id,
    is_oneshot_job,
    next_run_at_to_datetime,
)
from jiuwenswarm.gateway.cron.models import CronJob


def _job(**overrides: object) -> CronJob:
    base = {
        "id": "job-1",
        "name": "test",
        "enabled": True,
        "expired": False,
        "cron_expr": "0 * * * *",
        "timezone": "Asia/Shanghai",
        "description": "desc",
        "targets": "web",
        "delete_after_run": False,
    }
    base.update(overrides)
    return CronJob.from_dict(base)


def test_is_oneshot_job() -> None:
    assert not is_oneshot_job(_job())
    assert is_oneshot_job(_job(delete_after_run=True))


def test_next_run_at_roundtrip_epoch() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    push_dt = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    job = _job(next_run_at=push_dt.timestamp())
    parsed = next_run_at_to_datetime(job)
    assert parsed is not None
    assert int(parsed.timestamp()) == int(push_dt.timestamp())


def test_fire_at_from_run_id() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    push_dt = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    run_id = f"job-1:{int(push_dt.timestamp())}"
    job = _job()
    fire_at = fire_at_from_run_id(run_id, job)
    assert fire_at is not None
    assert int(fire_at.timestamp()) == int(push_dt.timestamp())


@pytest.mark.asyncio
async def test_claim_periodic_success(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job()
    tz = ZoneInfo("Asia/Shanghai")
    # 使用未来整点作为 fire_at：compute_next_push_after_fire 会把过期时间兜底为
    # 当前时间，若 fire_at 是过去时刻会导致 next_run_at 断言随时间漂移。
    fire_at = (datetime.now(tz) + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0,
    )
    next_push = compute_next_push_dt(job.cron_expr, job.timezone, base=fire_at)

    execute = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule._execute_claim_sql",
        execute,
    )

    ok = await claim_periodic_run(job, fire_at)
    assert ok is True
    assert execute.await_count == 1
    sql = execute.await_args.args[0]
    assert "UPDATE cron_job" in sql
    params = execute.await_args.args[1]
    assert params["job_id"] == "job-1"
    assert params["fire_at"] == datetime_to_db_value(fire_at)
    assert params["next_run_at"] == datetime_to_db_value(next_push)


@pytest.mark.asyncio
async def test_claim_periodic_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job()
    tz = ZoneInfo("Asia/Shanghai")
    fire_at = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule._execute_claim_sql",
        AsyncMock(return_value=0),
    )
    ok = await claim_periodic_run(job, fire_at)
    assert ok is False


@pytest.mark.asyncio
async def test_claim_oneshot_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(delete_after_run=True)
    tz = ZoneInfo("Asia/Shanghai")
    fire_at = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    execute = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule._execute_claim_sql",
        execute,
    )
    ok = await claim_oneshot_run(job, fire_at)
    assert ok is True
    sql = execute.await_args.args[0]
    assert "UPDATE cron_job" in sql
    assert "expired = 1" in sql
    assert "enabled = 0" in sql
    assert "DELETE FROM cron_job" not in sql
    params = execute.await_args.args[1]
    assert params["fire_at"] == datetime_to_db_value(fire_at)
    assert params["last_run_at"] == datetime_to_db_value(fire_at)


@pytest.mark.asyncio
async def test_scheduler_claim_wake_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService

    tz = ZoneInfo("Asia/Shanghai")
    push_dt = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    job = _job(next_run_at=push_dt.timestamp())
    run_id = f"{job.id}:{int(push_dt.timestamp())}"

    store = MagicMock()
    svc = CronSchedulerService(
        store=store,
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_use_db_schedule", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule.claim_periodic_run",
        claim,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule.fire_at_from_run_id",
        lambda _rid, _job: push_dt,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule.is_oneshot_job",
        lambda _job: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule.compute_next_push_after_fire",
        lambda _job, _fire: push_dt,
    )

    ok = await svc._claim_wake(job, run_id)
    assert ok is True
    assert run_id in svc._claimed_run_ids
    claim.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_claim_wake_bypasses_db_for_manual_run_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.gateway.cron.models import CronRunState
    from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService

    tz = ZoneInfo("Asia/Shanghai")
    push_dt = datetime(2026, 8, 31, 12, 0, 0, tzinfo=tz)
    job = _job(next_run_at=push_dt.timestamp() + 3600)
    run_id = f"{job.id}:{int(datetime.now(tz=tz).timestamp())}"

    store = MagicMock()
    svc = CronSchedulerService(
        store=store,
        agent_client=MagicMock(),
        message_handler=MagicMock(),
    )
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "_use_db_schedule", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.db_schedule.claim_periodic_run",
        claim,
    )
    svc._runs[run_id] = CronRunState(
        run_id=run_id,
        job_id=job.id,
        wake_at_iso=push_dt.isoformat(),
        push_at_iso=push_dt.isoformat(),
        manual_trigger=True,
    )

    ok = await svc._claim_wake(job, run_id)
    assert ok is True
    assert run_id in svc._claimed_run_ids
    claim.assert_not_awaited()


def test_should_use_db_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.gateway.cron.db_schedule import should_use_db_schedule

    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.enterprise_gate.enterprise_cron_enabled",
        lambda: False,
    )
    assert should_use_db_schedule() is False

    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.enterprise_gate.enterprise_cron_enabled",
        lambda: True,
    )
    monkeypatch.setenv("GATEWAY_DB_HOST", "mysql-headless.default")
    assert should_use_db_schedule() is True
