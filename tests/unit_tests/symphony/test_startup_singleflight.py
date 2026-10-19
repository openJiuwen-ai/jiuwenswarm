# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Background Flow recovery and first use share one owned preparation."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.symphony.service import SwarmSymphonyService


class Flow:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.candidate = SimpleNamespace(recipe_id="recovered", version=1)

    async def start(self):
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return (self.candidate,)


def runtime_for(flow):
    return SimpleNamespace(flow_engine=flow, aclose=AsyncMock(),
                           submit_evolution=AsyncMock(return_value=SimpleNamespace(new_candidates=())))


@pytest.mark.asyncio
async def test_two_starters_initialize_once_and_claim_candidates_once():
    service, flow = SwarmSymphonyService(), Flow()
    runtime = service._runtime = runtime_for(flow)
    first = asyncio.create_task(service._start_flow(runtime))
    await flow.entered.wait()
    second = asyncio.create_task(service._start_flow(runtime))
    flow.release.set()
    results = await asyncio.gather(first, second)
    assert flow.calls == 1 and sum(len(value) for value in results) == 1
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_caller_keeps_shared_recovery_running():
    service, flow = SwarmSymphonyService(), Flow()
    runtime = service._runtime = runtime_for(flow)
    first = asyncio.create_task(service._start_flow(runtime))
    await flow.entered.wait()
    second = asyncio.create_task(service._start_flow(runtime))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not flow.cancelled and not second.done()
    flow.release.set()
    assert await second == (flow.candidate,)
    assert flow.calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_background_recovery_candidates_reach_first_successful_submission(monkeypatch):
    service, flow = SwarmSymphonyService(), Flow()
    runtime = service._runtime = runtime_for(flow)
    config = SimpleNamespace(enabled=True, evolution=SimpleNamespace(flow=SimpleNamespace(enabled=True)))
    monkeypatch.setattr("jiuwenswarm.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr(service, "_runtime_for", lambda _config: runtime)
    notify = AsyncMock()
    monkeypatch.setattr(service, "_notify_candidate", notify)
    recovery = asyncio.create_task(service.start())
    await flow.entered.wait()
    submission = asyncio.create_task(service.submit_evolution_and_notify(None, {"outcome": "success"},
        session_id="original-session", capture_mode="agent", channel_id="web"))
    await asyncio.sleep(0)
    flow.release.set()
    await asyncio.gather(recovery, submission)
    assert flow.calls == 1
    notify.assert_awaited_once_with(flow.candidate, session_id="original-session", channel_id="web")
    assert service._recovered_candidates == ()
    await service.close()


@pytest.mark.asyncio
async def test_failed_flow_prepare_can_retry_on_next_actual_use():
    service = SwarmSymphonyService()
    flow = SimpleNamespace(start=AsyncMock(side_effect=[RuntimeError("first failure"), ()]))
    runtime = service._runtime = runtime_for(flow)
    with pytest.raises(RuntimeError, match="first failure"):
        await service._start_flow(runtime)
    assert await service._start_flow(runtime) == ()
    assert flow.start.await_count == 2
    await service.close()


@pytest.mark.asyncio
async def test_owner_close_cancels_preparation_before_runtime_close():
    service, flow = SwarmSymphonyService(), Flow()
    runtime = service._runtime = runtime_for(flow)

    async def close():
        assert flow.cancelled

    runtime.aclose = close
    task = asyncio.create_task(service._start_flow(runtime))
    await flow.entered.wait()
    await service.close()
    assert flow.cancelled
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancelled_close_waiter_keeps_single_owned_drain():
    service, flow = SwarmSymphonyService(), Flow()
    runtime = service._runtime = runtime_for(flow)
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    close_calls = 0

    async def close():
        nonlocal close_calls
        close_calls += 1
        assert flow.cancelled
        close_entered.set()
        await close_release.wait()

    runtime.aclose = close
    start = asyncio.create_task(service._start_flow(runtime))
    await flow.entered.wait()
    first = asyncio.create_task(service.close())
    await close_entered.wait()
    second = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert service._closing and not second.done() and close_calls == 1
    with pytest.raises(RuntimeError, match="closing"):
        await service._start_flow(runtime)
    close_release.set()
    await second
    assert not service._closing and service._runtime is None and close_calls == 1
    with pytest.raises(asyncio.CancelledError):
        await start

    # Controlled embedding may start the service again after orderly close.
    replacement = service._runtime = runtime_for(SimpleNamespace(start=AsyncMock(return_value=())))
    assert await service._start_flow(replacement) == ()
    await service.close()
    replacement.aclose.assert_awaited_once()


def test_importing_toolkit_does_not_load_symphony_service(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = "import sys; from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit; assert 'jiuwenswarm.symphony.service' not in sys.modules; assert SymphonyToolkit().get_tools({'symphony': {'enabled': False}}) == []; assert 'jiuwenswarm.symphony.service' not in sys.modules; print('lazy-service-boundary-ok')"
    env = {**os.environ, "PYTHONPATH": str(root), "JIUWENSWARM_DATA_DIR": str(tmp_path / "data"),
           "JIUWENSWARM_HOME": str(tmp_path / "home")}
    output = subprocess.check_output([sys.executable, "-c", script], cwd=root, env=env,
                                     encoding="utf-8", timeout=45)
    assert "lazy-service-boundary-ok" in output
