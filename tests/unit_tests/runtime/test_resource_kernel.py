"""Resource identity, retirement, isolation and concurrent construction."""
import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKernel, ResourceKey, identity_reference


def key():
    identity = KernelIdentity("trusted-owner", "data-a", "project-a", "agent", "definition", "caps", "v1")
    return ResourceKey(identity, "model", "stable-model-id", "route", "credential-ref", "recipe")


@pytest.mark.asyncio
async def test_single_flight_and_retirement_do_not_close_an_active_resource():
    kernel = ResourceKernel()
    built = []
    closed = []
    resource_key = key()

    def factory():
        value = object()
        built.append(value)
        return value

    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda _: kernel.acquire(resource_key, factory, close=closed.append), range(16)))
    assert len(built) == 1
    kernel.retire(lambda candidate: candidate == resource_key)
    replacement = kernel.acquire(resource_key, factory, close=closed.append)
    assert replacement.value is not leases[0].value
    assert closed == []
    old = leases[0].value
    await asyncio.gather(*(lease.release() for lease in leases))
    assert closed == [old]
    assert kernel.resource_count == 1
    await leases[0].release()
    assert closed == [old]
    await replacement.release()


@pytest.mark.asyncio
async def test_complete_identity_and_credential_rotation_separate_connections():
    kernel = ResourceKernel()
    original = kernel.acquire(key(), object)
    variations = [replace(key(), credential_ref="rotated"), replace(key(), routing="new-route")]
    for field in KernelIdentity.__dataclass_fields__:
        variations.append(replace(key(), identity=replace(key().identity, **{field: "different"})))
    for variant in variations:
        lease = kernel.acquire(variant, object)
        assert lease.value is not original.value
        await lease.release()
    await original.release()


def test_failed_factory_is_not_cached_and_secret_references_are_opaque():
    kernel = ResourceKernel()
    with pytest.raises(ValueError):
        kernel.acquire(key(), lambda: (_ for _ in ()).throw(ValueError("bad config")))
    assert kernel.resource_count == 0
    secret = "credential-secret-must-not-appear"
    ref = identity_reference({"token": secret})
    assert secret not in repr(replace(key(), credential_ref=ref))
    assert ref != identity_reference({"token": secret + "-rotated"})


@pytest.mark.asyncio
async def test_async_preparation_is_single_flight_and_survives_a_cancelled_waiter():
    kernel = ResourceKernel()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def factory():
        calls.append(object())
        started.set()
        await release.wait()
        return calls[-1]

    abandoned = asyncio.create_task(kernel.acquire_async(key(), factory))
    await started.wait()
    kept = asyncio.create_task(kernel.acquire_async(key(), factory))
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    release.set()
    lease = await kept
    assert len(calls) == 1
    assert lease.value is calls[0]
    await lease.release()
    await kernel.close()


@pytest.mark.asyncio
async def test_shutdown_attempts_other_resources_when_one_close_fails():
    kernel = ResourceKernel()
    closed = []

    def broken(value):
        closed.append(value)
        raise ValueError("connection cleanup failed")

    first = kernel.acquire(key(), object, close=broken)
    second = kernel.acquire(replace(key(), resource_id="second"), object, close=closed.append)
    values = [first.value, second.value]
    await first.release()
    await second.release()
    with pytest.raises(RuntimeError, match="cleanup failed for 1"):
        await kernel.close()
    assert closed == values
    assert kernel.resource_count == 0


@pytest.mark.asyncio
async def test_retiring_inflight_preparation_disposes_it_and_builds_a_fresh_resource():
    kernel = ResourceKernel()
    started, release = asyncio.Event(), asyncio.Event()
    built, closed = [], []

    async def factory():
        value = object()
        built.append(value)
        if len(built) == 1:
            started.set()
            await release.wait()
        return value

    acquiring = asyncio.create_task(kernel.acquire_async(key(), factory, close=closed.append))
    await started.wait()
    kernel.retire(lambda candidate: candidate == key())
    release.set()
    lease = await acquiring
    assert built == [closed[0], lease.value]
    await lease.release()
    await kernel.close()
    assert closed == built


async def _event_loop_checkpoint():
    """Let already-scheduled tasks reach their first await, without a sleep."""
    reached = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(reached.set_result, None)
    await reached


async def _thread_event(event):
    assert await asyncio.to_thread(event.wait, 5), "thread did not reach the expected boundary"


@pytest.mark.asyncio
async def test_factories_for_different_keys_execute_concurrently():
    kernel = ResourceKernel()
    rendezvous = threading.Barrier(2)

    def factory():
        rendezvous.wait(timeout=5)
        return object()

    leases = await asyncio.wait_for(asyncio.gather(
        asyncio.to_thread(kernel.acquire, key(), factory),
        asyncio.to_thread(kernel.acquire, replace(key(), resource_id="other"), factory),
    ), timeout=6)
    assert leases[0].value is not leases[1].value
    await asyncio.gather(*(lease.release() for lease in leases))
    await kernel.close()


@pytest.mark.asyncio
async def test_concurrent_sync_waiters_share_a_failure_and_later_retry(monkeypatch):
    kernel = ResourceKernel()
    started, release, joined = threading.Event(), threading.Event(), threading.Event()
    calls = []
    waiters = 4
    joined_count = 0
    join_lock = threading.Lock()

    def factory():
        calls.append("failed")
        started.set()
        assert release.wait(5)
        raise ValueError("construction failed")

    with ThreadPoolExecutor(max_workers=waiters + 1) as pool:
        builder = pool.submit(kernel.acquire, key(), factory)
        try:
            await _thread_event(started)
            with kernel._lock:
                completion = kernel._preparing[key()].done
            original_result = completion.result

            def wait_for_result(*args, **kwargs):
                nonlocal joined_count
                with join_lock:
                    joined_count += 1
                    if joined_count == waiters:
                        joined.set()
                return original_result(*args, **kwargs)

            monkeypatch.setattr(completion, "result", wait_for_result)
            followers = [pool.submit(kernel.acquire, key(), factory) for _ in range(waiters)]
            await _thread_event(joined)
        finally:
            release.set()
        for future in [builder, *followers]:
            with pytest.raises(ValueError, match="construction failed"):
                future.result(timeout=5)
    assert calls == ["failed"]
    assert kernel.resource_count == 0
    retried = kernel.acquire(key(), object)
    await retried.release()
    await kernel.close()


@pytest.mark.asyncio
async def test_retired_sync_factory_cannot_overwrite_new_generation():
    kernel = ResourceKernel()
    started, release = threading.Event(), threading.Event()
    stale, replacement = object(), object()
    closed = []

    def old_factory():
        started.set()
        assert release.wait(5)
        return stale

    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(kernel.acquire, key(), old_factory, close=closed.append)
        try:
            await _thread_event(started)
            kernel.retire(lambda candidate: candidate == key())
            updated = kernel.acquire(key(), lambda: replacement, close=closed.append)
        finally:
            release.set()
        resumed = original.result(timeout=5)
    assert resumed.value is updated.value is replacement
    assert kernel.resource_count == 1
    await resumed.release()
    assert closed == [stale]
    await updated.release()
    await kernel.close()
    assert closed == [stale, replacement]


@pytest.mark.asyncio
async def test_preparation_callbacks_run_outside_global_lock():
    kernel = ResourceKernel()
    started, release = threading.Event(), threading.Event()
    observed = []

    def factory():
        started.set()
        assert release.wait(5)
        return object()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(kernel.acquire, key(), factory)
        try:
            await _thread_event(started)
            with kernel._lock:
                completion = kernel._preparing[key()].done

            def callback(_future):
                # A different thread must obtain the lock before this callback
                # returns; the builder's RLock reentrancy cannot hide a bug.
                observed.append(pool.submit(lambda: kernel.resource_count).result(timeout=5))

            completion.add_done_callback(callback)
        finally:
            release.set()
        lease = result.result(timeout=6)
    assert observed == [1]
    await lease.release()
    await kernel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_waits_for_actual_sync_completion_and_disposes_late_value(cancel_close, monkeypatch):
    kernel = ResourceKernel()
    unhandled = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_exception_handler", unhandled.append)
    started, release = threading.Event(), threading.Event()
    late = object()
    closed = []

    def factory():
        started.set()
        assert release.wait(5)
        return late

    async def never_build():
        pytest.fail("async caller must join the sync construction")

    with ThreadPoolExecutor(max_workers=1) as pool:
        builder = pool.submit(kernel.acquire, key(), factory, close=closed.append)
        try:
            await _thread_event(started)
            follower = asyncio.create_task(kernel.acquire_async(key(), never_build))
            await _event_loop_checkpoint()
            closing = asyncio.create_task(kernel.close())
            await _event_loop_checkpoint()
            assert not closing.done()
            with pytest.raises(RuntimeError, match="Kernel is closed"):
                kernel.acquire(replace(key(), resource_id="new"), object)
            if cancel_close:
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
                with kernel._lock:
                    assert not next(iter(kernel._inflight)).done.cancelled()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="Kernel is closed"):
            builder.result(timeout=5)
        with pytest.raises(RuntimeError, match="Kernel is closed"):
            await follower
        await asyncio.wait_for(kernel.close() if cancel_close else closing, timeout=5)
    assert closed == [late]
    assert kernel.resource_count == 0
    gc.collect()
    await _event_loop_checkpoint()
    assert unhandled == []


@pytest.mark.asyncio
async def test_close_refuses_active_lease_without_poisoning_kernel():
    kernel = ResourceKernel()
    lease = kernel.acquire(key(), object)
    with pytest.raises(RuntimeError, match="active resource leases"):
        await kernel.close()
    second = kernel.acquire(key(), lambda: pytest.fail("existing resource should remain usable"))
    assert second.value is lease.value
    await lease.release()
    await second.release()
    await kernel.close()


@pytest.mark.asyncio
async def test_async_and_sync_same_key_share_sync_factory():
    kernel = ResourceKernel()
    started, release = threading.Event(), threading.Event()
    shared = object()

    def factory():
        started.set()
        assert release.wait(5)
        return shared

    async def wrong_factory():
        pytest.fail("sync construction must win")

    with ThreadPoolExecutor(max_workers=1) as pool:
        building = pool.submit(kernel.acquire, key(), factory)
        try:
            await _thread_event(started)
            asynchronous = asyncio.create_task(kernel.acquire_async(key(), wrong_factory))
            await _event_loop_checkpoint()
        finally:
            release.set()
        sync_lease = building.result(timeout=5)
        async_lease = await asynchronous
    assert sync_lease.value is async_lease.value is shared
    await sync_lease.release()
    await async_lease.release()
    await kernel.close()


@pytest.mark.asyncio
async def test_sync_waiter_shares_async_factory_without_blocking_owning_loop(monkeypatch):
    kernel = ResourceKernel()
    started, release = asyncio.Event(), asyncio.Event()
    joined = threading.Event()
    shared = object()

    async def factory():
        started.set()
        await release.wait()
        return shared

    asynchronous = asyncio.create_task(kernel.acquire_async(key(), factory))
    await started.wait()
    with pytest.raises(RuntimeError, match="own thread"):
        kernel.acquire(key(), object)
    with kernel._lock:
        completion = kernel._preparing[key()].done
    original_result = completion.result

    def wait_for_result(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            joined.set()
        return original_result(*args, **kwargs)

    monkeypatch.setattr(completion, "result", wait_for_result)
    with ThreadPoolExecutor(max_workers=1) as pool:
        synchronous = pool.submit(kernel.acquire, key(), lambda: pytest.fail("async construction must win"))
        try:
            await _thread_event(joined)
        finally:
            release.set()
        async_lease = await asynchronous
        sync_lease = synchronous.result(timeout=5)
    assert async_lease.value is sync_lease.value is shared
    await async_lease.release()
    await sync_lease.release()
    await kernel.close()


@pytest.mark.asyncio
async def test_async_shutdown_before_factory_first_step_completes_waiters():
    kernel = ResourceKernel()
    # Close is queued behind acquire but before the preparation Task that
    # acquire schedules. Cancelling that unstarted Task must complete its flight.
    acquiring = asyncio.create_task(kernel.acquire_async(key(), lambda: pytest.fail("factory must not start")))
    closing = asyncio.create_task(kernel.close())
    with pytest.raises(RuntimeError, match="Kernel is closed"):
        await asyncio.wait_for(acquiring, timeout=5)
    await asyncio.wait_for(closing, timeout=5)


@pytest.mark.asyncio
async def test_cross_loop_waiter_joins_factory_and_cleanup_returns_to_owner_loop():
    kernel = ResourceKernel()
    started, owner_released, stop = threading.Event(), threading.Event(), threading.Event()
    owner_loop = []
    unblock = []
    disposed_on = []
    shared = object()

    async def owner():
        owner_loop.append(asyncio.get_running_loop())
        release = asyncio.Event()
        unblock.append(release)

        async def factory():
            started.set()
            await release.wait()
            return shared

        async def dispose(value):
            assert value is shared
            disposed_on.append(asyncio.get_running_loop())

        lease = await kernel.acquire_async(key(), factory, close=dispose)
        await lease.release()
        owner_released.set()
        assert await asyncio.to_thread(stop.wait, 5)

    async def wrong_factory():
        pytest.fail("other loop must join the existing factory")

    with ThreadPoolExecutor(max_workers=1) as pool:
        owning = pool.submit(lambda: asyncio.run(owner()))
        try:
            await _thread_event(started)
            joining = asyncio.create_task(kernel.acquire_async(key(), wrong_factory))
            await _event_loop_checkpoint()
            owner_loop[0].call_soon_threadsafe(unblock[0].set)
            lease = await asyncio.wait_for(joining, timeout=5)
            assert lease.value is shared
            await _thread_event(owner_released)
            await lease.release()
            await asyncio.wait_for(kernel.close(), timeout=5)
            assert disposed_on == owner_loop
        finally:
            stop.set()
            if owner_loop and owner_loop[0].is_running():
                owner_loop[0].call_soon_threadsafe(unblock[0].set)
        owning.result(timeout=5)


@pytest.mark.asyncio
async def test_close_attempts_all_disposals_including_failed_late_sync_factory():
    kernel = ResourceKernel()
    started, release = threading.Event(), threading.Event()
    closed = []

    def broken(value):
        closed.append(value)
        raise ValueError("cannot close")

    idle = kernel.acquire(replace(key(), resource_id="idle"), object, close=closed.append)
    idle_value = idle.value
    await idle.release()
    late_value = object()

    def factory():
        started.set()
        assert release.wait(5)
        return late_value

    with ThreadPoolExecutor(max_workers=1) as pool:
        builder = pool.submit(kernel.acquire, key(), factory, close=broken)
        try:
            await _thread_event(started)
            closing = asyncio.create_task(kernel.close())
            await _event_loop_checkpoint()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="Kernel is closed"):
            builder.result(timeout=5)
        with pytest.raises(RuntimeError, match="cleanup failed for 1"):
            await closing
    assert closed == [idle_value, late_value]
    assert kernel.resource_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("old_fails", [False, True])
async def test_old_completion_does_not_remove_an_inflight_replacement(old_fails):
    kernel = ResourceKernel()
    old_started, old_release, old_completed = threading.Event(), threading.Event(), threading.Event()
    new_started, new_release = threading.Event(), threading.Event()
    stale, replacement = object(), object()
    closed = []

    def old_factory():
        old_started.set()
        assert old_release.wait(5)
        if old_fails:
            raise ValueError("obsolete construction failed")
        return stale

    def new_factory():
        new_started.set()
        assert new_release.wait(5)
        return replacement

    async def wrong_factory():
        pytest.fail("replacement flight must remain registered")

    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(kernel.acquire, key(), old_factory, close=closed.append)
        try:
            await _thread_event(old_started)
            with kernel._lock:
                old_flight = kernel._preparing[key()]
            old_flight.done.add_done_callback(lambda _future: old_completed.set())
            kernel.retire(lambda candidate: candidate == key())
            updated = pool.submit(kernel.acquire, key(), new_factory, close=closed.append)
            await _thread_event(new_started)
            with kernel._lock:
                new_flight = kernel._preparing[key()]
            assert new_flight is not old_flight
            old_release.set()
            await _thread_event(old_completed)
            with kernel._lock:
                assert kernel._preparing[key()] is new_flight
            follower = asyncio.create_task(kernel.acquire_async(key(), wrong_factory))
            await _event_loop_checkpoint()
        finally:
            old_release.set()
            new_release.set()
        leases = [updated.result(timeout=5), await follower]
        if old_fails:
            with pytest.raises(ValueError, match="obsolete construction failed"):
                original.result(timeout=5)
        else:
            leases.append(original.result(timeout=5))
    assert all(lease.value is replacement for lease in leases)
    await asyncio.gather(*(lease.release() for lease in leases))
    await kernel.close()
    assert closed == ([replacement] if old_fails else [stale, replacement])


@pytest.mark.asyncio
async def test_close_cancels_async_factory_on_its_own_event_loop():
    kernel = ResourceKernel()
    started = threading.Event()
    cancelled_on = []
    owner_loop = []
    unblock = []

    async def owner():
        owner_loop.append(asyncio.get_running_loop())
        release = asyncio.Event()
        unblock.append(release)

        async def factory():
            started.set()
            try:
                await release.wait()
            finally:
                cancelled_on.append(asyncio.get_running_loop())

        with pytest.raises(RuntimeError, match="Kernel is closed"):
            await kernel.acquire_async(key(), factory)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owning = pool.submit(lambda: asyncio.run(owner()))
        try:
            await _thread_event(started)
            await asyncio.wait_for(kernel.close(), timeout=5)
        finally:
            if owner_loop and owner_loop[0].is_running():
                owner_loop[0].call_soon_threadsafe(unblock[0].set)
        owning.result(timeout=5)
    assert cancelled_on == owner_loop
    assert kernel.resource_count == 0


def test_enterprise_kernel_resources_are_scoped_to_trusted_tenant_root(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    inputs = dict(project_dir="", work_mode="agent", definition=None,
                  capabilities={}, configuration={})
    with TenantContext.scoped("group::bot::alice"):
        alice = KernelIdentity.for_agent(**inputs)
        assert alice == KernelIdentity.for_agent(**inputs)
        assert alice.data_dir == str(TenantContext.current_tenant_root().resolve())
    with TenantContext.scoped("group::bot::bob"):
        bob = KernelIdentity.for_agent(**inputs)
    assert alice != bob
    assert alice.owner != bob.owner
    assert alice.data_dir != bob.data_dir


def test_personal_kernel_identity_keeps_process_root(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path)
    inputs = dict(project_dir="", work_mode="agent", definition=None,
                  capabilities={}, configuration={})
    with TenantContext.scoped("first"):
        first = KernelIdentity.for_agent(**inputs)
    with TenantContext.scoped("second"):
        second = KernelIdentity.for_agent(**inputs)
    assert first == second
    assert first.data_dir == str(tmp_path.resolve())
