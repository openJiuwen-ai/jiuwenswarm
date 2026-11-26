import asyncio

import pytest

from jiuwenswarm.server.runtime.session import recovery_budget as recovery
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError


@pytest.mark.asyncio
async def test_recovery_queue_orders_waiters_and_returns_raced_grants(monkeypatch):
    budget = recovery._RecoveryBudget(capacity=1, max_waiters=3, wait_seconds=2)
    monkeypatch.setattr(recovery, "_budget", budget)
    order = []

    async def enter(index):
        async with recovery.admit_context_recovery(str(index)):
            async with recovery.admit_context_recovery(str(index)):
                order.append(index)

    async with recovery.admit_context_recovery("owner"):
        # Child tasks inherit the ContextVar but cannot borrow the parent's slot.
        waiters = [asyncio.create_task(enter(i)) for i in range(3)]
        await asyncio.sleep(0)
        with pytest.raises(HistoryReplayError) as full:
            await asyncio.create_task(enter(3))
        assert full.value.code == "HISTORY_RESTORE_BUSY"
        assert not order
        waiters[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiters[1]
        last = asyncio.create_task(enter(4))
        await asyncio.sleep(0)
    # The first waiter owns the grant but has not yet resumed.
    waiters[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiters[0]
    await asyncio.gather(waiters[2], last)
    assert order == [2, 4]
    await enter(5)
    assert order == [2, 4, 5]


@pytest.mark.asyncio
async def test_wait_timeout_does_not_consume_queue_space_or_a_future_slot(monkeypatch):
    budget = recovery._RecoveryBudget(capacity=1, max_waiters=1, wait_seconds=0.01)
    monkeypatch.setattr(recovery, "_budget", budget)

    async def enter():
        async with recovery.admit_context_recovery("next"):
            return "admitted"

    async with recovery.admit_context_recovery("owner"):
        for _ in range(2):
            with pytest.raises(HistoryReplayError) as timeout:
                await asyncio.create_task(enter())
            assert timeout.value.code == "HISTORY_RESTORE_BUSY"
            assert "wait expired" in str(timeout.value)
    assert await enter() == "admitted"
