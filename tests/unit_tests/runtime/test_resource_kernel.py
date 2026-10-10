"""Resource identity, retirement, isolation and concurrent construction."""
import asyncio
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
