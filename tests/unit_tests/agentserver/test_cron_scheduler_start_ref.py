from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    CronRuntimeBridge,
    _CronToolsCronBackend,
)

_LOGGER_NAME = "jiuwenswarm.agents.harness.common.tools.cron.cron_runtime"


class _FakeCronTools:
    """Minimal stand-in for CronTools exposing only ``ensure_scheduler``."""

    def __init__(
        self,
        *,
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.start_called = asyncio.Event()
        self._gate = gate
        self._error = error

    async def ensure_scheduler(self) -> None:
        self.start_called.set()
        if self._gate is not None:
            await self._gate.wait()
        if self._error is not None:
            raise self._error


def _bridge_with(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeCronTools
) -> CronRuntimeBridge:
    backend = _CronToolsCronBackend(fake)
    bridge = CronRuntimeBridge()
    monkeypatch.setattr(
        bridge, "get_backend", lambda service_id=None, agent_id=None: backend
    )
    return bridge


@pytest.mark.asyncio
async def test_background_start_task_is_referenced_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：create_task 必须被强引用（修复前是 fire-and-forget，可被 GC）。"""
    gate = asyncio.Event()
    fake = _FakeCronTools(gate=gate)
    bridge = _bridge_with(monkeypatch, fake)

    bridge.ensure_scheduler_started()

    assert len(bridge._scheduler_start_tasks) == 1, (
        "scheduler-start task must be strongly referenced "
        "(fire-and-forget regression)"
    )
    # 主动触发 GC：被强引用的任务不应被回收。
    gc.collect()
    assert len(bridge._scheduler_start_tasks) == 1, (
        "pending task must survive an explicit GC"
    )

    await asyncio.wait_for(fake.start_called.wait(), timeout=1.0)
    gate.set()
    for _ in range(200):
        if not bridge._scheduler_start_tasks:
            break
        await asyncio.sleep(0)
    assert bridge._scheduler_start_tasks == set(), (
        "task reference must be released once it completes"
    )


@pytest.mark.asyncio
async def test_background_start_failure_is_logged_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """回归：后台启动失败必须落到日志，而不是静默的未取回异常。"""
    fake = _FakeCronTools(error=RuntimeError("scheduler boom"))
    bridge = _bridge_with(monkeypatch, fake)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        bridge.ensure_scheduler_started()
        await asyncio.wait_for(fake.start_called.wait(), timeout=1.0)
        for _ in range(200):
            if not bridge._scheduler_start_tasks:
                break
            await asyncio.sleep(0)

    assert "background scheduler start failed" in caplog.text


def test_sync_fallback_runs_when_loop_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无运行循环时仍走同步启动分支（行为保持不变）。"""
    fake = _FakeCronTools()
    bridge = _bridge_with(monkeypatch, fake)

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "get_event_loop", lambda: loop)
    try:
        bridge.ensure_scheduler_started()
    finally:
        loop.close()

    assert fake.start_called.is_set(), "sync fallback must await ensure_scheduler"


@pytest.mark.asyncio
async def test_concurrent_ensure_scheduler_starts_scheduler_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """回归：并发 ensure_scheduler 只能启动一次调度器（启动临界区单飞）。

    无锁时两个协程会各自越过状态检查并双 start，覆盖 self._scheduler 泄漏旧实例。
    """
    import jiuwenswarm.agents.harness.common.tools.cron.cron_tools as ct
    from jiuwenswarm.server.runtime import cron_local_runtime as clr

    gate = asyncio.Event()
    start_calls = {"n": 0}

    class _FakeScheduler:
        def __init__(self, **_kwargs) -> None:
            self.started = False

        def is_running(self) -> bool:
            return self.started

        async def start(self) -> None:
            start_calls["n"] += 1
            await gate.wait()  # 卡住首次启动，制造并发窗口
            self.started = True

        async def stop(self) -> None:
            self.started = False

    monkeypatch.setattr(ct, "CronSchedulerService", _FakeScheduler)
    monkeypatch.setattr(
        ct, "resolve_cron_jobs_path", lambda *a, **k: tmp_path / "cron_jobs.json"
    )
    monkeypatch.setattr(clr, "resolve_agent_side_cron_deps", lambda **_kw: (None, None))
    monkeypatch.setattr(
        clr.AgentCronRegistry, "is_current", staticmethod(lambda *a, **k: True)
    )

    tools = ct.CronTools()
    monkeypatch.setattr(tools, "_enterprise_ready", lambda: False)

    t1 = asyncio.create_task(tools.ensure_scheduler())
    t2 = asyncio.create_task(tools.ensure_scheduler())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert start_calls["n"] == 1, (
        "only one coroutine may enter the start critical section"
    )

    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert start_calls["n"] == 1, "scheduler must be started exactly once"
    assert r1 is r2 and r1 is not None, "both callers must observe the same scheduler"