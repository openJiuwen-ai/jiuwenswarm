"""Diagnostic cleanup cannot change business results or wait indefinitely."""

import asyncio

import pytest

from jiuwenswarm.agents.harness.team.expert_org import turn_diagnostics as diagnostics


@pytest.mark.asyncio
async def test_failed_sampler_is_consumed_without_raising():
    monitor = diagnostics.OrganizationTurnDiagnostics("t", "s", "r", "org_summary_background")

    async def fail():
        raise RuntimeError("sampler failed")

    task = asyncio.create_task(fail())
    monitor.task = task
    await asyncio.sleep(0)
    await monitor.stop()
    assert monitor.task is None
    assert task.done()
    await monitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_business", [False, True])
async def test_slow_sampler_has_bounded_cleanup_without_swallowing_business_cancellation(monkeypatch, cancel_business):
    monkeypatch.setattr(diagnostics, "_STOP_TIMEOUT_SECONDS", 0.01 if not cancel_business else 10)
    monitor = diagnostics.OrganizationTurnDiagnostics("t", "s", "r", "org_summary_background")
    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()

    sampler = asyncio.create_task(slow())
    monitor.task = sampler
    await started.wait()
    cleanup = asyncio.create_task(monitor.stop())
    try:
        await cancelling.wait()
        if cancel_business:
            cleanup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cleanup
        else:
            await asyncio.wait_for(cleanup, timeout=0.5)
            assert not sampler.done()
    finally:
        release.set()
        await sampler

