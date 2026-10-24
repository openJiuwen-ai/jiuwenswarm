# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker ownership keeps the unmodified SDK registry on one event loop."""

import asyncio
from contextvars import ContextVar
import threading
import time
from types import SimpleNamespace

import pytest

from openjiuwen.core.runner.callback import AsyncCallbackFramework
from jiuwenswarm.server.worker.callback_registry import bind_callback_registry_owner
from jiuwenswarm.server.worker.lifecycle import WorkerRuntime


@pytest.mark.asyncio
async def test_initializer_loops_and_reload_share_one_sdk_registry_owner():
    framework = AsyncCallbackFramework(enable_logging=False)
    owner_loop = asyncio.get_running_loop()
    context = ContextVar("registration_session", default="unset")
    observed = []
    initializer_loops = []
    barrier = threading.Barrier(3)
    original = framework.register_sync

    def register(*args, **kwargs):
        observed.append((asyncio.get_running_loop(), context.get(), kwargs["namespace"]))
        time.sleep(0.01)
        return original(*args, **kwargs)

    framework.register_sync = register
    owner = bind_callback_registry_owner(framework)

    def initialize(index):
        async def callback():
            return index

        async def run():
            initializer_loops.append(asyncio.get_running_loop())
            barrier.wait(timeout=2)
            session = f"session-{index}"
            token = context.set(session)
            try:
                await framework.register("event", callback, namespace=session)
                return await framework.unregister("event", callback)
            finally:
                context.reset(token)

        return asyncio.run(run())

    try:
        calls = [asyncio.create_task(asyncio.to_thread(initialize, index)) for index in range(3)]
        await framework.unregister_namespace("old-session")
        await asyncio.wait_for(asyncio.gather(*calls), 5)
        assert len(set(initializer_loops)) == 3
        assert all(loop is not owner_loop for loop in initializer_loops)
        assert len(observed) == 3
        assert all(loop is owner_loop and value == session for loop, value, session in observed)
        assert framework.list_callbacks("event") == []
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_sync_decorator_registration_and_queries_run_on_owner_loop():
    framework = AsyncCallbackFramework(enable_logging=False)
    loop = asyncio.get_running_loop()
    observed = []
    original = framework.register_sync

    def register(*args, **kwargs):
        observed.append(asyncio.get_running_loop())
        return original(*args, **kwargs)

    framework.register_sync = register
    owner = bind_callback_registry_owner(framework)

    def initialize():
        @framework.on("event")
        async def callback():
            return "ready"

        assert framework.has_subscribers("event")
        return framework.list_callbacks("event")

    try:
        callbacks = await asyncio.wait_for(asyncio.to_thread(initialize), 2)
        assert len(callbacks) == 1
        assert observed == [loop]
        assert await framework.trigger("event") == ["ready"]
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_initializer_wait_does_not_block_worker_control_loop():
    framework = AsyncCallbackFramework(enable_logging=False)
    owner = bind_callback_registry_owner(framework)
    registered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def initialize():
        framework.register_sync("event", lambda: None)
        loop.call_soon_threadsafe(registered.set)
        assert release.wait(3)

    task = asyncio.create_task(asyncio.to_thread(initialize))
    try:
        await asyncio.wait_for(registered.wait(), 2)
        # The actual initializer remains in its thread while control/reload
        # tasks can still use the same registry from the business loop.
        assert not task.done()
        await asyncio.wait_for(framework.unregister_event("event"), 1)
        assert not framework.has_subscribers("event")
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
        await owner.aclose()


@pytest.mark.asyncio
async def test_foreign_loop_receives_original_registry_exceptions():
    error = ValueError("invalid callback")

    async def register():
        raise error

    def query():
        raise error

    framework = SimpleNamespace(register=register, list_events=query)
    owner = bind_callback_registry_owner(framework)
    try:
        with pytest.raises(ValueError) as async_error:
            await asyncio.to_thread(lambda: asyncio.run(framework.register()))
        with pytest.raises(ValueError) as sync_error:
            await asyncio.to_thread(framework.list_events)
        assert async_error.value is error
        assert sync_error.value is error
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_cancelling_initializer_cancels_its_forwarded_registry_call():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    caller_loop = None
    caller_task = None

    async def register():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    framework = SimpleNamespace(register=register)
    owner = bind_callback_registry_owner(framework)

    def initialize():
        async def run():
            nonlocal caller_loop, caller_task
            caller_loop = asyncio.get_running_loop()
            caller_task = asyncio.current_task()
            await framework.register()

        return asyncio.run(run())

    call = asyncio.create_task(asyncio.to_thread(initialize))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        caller_loop.call_soon_threadsafe(caller_task.cancel)
        result = await asyncio.wait_for(asyncio.gather(call, return_exceptions=True), 2)
        assert isinstance(result[0], asyncio.CancelledError)
        await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        await owner.aclose()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
async def test_binding_is_idempotent_and_rejects_late_calls_after_close():
    framework = AsyncCallbackFramework(enable_logging=False)
    owner = bind_callback_registry_owner(framework)
    assert bind_callback_registry_owner(framework) is owner
    await owner.aclose()
    await owner.aclose()

    with pytest.raises(RuntimeError, match="owner is closed"):
        framework.register_sync("event", lambda: None)
    with pytest.raises(RuntimeError, match="owner is closed"):
        await framework.unregister_event("event")
    with pytest.raises(RuntimeError, match="owner is closed"):
        bind_callback_registry_owner(framework)


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_foreign_loop_operation():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def register(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    framework = SimpleNamespace(register=register)
    owner = bind_callback_registry_owner(framework)
    call = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(framework.register())))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await owner.aclose()
        result = await asyncio.wait_for(asyncio.gather(call, return_exceptions=True), 2)
        assert isinstance(result[0], asyncio.CancelledError)
        assert cancelled.is_set()
    finally:
        await owner.aclose()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_prevents_queued_sync_registration_from_running(monkeypatch):
    loop = asyncio.get_running_loop()
    submitted = threading.Event()
    calls = []
    errors = []
    framework = SimpleNamespace(register_sync=lambda: calls.append("late"))
    owner = bind_callback_registry_owner(framework)
    original = loop.call_soon_threadsafe

    def schedule(callback, *args, **kwargs):
        if getattr(callback, "__self__", None) is owner:
            submitted.set()
        return original(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", schedule)

    def register():
        try:
            framework.register_sync()
        except RuntimeError as exc:
            errors.append(exc)

    thread = threading.Thread(target=register)
    thread.start()
    try:
        # Hold this loop until the thread has queued its public SDK call.
        assert submitted.wait(2)
        await owner.aclose()
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()
        assert calls == []
        assert len(errors) == 1 and "owner is closed" in str(errors[0])
    finally:
        await owner.aclose()
        await asyncio.to_thread(thread.join, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_runtime_keeps_registry_open_through_teardown_then_closes_it(failed):
    framework = AsyncCallbackFramework(enable_logging=False)
    owner = bind_callback_registry_owner(framework)
    runtime = WorkerRuntime(
        server=SimpleNamespace(), stop_event=asyncio.Event(),
        callback_registry_owner=owner,
    )

    async def teardown():
        await framework.unregister_event("event")
        if failed:
            raise OSError("teardown failed")

    runtime._shutdown_owned_resources = teardown
    if failed:
        with pytest.raises(OSError, match="teardown failed"):
            await runtime.shutdown()
    else:
        await runtime.shutdown()
    with pytest.raises(RuntimeError, match="owner is closed"):
        framework.has_subscribers("event")


@pytest.mark.asyncio
@pytest.mark.parametrize("constructed", [False, True])
async def test_failed_startup_closes_registry_after_partial_runtime_cleanup(
    monkeypatch, worker_callback_framework, constructed,
):
    from jiuwenswarm.server.worker import lifecycle

    framework = worker_callback_framework
    retained = []
    stopped = []

    async def stop():
        # Partial startup must still let SDK teardown remove callbacks.
        await framework.unregister_event("event")
        stopped.append(True)

    server = SimpleNamespace(stop=stop)

    async def start(_host, _port, _signals, *, runtime, on_constructed, startup_t0):
        assert startup_t0 > 0
        assert runtime.callback_registry_owner is not None
        if constructed:
            runtime.server = server
            await on_constructed(server)
        raise OSError("construction failed")

    async def retain(value):
        retained.append(value)

    monkeypatch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
    monkeypatch.setattr(lifecycle, "_start_worker_runtime", start)
    with pytest.raises(OSError, match="construction failed"):
        await lifecycle.start_worker_runtime(
            "127.0.0.1", 18000, SimpleNamespace(), on_constructed=retain,
        )
    assert retained == ([server] if constructed else [])
    assert stopped == ([True] if constructed else [])
    with pytest.raises(RuntimeError, match="owner is closed"):
        framework.has_subscribers("event")


@pytest.mark.asyncio
async def test_worker_binds_registry_before_loading_extensions(monkeypatch, worker_callback_framework):
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.worker import lifecycle

    framework = worker_callback_framework
    owner_loop = asyncio.get_running_loop()

    def create_registry(*, callback_framework, config, logger):
        assert callback_framework is framework
        owner = getattr(framework.register, "_worker_callback_registry_owner")
        owner.check_owner_loop()
        assert asyncio.get_running_loop() is owner_loop
        raise OSError("extension loading failed")

    monkeypatch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
    monkeypatch.setattr(ExtensionRegistry, "create_instance", create_registry)
    with pytest.raises(OSError, match="extension loading failed"):
        await lifecycle.start_worker_runtime("127.0.0.1", 18000, SimpleNamespace())
    with pytest.raises(RuntimeError, match="owner is closed"):
        framework.has_subscribers("event")


@pytest.mark.asyncio
async def test_worker_shutdown_keeps_registry_open_for_server_stop(worker_callback_framework):
    framework = worker_callback_framework
    owner = bind_callback_registry_owner(framework)
    stopped = []

    async def stop():
        await asyncio.to_thread(lambda: asyncio.run(framework.unregister_event("event")))
        stopped.append(True)

    runtime = WorkerRuntime(
        server=SimpleNamespace(stop=stop), stop_event=asyncio.Event(),
        callback_registry_owner=owner,
    )
    await runtime.shutdown()
    assert stopped == [True]
    assert runtime.stop_event.is_set()
    with pytest.raises(RuntimeError, match="owner is closed"):
        await framework.register("event", lambda: None)


def test_simulated_worker_lifetimes_restore_the_process_registry(monkeypatch):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.worker import lifecycle

    original = Runner.callback_framework
    frameworks = []

    async def run_worker():
        framework = AsyncCallbackFramework(enable_logging=False)
        frameworks.append(framework)

        async def stop():
            await framework.unregister_event("event")

        async def start(_host, _port, _signals, *, runtime, on_constructed, startup_t0):
            runtime.server = SimpleNamespace(stop=stop)
            await asyncio.to_thread(lambda: asyncio.run(framework.register("event", stop)))

        with monkeypatch.context() as patch:
            patch.setattr(Runner, "callback_framework", framework)
            patch.setattr(lifecycle, "_preload_runtime_backend", lambda: None)
            patch.setattr(lifecycle, "_start_worker_runtime", start)
            runtime = await lifecycle.start_worker_runtime("127.0.0.1", 18000, SimpleNamespace())
            assert framework.has_subscribers("event")
            await runtime.shutdown()
            with pytest.raises(RuntimeError, match="owner is closed"):
                framework.has_subscribers("event")
        assert Runner.callback_framework is original

    # Each process boundary gets a new framework, even when tests simulate
    # successive Workers in distinct event loops within one Python process.
    asyncio.run(run_worker())
    asyncio.run(run_worker())
    assert frameworks[0] is not frameworks[1]
    assert Runner.callback_framework is original
