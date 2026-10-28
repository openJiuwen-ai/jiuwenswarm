# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Constructed Worker ownership survives startup failures and orderly shutdown."""

import asyncio
import gc
import inspect
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.worker import capabilities, lifecycle, runtime_host, service
from jiuwenswarm.server.worker.lifecycle import WorkerRuntime
from jiuwenswarm.server.worker.runtime_bootstrap import bind_runtime_signals


@pytest.mark.asyncio
async def test_stopped_owner_rejects_and_closes_notification_coroutine():
    runtime = WorkerRuntime(server=object(), stop_event=asyncio.Event())
    runtime.stop_event.set()
    invoked = []

    async def notification():
        invoked.append(True)

    coroutine = notification()
    with pytest.raises(RuntimeError, match="Runtime is stopping"):
        runtime.spawn(coroutine, name="late-notification")

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert invoked == []
    assert runtime.background_tasks == set()


@pytest.mark.asyncio
async def test_failed_notification_is_logged_and_retrieved_before_owner_discards_it(monkeypatch, caplog):
    runtime = WorkerRuntime(server=object(), stop_event=asyncio.Event())
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    monkeypatch.setattr(runtime_host.logger, "handlers", [caplog.handler])
    completed = loop.create_future()

    async def notification():
        raise OSError("delivery failed")

    try:
        with caplog.at_level(logging.WARNING, logger=runtime_host.__name__):
            task = runtime.spawn(notification(), name="failed-notification")
            # Waiting on a separate completion future deliberately does not
            # retrieve the Task exception on behalf of its Runtime owner.
            task.add_done_callback(lambda _task: completed.set_result(None))
            await asyncio.wait_for(completed, 1)
            assert task.done()
            assert runtime.background_tasks == set()
            assert "background task failed-notification failed: delivery failed" in caplog.text
            del task
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == []
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_delayed_zen_callback_and_teammate_factory_cannot_restart_stopped_owner(monkeypatch):
    from jiuwenswarm.common import config

    factories = {}
    callbacks = []

    def register(name, factory, *, imports):
        factories[name] = factory

    registry = SimpleNamespace(register=register, start=lambda _name: None)
    server = SimpleNamespace(
        _preparation_registry=registry, reset_model_cache=Mock(), send_push=AsyncMock(),
    )
    runtime = WorkerRuntime(server=server, stop_event=asyncio.Event())
    spawn = Mock(wraps=runtime.spawn)
    teammate = Mock()
    monkeypatch.setattr(runtime, "spawn", spawn)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(capabilities, "_spawn_teammate_bootstrap", teammate)
    monkeypatch.setitem(sys.modules, "jiuwenswarm.server.runtime.opencode_zen", SimpleNamespace(
        register_models_ready_callback=callbacks.append, set_main_event_loop=Mock(),
        warm_zen_free_models=AsyncMock(return_value=True),
    ))

    await capabilities.start_optional_preparation(
        runtime, SimpleNamespace(degraded=Mock()), SimpleNamespace(stage=Mock()),
    )
    assert await factories["zen"]() is True
    assert len(callbacks) == 1
    callbacks[0]()
    server.reset_model_cache.assert_called_once_with()
    spawn.assert_called_once()
    await asyncio.gather(*runtime.background_tasks)
    server.send_push.assert_awaited_once()

    server.reset_model_cache.reset_mock()
    server.send_push.reset_mock()
    spawn.reset_mock()
    runtime.stop_event.set()
    callbacks[0]()
    await factories["teammate"]()

    server.reset_model_cache.assert_not_called()
    server.send_push.assert_not_called()
    spawn.assert_not_called()
    teammate.assert_not_called()
    assert runtime.teammate_task is None
    assert runtime.background_tasks == set()


@pytest.mark.parametrize("legacy", [False, True])
def test_readiness_hooks_preserve_base_and_execution_boundaries_for_compatible_servers(legacy):
    hooks = {}
    signals = SimpleNamespace(
        runtime_base_ready=Mock(), agent_ready=Mock(), warmup_retry=Mock(), failed=Mock(),
        capabilities=Mock(), execution_min_ready=Mock(),
    )

    def bind_current(*, on_ready, on_warmup_retry, on_failed,
                     on_capabilities, on_execution_min_ready):
        hooks.update(on_ready=on_ready, on_warmup_retry=on_warmup_retry, on_failed=on_failed,
                     on_capabilities=on_capabilities, on_execution_min_ready=on_execution_min_ready)

    def bind_legacy(*, on_ready, on_warmup_retry, on_failed):
        hooks.update(on_ready=on_ready, on_warmup_retry=on_warmup_retry, on_failed=on_failed)

    server = SimpleNamespace(set_runtime_lifecycle_hooks=bind_legacy if legacy else bind_current)
    bind_runtime_signals(server, signals)

    hooks["on_ready"]()
    signals.runtime_base_ready.assert_called_once_with()
    signals.agent_ready.assert_not_called()
    signals.execution_min_ready.assert_not_called()
    hooks["on_warmup_retry"]("retry")
    signals.warmup_retry.assert_called_once_with("retry")
    hooks["on_failed"]("startup failed")
    signals.failed.assert_called_once_with("startup failed")
    if not legacy:
        readiness = {"image": "pending"}
        hooks["on_capabilities"](readiness, False)
        signals.capabilities.assert_called_once_with(readiness, fully_warm=False)
        signals.execution_min_ready.assert_not_called()
        hooks["on_execution_min_ready"]()
        signals.execution_min_ready.assert_called_once_with()


@pytest.fixture
def shutdown_resources(monkeypatch):
    """Observe resource barriers without closing process-wide stores or kernel."""
    events = []
    outcomes = {"history": True, "metadata": True}
    flush_barrier = threading.Barrier(2, timeout=2)

    async def unbind():
        events.append("unbind")

    def flush(name, timeout):
        assert timeout > 0
        events.append(name + ".begin")
        # Neither writer barrier may serialize behind the other one.
        flush_barrier.wait()
        events.append(name + ".done")
        return outcomes[name]

    async def close_kv():
        events.append("kv.close")

    async def close_kernel():
        events.append("kernel.close")

    modules = {
        "jiuwenswarm.observability.gateway_hints": SimpleNamespace(
            trajectory_gateway_hint_bridge=SimpleNamespace(unbind=unbind),
        ),
        "jiuwenswarm.server.runtime.session.session_history": SimpleNamespace(
            flush_pending_writes=lambda timeout: flush("history", timeout),
        ),
        "jiuwenswarm.server.runtime.session.session_metadata": SimpleNamespace(
            flush_pending_writes=lambda timeout: flush("metadata", timeout),
        ),
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner": SimpleNamespace(
            get_kv_cache_application_owner=lambda: SimpleNamespace(close=close_kv),
        ),
        "jiuwenswarm.agents.harness.team.team_manager": SimpleNamespace(
            shutdown_team_observability=lambda: events.append("team.close"),
        ),
        "jiuwenswarm.agents.harness.agent_observability": SimpleNamespace(
            shutdown_agent_observability=lambda: events.append("agent.close"),
        ),
        "jiuwenswarm.runtime.kernel": SimpleNamespace(
            process_kernel=lambda: SimpleNamespace(close=close_kernel),
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(events=events, outcomes=outcomes)


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_tasks_before_server_and_joins_persistence(shutdown_resources):
    resources = shutdown_resources
    tasks = {}

    async def background(name):
        try:
            await asyncio.Event().wait()
        finally:
            resources.events.append(name + ".cancelled")

    for name in ("image", "proactive", "teammate", "zen"):
        tasks[name] = asyncio.create_task(background(name))
    await asyncio.sleep(0)

    async def stop_server():
        assert all(task.done() for task in tasks.values())
        resources.events.append("server.stop")

    runtime = WorkerRuntime(
        server=SimpleNamespace(stop=stop_server), stop_event=asyncio.Event(),
        image_task=tasks["image"], proactive_task=tasks["proactive"],
        teammate_task=tasks["teammate"], zen_task=tasks["zen"],
    )
    tasks["notification"] = runtime.spawn(background("notification"), name="notification-test")
    await asyncio.sleep(0)
    try:
        await runtime.shutdown()
        assert runtime.stop_event.is_set()
        assert all(task.cancelled() for task in tasks.values())
        events = resources.events
        assert events.index("unbind") < events.index("server.stop")
        for name in ("history", "metadata"):
            assert events.index("server.stop") < events.index(name + ".begin")
            assert events.index(name + ".done") < events.index("kv.close")
        assert events[-4:] == ["kv.close", "team.close", "agent.close", "kernel.close"]
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_writer", ["history", "metadata"])
async def test_shutdown_reports_incomplete_persistence_before_releasing_stores(
    shutdown_resources, failed_writer,
):
    resources = shutdown_resources
    resources.outcomes[failed_writer] = False

    async def stop_server():
        resources.events.append("server.stop")

    runtime = WorkerRuntime(server=SimpleNamespace(stop=stop_server), stop_event=asyncio.Event())
    with pytest.raises(RuntimeError, match="persistence barrier did not complete"):
        await runtime.shutdown()

    assert "history.done" in resources.events and "metadata.done" in resources.events
    assert "kv.close" not in resources.events
    assert "kernel.close" not in resources.events


@pytest.mark.asyncio
async def test_shutdown_owns_image_waiter_without_cancelling_server_owned_refresh(shutdown_resources):
    entered = asyncio.Event()

    async def refresh():
        entered.set()
        await asyncio.Event().wait()

    underlying = asyncio.create_task(refresh())

    async def await_refresh():
        await asyncio.shield(underlying)

    waiter = asyncio.create_task(await_refresh())
    await entered.wait()
    await asyncio.sleep(0)

    async def stop_server():
        assert waiter.cancelled()
        assert not underlying.done(), "server retains refresh ownership until its own stop"
        underlying.cancel()
        await asyncio.gather(underlying, return_exceptions=True)
        shutdown_resources.events.append("server.stop")

    runtime = WorkerRuntime(
        server=SimpleNamespace(stop=stop_server), stop_event=asyncio.Event(), image_task=waiter,
    )
    try:
        await runtime.shutdown()
        assert waiter.cancelled() and underlying.cancelled()
    finally:
        waiter.cancel()
        underlying.cancel()
        await asyncio.gather(waiter, underlying, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_service_keeps_same_constructed_owner_for_optional_tasks_and_failed_cleanup(
    monkeypatch, shutdown_resources, startup_fails,
):
    loaded = {}
    owners = []
    task_entered = asyncio.Event()
    task_cancelled = asyncio.Event()
    startup_error = OSError("optional factory failed after creating its task")

    async def background():
        task_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            task_cancelled.set()

    async def stop_server():
        assert task_cancelled.is_set()
        shutdown_resources.events.append("server.stop")

    server = SimpleNamespace(start=AsyncMock(), stop=stop_server)

    async def construct(_host, _port, *, on_constructed, telemetry):
        await on_constructed(server)
        return server

    async def start_optional(runtime, _signals, _measurements):
        assert loaded["runtime"] is runtime
        owners.append(runtime)
        runtime.image_task = asyncio.create_task(background())
        await task_entered.wait()
        if startup_fails:
            raise startup_error

    monkeypatch.setattr(lifecycle.runtime_backend, "preload_runtime_backend", lambda: None)
    monkeypatch.setattr(lifecycle.runtime_bootstrap, "construct_runtime_server", construct)
    monkeypatch.setattr(lifecycle.runtime_bootstrap, "bind_runtime_signals", lambda *_args: None)
    monkeypatch.setattr(lifecycle.telemetry, "install_runtime_observability", lambda _server: None)
    monkeypatch.setattr(lifecycle.capabilities, "start_optional_preparation", start_optional)
    signals = SimpleNamespace(failed=Mock())
    dispatcher = SimpleNamespace(bind_runtime=AsyncMock(), mark_failed=AsyncMock())
    loader = asyncio.create_task(service._load_runtime(
        "127.0.0.1", 18000, signals, dispatcher, loaded,
    ))
    try:
        if startup_fails:
            with pytest.raises(OSError, match="optional factory failed") as caught:
                await loader
            assert caught.value is startup_error
            signals.failed.assert_called_once_with(str(startup_error))
            dispatcher.mark_failed.assert_awaited_once_with(str(startup_error))
        else:
            assert await loader is owners[0]
            signals.failed.assert_not_called()
            dispatcher.mark_failed.assert_not_awaited()

        owner = await service._runtime_for_shutdown(loader, loaded, timeout=1)
        assert owner is loaded["runtime"] is owners[0]
        assert owner.server is server
        assert not owner.image_task.done()
        dispatcher.bind_runtime.assert_awaited_once_with(server)
        server.start.assert_awaited_once_with(bind_transport=False)

        await owner.shutdown()
        assert owner.image_task.cancelled()
        assert task_cancelled.is_set()
    finally:
        if owners:
            task = owners[0].image_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if not loader.done():
            loader.cancel()
            await asyncio.gather(loader, return_exceptions=True)
