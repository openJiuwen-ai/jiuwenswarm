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
from jiuwenswarm.server.worker.runtime_host import WorkerRuntime


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
    server = SimpleNamespace(worker_callback_registry_owner=owner)
    runtime = WorkerRuntime(server=server, stop_event=asyncio.Event())

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
@pytest.mark.parametrize("published", [False, True])
async def test_failed_construction_leaves_registry_with_its_cleanup_owner(monkeypatch, published):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.worker import runtime_bootstrap

    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    server = SimpleNamespace()
    retained = []

    async def construct(_host, _port, *, on_constructed, telemetry):
        if published:
            await on_constructed(server)
        raise OSError("construction failed")

    async def retain(value):
        retained.append(value)

    monkeypatch.setattr(runtime_bootstrap, "_construct_runtime_server", construct)
    with pytest.raises(OSError, match="construction failed"):
        await runtime_bootstrap.construct_runtime_server(
            "127.0.0.1", 18000, on_constructed=retain, telemetry=None,
        )
    owner = getattr(framework.register, "_worker_callback_registry_owner")
    if published:
        assert retained == [server]
        assert server.worker_callback_registry_owner is owner
        assert not framework.has_subscribers("event")
        await owner.aclose()
    else:
        assert retained == []
    with pytest.raises(RuntimeError, match="owner is closed"):
        framework.has_subscribers("event")
