# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""CronController tenant isolation unit tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronJob


def _job(**kwargs: Any) -> CronJob:
    base = dict(
        id="job-1",
        name="n",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        targets="web",
        group_id="g1",
        bot_id="b1",
        user_id="u1",
    )
    base.update(kwargs)
    return CronJob(**base)


@pytest.fixture
def controller() -> CronController:
    CronController.reset_instance()
    store = MagicMock()
    scheduler = MagicMock()
    scheduler.reload = AsyncMock()
    scheduler.trigger_run_now = AsyncMock(return_value="run-1")
    return CronController.get_instance(store=store, scheduler=scheduler)


@pytest.mark.asyncio
async def test_list_jobs_non_enterprise_unfiltered(controller: CronController) -> None:
    controller._store.list_jobs = AsyncMock(return_value=[_job(), _job(id="job-2", user_id="u2")])
    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=False):
        jobs = await controller.list_jobs()
    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_list_jobs_enterprise_requires_triple(controller: CronController) -> None:
    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=True):
        with pytest.raises(ValueError, match="requires group_id"):
            await controller.list_jobs({})


@pytest.mark.asyncio
async def test_get_job_cross_tenant_not_found(controller: CronController) -> None:
    controller._store.get_job = AsyncMock(return_value=_job())
    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=True):
        found = await controller.get_job(
            "job-1",
            group_id="g1",
            bot_id="b1",
            user_id="other",
        )
    assert found is None


@pytest.mark.asyncio
async def test_create_job_enterprise_rejects_without_jid(
    controller: CronController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    with patch("jiuwenswarm.gateway.cron.controller.is_enterprise", return_value=True):
        with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=False):
            with pytest.raises(PermissionError, match="not ready"):
                await controller.create_job(
                    {
                        "name": "n",
                        "cron_expr": "0 9 * * *",
                        "timezone": "Asia/Shanghai",
                        "description": "d",
                        "targets": "web",
                        "group_id": "g1",
                        "bot_id": "b1",
                        "user_id": "u1",
                    }
                )


@pytest.mark.asyncio
async def test_create_job_non_enterprise_does_not_pass_routing_triple(controller: CronController) -> None:
    created = _job()
    controller._store.create_job = AsyncMock(return_value=created)
    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=False):
        with patch("jiuwenswarm.gateway.cron.controller.is_enterprise", return_value=False):
            result = await controller.create_job(
                {
                    "name": "n",
                    "cron_expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "description": "d",
                    "targets": "web",
                    "group_id": "g1",
                    "bot_id": "b1",
                    "user_id": "u1",
                }
            )
    assert result["id"] == "job-1"
    kwargs = controller._store.create_job.await_args.kwargs
    assert "group_id" not in kwargs
    assert "bot_id" not in kwargs
    assert "user_id" not in kwargs


@pytest.mark.asyncio
async def test_create_job_enterprise_passes_routing_triple(controller: CronController) -> None:
    created = _job()
    controller._store.create_job = AsyncMock(return_value=created)
    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=True):
        with patch("jiuwenswarm.gateway.cron.controller.is_enterprise", return_value=True):
            result = await controller.create_job(
                {
                    "name": "n",
                    "cron_expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "description": "d",
                    "targets": "web",
                    "group_id": "g1",
                    "bot_id": "b1",
                    "user_id": "u1",
                }
            )
    assert result["id"] == "job-1"
    kwargs = controller._store.create_job.await_args.kwargs
    assert kwargs["group_id"] == "g1"
    assert kwargs["bot_id"] == "b1"
    assert kwargs["user_id"] == "u1"


@pytest.mark.asyncio
async def test_create_job_enterprise_clamps_wake_offset_by_horizon(controller: CronController) -> None:
    """企业创建：距下次触发仅约 90s 时，wake_offset=300 收敛到剩余秒数。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    push_at = datetime.now(tz=tz) + timedelta(seconds=90)
    created = _job(wake_offset_seconds=90)
    controller._store.create_job = AsyncMock(return_value=created)

    with patch("jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled", return_value=True):
        with patch("jiuwenswarm.gateway.cron.controller.is_enterprise", return_value=True):
            with patch(
                "jiuwenswarm.gateway.cron.controller._cron_next_push_dt",
                return_value=push_at,
            ):
                await controller.create_job(
                    {
                        "name": "吃饭提醒",
                        "cron_expr": "0 0 1 1 * 0 2099",
                        "timezone": "Asia/Shanghai",
                        "description": "吃饭",
                        "targets": "web",
                        "wake_offset_seconds": 300,
                        "delete_after_run": True,
                        "group_id": "g1",
                        "bot_id": "b1",
                        "user_id": "u1",
                    }
                )

    kwargs = controller._store.create_job.await_args.kwargs
    # floor(delay) 可能因调用耗时略小于 90，但必须远小于默认 300
    assert kwargs["wake_offset_seconds"] <= 90
    assert kwargs["wake_offset_seconds"] >= 85
