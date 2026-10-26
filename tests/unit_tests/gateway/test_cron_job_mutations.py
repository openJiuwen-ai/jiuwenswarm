# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Cron job mutations unit tests."""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.cron_job_mutations import apply_cron_job_patch, build_new_cron_job
from jiuwenswarm.gateway.cron.models import CronJob


def test_build_new_cron_job_defaults() -> None:
    job = build_new_cron_job(
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="desc",
        targets="web",
    )
    assert job.id
    assert job.wake_offset_seconds == 0
    assert job.mode == "agent"
    assert job.enabled is True


def test_apply_cron_job_patch_clears_expired_on_reenable() -> None:
    job = CronJob(
        id="j1",
        name="n",
        enabled=False,
        expired=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
    )
    updated = apply_cron_job_patch(job, {"enabled": True})
    assert updated.enabled is True
    assert updated.expired is False


@pytest.mark.asyncio
async def test_apply_cron_job_patch_invalid_wake_offset() -> None:
    job = CronJob(
        id="j1",
        name="n",
        enabled=True,
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
    )
    with pytest.raises(ValueError, match="wake_offset_seconds"):
        apply_cron_job_patch(job, {"wake_offset_seconds": "bad"})
