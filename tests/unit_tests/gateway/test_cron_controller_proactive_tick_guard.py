# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""proactive.tick 内部模式的创建/修改准入（#5128）。

proactive.tick 由 proactive_cron_sync 以固定 id 自动注册、由设置开关驱动启停；
后端保护按 mode 拦截而前端只按固定 id 识别系统任务，用户创建的同模式任务会
落入"停不掉也删不掉"的缝隙。入口层（controller.create_job/update_job）必须
拒绝用户路径的该模式，仅放行内部 sync 的固定 id 注册。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.proactive_cron_sync import (
    DEFAULT_CRON_EXPR,
    PROACTIVE_JOB_ID,
)
from jiuwenswarm.gateway.cron.store import CronJobStore


def _make_controller(tmp_path) -> tuple[CronController, CronJobStore]:
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    scheduler = SimpleNamespace(
        reload=AsyncMock(),
        project_execution_allowed=AsyncMock(return_value=True),
    )
    return CronController(store=store, scheduler=scheduler), store


def _job_params(**overrides) -> dict:
    params: dict = {
        "name": "job",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "reminder",
        "targets": "web",
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_create_job_rejects_proactive_tick_mode(tmp_path):
    """用户路径创建 mode=proactive.tick 的任务 → 入口即拒，不落库。"""
    controller, store = _make_controller(tmp_path)

    with pytest.raises(ValueError, match="proactive.tick"):
        await controller.create_job(_job_params(mode="proactive.tick", name="模式_proactive.tick"))

    assert await store.list_jobs() == []


@pytest.mark.asyncio
async def test_create_job_allows_auto_registered_proactive_tick_job(
    tmp_path,
):
    """proactive_cron_sync 的固定 id 注册不受影响（内部路径唯一合法入口）。"""
    controller, store = _make_controller(tmp_path)

    job = await controller.create_job(
        _job_params(
            id=PROACTIVE_JOB_ID,
            name="主动推荐定时检查",
            cron_expr=DEFAULT_CRON_EXPR,
            mode="proactive.tick",
        )
    )

    assert job["id"] == PROACTIVE_JOB_ID
    assert job["mode"] == "proactive.tick"
    assert await store.get_job(PROACTIVE_JOB_ID) is not None


@pytest.mark.asyncio
async def test_update_job_rejects_switching_to_proactive_tick(tmp_path):
    """把既有任务改成 proactive.tick → 拒绝，任务保持原模式。"""
    controller, store = _make_controller(tmp_path)
    job = await controller.create_job(_job_params(mode="agent"))

    with pytest.raises(ValueError, match="proactive.tick"):
        await controller.update_job(job["id"], {"mode": "proactive.tick"})

    stored = await store.get_job(job["id"])
    assert stored.mode == "agent.work.normal"


@pytest.mark.asyncio
async def test_update_job_allows_normal_mode_change(tmp_path):
    """普通模式间切换不受 guard 影响。"""
    controller, _ = _make_controller(tmp_path)
    job = await controller.create_job(_job_params(mode="agent"))

    updated = await controller.update_job(job["id"], {"mode": "team"})

    assert updated["mode"] == "team.work.normal"
