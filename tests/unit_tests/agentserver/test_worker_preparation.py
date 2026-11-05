"""Optional first-use preparation and compatible, truthful Worker readiness."""
import asyncio
import threading

import pytest

from jiuwenswarm.server.lifecycle import Readiness, ReadinessState
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.worker.gateway_bridge import ConnHolder
from jiuwenswarm.server.worker.preparation import CapabilityUnavailableError, PreparationRegistry
from jiuwenswarm.server.worker.signals import RuntimeSignalSink


async def checkpoint():
    reached = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(reached.set_result, None)
    await reached


@pytest.mark.asyncio
async def test_background_preparation_is_bounded_and_explicit_first_use_has_priority():
    registry = PreparationRegistry(background_concurrency=1)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_network_warmup():
        calls.append("background")
        entered.set()
        await release.wait()
        return "network ready"

    registry.register("network", slow_network_warmup)
    registry.register("unrelated", lambda: calls.append("unrelated"))
    registry.register("requested", lambda: calls.append("requested"))
    registry.mark_minimum_ready()
    await entered.wait()
    await checkpoint()
    assert calls == ["background"]
    token = registry.pause_background()
    await asyncio.wait_for(registry.ensure("requested"), timeout=1)
    assert calls == ["background", "requested"]
    release.set()
    await registry.ensure("network")
    await checkpoint()
    assert "unrelated" not in calls
    registry.resume_background(token)
    await registry.start("unrelated")
    assert calls == ["background", "requested", "unrelated"]
    await registry.close()


@pytest.mark.asyncio
async def test_foreground_priority_is_released_only_when_every_owner_resumes():
    registry = PreparationRegistry()
    calls = []
    first = registry.pause_background()
    second = registry.pause_background()
    registry.register("background", lambda: calls.append("background"))
    registry.mark_minimum_ready()
    registry.resume_background(first)
    registry.resume_background(first)
    await checkpoint()
    assert calls == []
    registry.resume_background(second)
    await registry.start("background")
    assert calls == ["background"]
    await registry.close()


@pytest.mark.asyncio
async def test_sync_preparation_runs_off_loop_and_preserves_async_factory_loop(monkeypatch):
    from jiuwenswarm.server.worker import preparation

    main_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    threads = []

    def blocking_import(name):
        assert name == "optional.heavy"
        threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), "test did not release blocking import"

    monkeypatch.setattr(preparation.importlib, "import_module", blocking_import)
    registry = PreparationRegistry()

    async def factory():
        assert threading.get_ident() == main_thread
        return "usable"

    registry.register("module", factory, imports=("optional.heavy",))
    registry.register("sync", threading.get_ident, offload=True)
    registry.mark_minimum_ready()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # An import is genuinely blocked in another thread; IPC-loop callbacks
        # and unrelated foreground preparation must still progress.
        await asyncio.wait_for(checkpoint(), .5)
        assert await asyncio.wait_for(registry.ensure("sync"), .5) != main_thread
        release.set()
        assert await registry.ensure("module") == "usable"
        assert threads and all(thread != main_thread for thread in threads)
    finally:
        release.set()
        await registry.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_background_admission_waiters():
    registry = PreparationRegistry()
    registry.pause_background()
    registry.register("blocked", lambda: pytest.fail("background was paused"))
    registry.mark_minimum_ready()
    task = registry.start("blocked")
    await checkpoint()
    await registry.close()
    assert task.cancelled()
    assert registry.snapshot()["blocked"] == "failed"


@pytest.mark.asyncio
async def test_optional_start_waits_for_minimum_and_disabled_factory_never_runs():
    changes = []
    registry = PreparationRegistry(on_change=lambda states, warm: changes.append((states, warm)))
    called = []

    async def prepare():
        called.append("optional")
        return "usable"

    registry.register("optional", prepare)
    registry.register("off", lambda: pytest.fail("disabled factory must not run"), enabled=False)
    task = registry.start("optional")
    assert registry.start("optional") is task
    await checkpoint()
    assert called == []
    assert not registry.fully_warm
    assert not registry.first_pass_settled
    with pytest.raises(CapabilityUnavailableError, match="disabled"):
        await registry.ensure("off")
    registry.mark_minimum_ready()
    assert await registry.ensure("optional") == "usable"
    assert called == ["optional"]
    assert registry.snapshot() == {"optional": "ready", "off": "disabled"}
    assert registry.fully_warm and registry.first_pass_settled
    assert changes[-1] == ({"optional": "ready", "off": "disabled"}, True)
    await registry.close()


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_singleflight_preparation():
    registry = PreparationRegistry()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def factory():
        calls.append(object())
        started.set()
        await release.wait()
        return calls[-1]

    registry.register("optional", factory)
    registry.mark_minimum_ready()
    abandoned = asyncio.create_task(registry.ensure("optional"))
    await started.wait()
    kept = asyncio.create_task(registry.ensure("optional"))
    await checkpoint()
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    assert registry.snapshot() == {"optional": "pending"}
    release.set()
    assert await kept is calls[0]
    assert len(calls) == 1
    assert registry.fully_warm
    await registry.close()


@pytest.mark.asyncio
async def test_background_start_preserves_false_return_but_reports_failed():
    registry = PreparationRegistry()
    registry.register("best-effort", lambda: False)
    task = registry.start("best-effort")
    registry.mark_minimum_ready()
    assert await task is False
    assert registry.snapshot() == {"best-effort": "failed"}
    assert registry.first_pass_settled
    assert not registry.fully_warm
    with pytest.raises(CapabilityUnavailableError, match="preparation failed"):
        await registry.ensure("best-effort")
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, "exception"])
async def test_failed_capability_is_not_reported_ready_or_rebuilt_by_another_caller(result):
    registry = PreparationRegistry()
    calls = []

    async def factory():
        calls.append("failed")
        if result == "exception":
            raise ValueError("optional connection unavailable")
        return result

    registry.register("failed", factory)
    registry.register("base", lambda: "base is usable")
    registry.mark_minimum_ready()
    for _ in range(2):
        with pytest.raises((ValueError, CapabilityUnavailableError)):
            await registry.ensure("failed")
    assert await registry.ensure("base") == "base is usable"
    assert calls == ["failed"]
    assert registry.snapshot() == {"failed": "failed", "base": "ready"}
    assert registry.first_pass_settled
    assert not registry.fully_warm
    await registry.close()


@pytest.mark.asyncio
async def test_first_use_pass_does_not_await_a_capability_daemon_lifetime():
    registry = PreparationRegistry()
    daemon_stop = asyncio.Event()
    daemons = []

    async def factory():
        daemons.append(asyncio.create_task(daemon_stop.wait()))
        return "service initialized"

    registry.register("first-use", factory, warm=False)
    registry.mark_minimum_ready()
    assert daemons == []
    assert registry.fully_warm
    try:
        assert await registry.ensure("first-use") == "service initialized"
        assert not daemons[0].done()
        assert registry.fully_warm
        await registry.close()
        # Service lifetimes are still closed by their existing application owner.
        assert not daemons[0].done()
    finally:
        daemon_stop.set()
        await asyncio.gather(*daemons)


@pytest.mark.asyncio
async def test_close_cancels_pending_preparation_and_releases_minimum_waiters():
    registry = PreparationRegistry()
    registry.register("gated", lambda: pytest.fail("minimum was never reached"))
    ensuring = asyncio.create_task(registry.ensure("gated"))
    minimum = asyncio.create_task(registry.wait_minimum_ready())
    await checkpoint()
    await registry.close()
    with pytest.raises(asyncio.CancelledError):
        await ensuring
    with pytest.raises(RuntimeError, match="closed"):
        await minimum
    with pytest.raises(RuntimeError, match="closed"):
        await registry.ensure("gated")
    assert registry.snapshot() == {"gated": "failed"}
    assert not registry.fully_warm


@pytest.mark.asyncio
async def test_close_cancels_unstarted_factory_and_does_not_leave_pending_state():
    registry = PreparationRegistry()
    registry.register("unstarted", lambda: pytest.fail("factory must not enter"))
    task = registry.start("unstarted")
    await registry.close()
    assert task.cancelled()
    assert registry.snapshot() == {"unstarted": "failed"}


@pytest.mark.asyncio
async def test_observer_failure_does_not_change_actual_preparation_outcome():
    observed = []

    def broken_observer(_states, _warm):
        observed.append((_states, _warm))
        raise RuntimeError("status observer unavailable")

    registry = PreparationRegistry(on_change=broken_observer)
    registry.register("optional", lambda: "usable")
    registry.mark_minimum_ready()
    assert await registry.ensure("optional") == "usable"
    assert registry.snapshot() == {"optional": "ready"}
    assert registry.fully_warm
    assert observed[-1] == ({"optional": "ready"}, True)
    await registry.close()


@pytest.mark.asyncio
async def test_base_admission_does_not_claim_execution_minimum_before_prepared_entry():
    holder = ConnHolder()
    admitted = []

    async def on_ready():
        admitted.append("base")

    signals = RuntimeSignalSink(holder, on_ready=on_ready)
    signals.ipc_ready()
    signals.runtime_base_ready()
    assert holder.last_readiness["agent_ready"] is True
    assert holder.last_readiness["runtime_phase"] == "IPC_READY"
    signals.capabilities({"optional": "ready"}, fully_warm=True)
    assert holder.last_readiness["runtime_phase"] == "IPC_READY"
    with pytest.raises(RuntimeError, match="minimum is not ready"):
        signals.fully_warm()
    await checkpoint()
    assert admitted == ["base"]
    signals.execution_min_ready()
    assert holder.last_readiness["runtime_phase"] == "FULLY_WARM"
    await checkpoint()
    assert admitted == ["base"]
    signals.warmup_retry("base retry")
    signals.capabilities({"optional": "ready"}, fully_warm=True)
    signals.runtime_base_ready()
    assert holder.last_readiness["runtime_phase"] == "IPC_READY"
    signals.execution_min_ready()
    assert holder.last_readiness["runtime_phase"] == "FULLY_WARM"
    await checkpoint()
    assert admitted == ["base", "base"]
    signals.failed("required preparation unavailable")
    with pytest.raises(RuntimeError, match="minimum is not ready"):
        signals.fully_warm()


@pytest.mark.asyncio
async def test_capability_failure_degrades_minimum_without_claiming_fully_warm():
    holder = ConnHolder()
    released = []

    async def on_ready():
        released.append("minimum")

    signals = RuntimeSignalSink(holder, on_ready=on_ready)
    signals.ipc_ready()
    registry = PreparationRegistry(
        on_change=lambda states, warm: signals.capabilities(states, fully_warm=warm),
    )
    registry.register("optional", lambda: False)
    assert holder.last_readiness["agent_ready"] is False
    signals.execution_min_ready()
    registry.mark_minimum_ready()
    with pytest.raises(CapabilityUnavailableError):
        await registry.ensure("optional")
    signals.agent_ready()  # Legacy callback does not start admission twice.
    await checkpoint()
    message = holder.last_readiness
    assert released == ["minimum"]
    assert message["state"] == "DEGRADED"
    assert message["agent_ready"] is True
    assert message["runtime_phase"] == "EXECUTION_MIN_READY"
    assert message["capability_readiness"] == {"optional": "failed"}
    with pytest.raises(RuntimeError, match="not fully warm"):
        signals.fully_warm()
    await registry.close()


def test_optional_callbacks_cannot_hide_a_terminal_core_failure():
    holder = ConnHolder()
    signals = RuntimeSignalSink(holder)
    signals.ipc_ready()
    signals.failed("required model preparation failed")
    signals.capabilities({"optional": "ready"}, fully_warm=True)
    signals.degraded("optional degradation must not replace core failure")
    assert holder.last_readiness["state"] == "FAILED"
    assert holder.last_readiness["agent_ready"] is False
    assert holder.last_readiness["runtime_phase"] == "IPC_READY"
    assert holder.last_readiness["reason"] == "required model preparation failed"


def test_readiness_optional_progress_does_not_make_core_ready_and_snapshots_are_independent():
    readiness = Readiness()
    assert "runtime_phase" not in readiness.snapshot()
    assert "capability_readiness" not in readiness.snapshot()
    readiness.mark_runtime_warming()
    readiness.note_runtime_progress(runtime_phase="FULLY_WARM", capability_readiness={"optional": "ready"})
    assert readiness.snapshot()["agent_ready"] is False
    snapshot = readiness.snapshot()
    snapshot["capability_readiness"]["optional"] = "failed"
    assert readiness.snapshot()["capability_readiness"] == {"optional": "ready"}
    readiness.mark_runtime_warming()
    assert "runtime_phase" not in readiness.snapshot()
    readiness.note_runtime_progress(runtime_phase="unknown", capability_readiness={"bad": {}, 1: "ready"})
    assert "runtime_phase" not in readiness.snapshot()
    assert readiness.snapshot()["capability_readiness"] == {}


def test_supervisor_preserves_legacy_readiness_and_forwards_truthful_optional_progress():
    readiness = Readiness()
    supervisor = RuntimeSupervisor(readiness)
    supervisor._apply_readiness({"state": "RUNTIME_WARMING", "runtime_phase": "IPC_READY",
                                 "capability_readiness": {"optional": "pending"}})
    assert readiness.snapshot()["agent_ready"] is False
    supervisor._apply_readiness({"state": "AGENT_READY", "agent_ready": False,
                                 "runtime_phase": "FULLY_WARM"})
    assert readiness.state is ReadinessState.RUNTIME_WARMING
    supervisor._apply_readiness({"state": "AGENT_READY"})  # Old Worker field set remains accepted.
    assert readiness.state is ReadinessState.AGENT_READY
    supervisor._apply_readiness({"state": "DEGRADED", "agent_ready": True,
                                 "runtime_phase": "EXECUTION_MIN_READY",
                                 "capability_readiness": {"optional": "failed"}})
    snapshot = readiness.snapshot()
    assert snapshot["agent_ready"] is True
    assert snapshot["state"] == "DEGRADED"
    assert snapshot["runtime_phase"] == "EXECUTION_MIN_READY"
    assert snapshot["capability_readiness"] == {"optional": "failed"}
