# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Cron scheduler activation tests (standalone vs active-standby PRIMARY/STANDBY)。

仅通过公开 API（is_active / set_active / start / stop / reload / trigger_run_now、
注入的 agent_client 桩、store API）观测行为，不访问 CronSchedulerService 受保护成员。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import FileCronJobStore
from jiuwenswarm.common.schema.agent import AgentResponse


class _StubAgentClient:
    """可观测的 agent 客户端桩：可阻塞、可记录调用、可感知 CancelledError。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.block: bool = False

    async def send_request(self, envelope: Any) -> AgentResponse:
        self.calls.append(envelope)
        self.started.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return AgentResponse(
            request_id=getattr(envelope, "request_id", None) or "",
            channel_id=getattr(envelope, "channel", None)
            or getattr(envelope, "channel_id", None)
            or "",
            ok=True,
            payload={"content": "ok"},
        )

    async def send_request_stream(self, envelope: Any):  # pragma: no cover - unused
        if False:
            yield envelope


class _StubMessageHandler:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish_robot_messages(self, msg: Any) -> None:
        self.published.append(msg)


def _make_scheduler(
    tmp_path,
) -> tuple[CronSchedulerService, FileCronJobStore, _StubAgentClient]:
    store = FileCronJobStore(path=tmp_path / "cron_jobs.json")
    agent_client = _StubAgentClient()
    scheduler = CronSchedulerService(
        store=store,
        agent_client=agent_client,  # type: ignore[arg-type]
        message_handler=_StubMessageHandler(),  # type: ignore[arg-type]
    )
    return scheduler, store, agent_client


def _one_shot_cron_expr(at_dt: datetime) -> str:
    """7 段 Quartz cron（second minute hour day month dow year），本地校验要求 dow 用 ?。"""
    return (
        f"{at_dt.second} {at_dt.minute} {at_dt.hour} {at_dt.day} {at_dt.month} "
        f"? {at_dt.year}"
    )


@pytest.mark.asyncio
async def test_standalone_default_is_active(tmp_path) -> None:
    """Standalone 模式（默认）：`is_active()` 为 True，与改动前行为一致。"""
    scheduler, _, _ = _make_scheduler(tmp_path)
    assert scheduler.is_active() is True


@pytest.mark.asyncio
async def test_set_active_is_idempotent(tmp_path) -> None:
    scheduler, _, _ = _make_scheduler(tmp_path)

    scheduler.set_active(True)
    assert scheduler.is_active() is True

    scheduler.set_active(False)
    assert scheduler.is_active() is False

    scheduler.set_active(False)  # no-op
    assert scheduler.is_active() is False

    scheduler.set_active(True)
    assert scheduler.is_active() is True


@pytest.mark.asyncio
async def test_set_active_false_cancels_in_flight_run(tmp_path) -> None:
    """STANDBY 转换：正在跑的 agent 调用应被取消。"""
    scheduler, store, agent_client = _make_scheduler(tmp_path)
    agent_client.block = True

    job = await store.create_job(
        name="t",
        cron_expr="*/5 * * * *",  # 远期触发，避免与 trigger_run_now 抢
        timezone="UTC",
        description="t",
        targets="web",
        wake_offset_seconds=0,
    )

    await scheduler.start()
    try:
        await scheduler.trigger_run_now(job.id)
        await asyncio.wait_for(agent_client.started.wait(), timeout=2.0)

        scheduler.set_active(False)
        assert scheduler.is_active() is False

        await asyncio.wait_for(agent_client.cancelled.wait(), timeout=2.0)
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_inactive_scheduler_does_not_execute_jobs(tmp_path) -> None:
    """STANDBY 期间：到期任务不会被实际执行（不调用 agent_client）。"""
    scheduler, store, agent_client = _make_scheduler(tmp_path)

    run_at = datetime.now(tz=ZoneInfo("UTC")) + timedelta(seconds=1)
    await store.create_job(
        name="t",
        cron_expr=_one_shot_cron_expr(run_at),
        timezone="UTC",
        description="t",
        targets="web",
        wake_offset_seconds=0,
    )

    scheduler.set_active(False)
    await scheduler.start()
    try:
        # 即便 cron 时间到了，inactive 状态下也不会触发 agent 调用
        await asyncio.sleep(2.0)
        assert not agent_client.started.is_set()
        assert agent_client.calls == []
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_promotion_reloads_and_executes_jobs(tmp_path) -> None:
    """STANDBY → PRIMARY：reload 后激活，新加入 store 的任务能被正常触发。"""
    scheduler, store, agent_client = _make_scheduler(tmp_path)

    scheduler.set_active(False)
    await scheduler.start()
    try:
        # 模拟另一个 PRIMARY 在本实例处于 STANDBY 期间写入 store
        run_at = datetime.now(tz=ZoneInfo("UTC")) + timedelta(seconds=5)
        await store.create_job(
            name="t",
            cron_expr=_one_shot_cron_expr(run_at),
            timezone="UTC",
            description="t",
            targets="web",
            wake_offset_seconds=0,
        )

        # leader 回调约定的顺序：先 reload，再 set_active(True)
        await scheduler.reload()
        scheduler.set_active(True)

        await asyncio.wait_for(agent_client.started.wait(), timeout=10.0)
        assert len(agent_client.calls) >= 1
    finally:
        await scheduler.stop()
