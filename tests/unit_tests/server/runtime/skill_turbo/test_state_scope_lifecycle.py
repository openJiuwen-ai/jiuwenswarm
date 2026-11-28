"""真实 SDK 会话的状态元数据回收、写锁及快照同步。"""

import asyncio
import gc
import weakref

import pytest

from segment_state_fixtures import storage
from jiuwenswarm.server.runtime.skill_turbo import resume_context
from jiuwenswarm.server.runtime.skill_turbo.resume_context import isolated_skill_state


async def test_historical_session_metadata_does_not_accumulate(storage):
    async def read_once(index):
        session = storage.session(sid=f'lifecycle-{index}')
        async with isolated_skill_state(session) as state:
            assert state.get('lifecycle_probe') is None
        return weakref.ref(session)

    callers = [await read_once(index) for index in range(1000)]
    gc.collect()
    assert all(caller() is None for caller in callers)
    assert len(resume_context._state_scopes) == 0
    assert len(resume_context._caller_scopes) == 0


async def test_live_caller_keeps_scope_and_receives_later_commits(storage):
    caller = storage.session()
    await caller.pre_run(inputs=None)
    async with isolated_skill_state(caller, write=True) as state:
        state.replace('probe', {'revision': 1})
        state.replace('removed', 'old')
        scope = weakref.ref(state.scope)
    del state
    gc.collect()
    assert scope() is not None
    assert len(resume_context._state_scopes) == 1
    other = storage.session()
    async with isolated_skill_state(other, write=True) as state:
        assert state.scope is scope()
        state.replace('probe', {'revision': 2})
        state.replace('removed', None)
    assert caller.get_state('probe') == {'revision': 2}
    assert caller.get_state('removed') is None
    await caller.post_run()
    fresh = storage.session()
    async with isolated_skill_state(fresh) as state:
        assert state.get('probe') == {'revision': 2}
        assert state.get('removed') is None
    del state, caller, other, fresh
    gc.collect()
    assert scope() is None
    assert len(resume_context._state_scopes) == len(resume_context._caller_scopes) == 0


async def test_reclaiming_scope_preserves_persisted_state(storage):
    async def write_once():
        caller = storage.session()
        async with isolated_skill_state(caller, write=True) as state:
            state.replace('persistent_probe', {'revision': 7})
        return weakref.ref(caller)

    caller = await write_once()
    gc.collect()
    assert caller() is None
    assert len(resume_context._state_scopes) == len(resume_context._caller_scopes) == 0
    async with isolated_skill_state(storage.session()) as state:
        assert state.get('persistent_probe') == {'revision': 7}


async def test_waiting_and_new_writers_share_one_lock(storage):
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(2)]
    active = 0
    maximum = 0
    scopes = []
    order = []

    async def writer(index):
        nonlocal active, maximum
        async with isolated_skill_state(storage.session(), write=True) as state:
            active += 1
            maximum = max(maximum, active)
            scopes.append(id(state.scope))
            entered[index].set()
            try:
                if index < 2:
                    await release[index].wait()
                state.replace('writer', index)
                order.append(index)
            finally:
                active -= 1

    tasks = [asyncio.create_task(writer(0))]
    try:
        await asyncio.wait_for(entered[0].wait(), timeout=5)
        tasks.append(asyncio.create_task(writer(1)))
        await asyncio.sleep(0)
        assert not entered[1].is_set()
        release[0].set()
        await tasks[0]
        await asyncio.wait_for(entered[1].wait(), timeout=5)
        tasks.append(asyncio.create_task(writer(2)))
        await asyncio.sleep(0)
        assert not entered[2].is_set()
    finally:
        for event in release:
            event.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert maximum == 1
    assert len(set(scopes)) == 1
    assert order == [0, 1, 2]
    gc.collect()
    assert len(resume_context._state_scopes) == len(resume_context._caller_scopes) == 0


async def test_cancelling_waiter_does_not_release_active_writer(storage):
    entered = asyncio.Event()
    release = asyncio.Event()
    successor_entered = asyncio.Event()

    async def owner():
        async with isolated_skill_state(storage.session(), write=True):
            entered.set()
            await release.wait()

    async def successor():
        async with isolated_skill_state(storage.session(), write=True) as state:
            successor_entered.set()
            state.replace('probe', 'successor')

    first = asyncio.create_task(owner())
    tasks = [first]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        cancelled = asyncio.create_task(successor())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not successor_entered.is_set()
        tasks.append(asyncio.create_task(successor()))
        await asyncio.sleep(0)
        assert not successor_entered.is_set()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert successor_entered.is_set()


async def test_cancelling_active_writer_allows_waiter_to_continue(storage):
    entered = asyncio.Event()
    release = asyncio.Event()
    scopes = []

    async def owner():
        async with isolated_skill_state(storage.session(), write=True) as state:
            scopes.append(id(state.scope))
            entered.set()
            await release.wait()

    async def waiter():
        async with isolated_skill_state(storage.session(), write=True) as state:
            scopes.append(id(state.scope))
            state.replace('probe', 'after cancellation')

    first = asyncio.create_task(owner())
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        queued = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert not queued.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(queued, timeout=5)
    finally:
        release.set()
        await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)
    assert len(set(scopes)) == 1
    async with isolated_skill_state(storage.session()) as state:
        assert state.get('probe') == 'after cancellation'


@pytest.mark.parametrize('failure', ['read', 'body', 'commit', 'ack', 'confirm'])
async def test_failed_operations_release_scope_and_allow_retry(storage, failure):
    if failure == 'read':
        storage.cp.fail_next_read = True
    elif failure in {'commit', 'ack', 'confirm'}:
        storage.cp.mode = {'commit': 'before', 'ack': 'after', 'confirm': 'confirm'}[failure]

    async def attempt():
        async with isolated_skill_state(storage.session(), write=True) as state:
            state.replace('probe', 'first')
            if failure == 'body':
                raise OSError('body failed')

    with pytest.raises(OSError):
        await attempt()
    gc.collect()
    assert len(resume_context._state_scopes) == len(resume_context._caller_scopes) == 0
    storage.cp.mode = ''
    async with isolated_skill_state(storage.session(), write=True) as state:
        state.replace('probe', 'retry')
    async with isolated_skill_state(storage.session()) as state:
        assert state.get('probe') == 'retry'


def test_reclaimed_identity_can_be_used_in_a_new_event_loop(storage):
    scopes = []

    async def run_contended_writes():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def writer(first):
            async with isolated_skill_state(storage.session(), write=True) as state:
                scopes.append(weakref.ref(state.scope))
                if first:
                    entered.set()
                    await release.wait()
                state.replace('loop_probe', 'saved')

        owner = asyncio.create_task(writer(True))
        waiter = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            waiter = asyncio.create_task(writer(False))
            await asyncio.sleep(0)
            assert not waiter.done()
        finally:
            release.set()
            await asyncio.gather(owner, *([waiter] if waiter is not None else []))

    for _ in range(2):
        asyncio.run(run_contended_writes())
        gc.collect()
        assert all(scope() is None for scope in scopes)
        assert len(resume_context._state_scopes) == len(resume_context._caller_scopes) == 0
