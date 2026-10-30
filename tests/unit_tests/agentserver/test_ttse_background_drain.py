# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise host draining with both SDK handles and asyncio awaitables."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.background_tasks import BackgroundTask

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _drain_ttse_rail_background_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "future", "handle"])
async def test_drain_waits_for_each_supported_task_shape(kind):
    proceed = asyncio.Event()
    if kind == "future":
        underlying = asyncio.get_running_loop().create_future()
    else:
        underlying = asyncio.create_task(proceed.wait())
    resource = BackgroundTask.from_asyncio_task(underlying, group="test") if kind == "handle" else underlying
    rail = SimpleNamespace(_bg_tasks={resource})
    draining = asyncio.create_task(_drain_ttse_rail_background_tasks(rail, timeout_secs=2))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not draining.done()
        assert not underlying.done()
    finally:
        if kind == "future":
            underlying.set_result(None)
        else:
            proceed.set()
        await asyncio.wait_for(draining, 3)
    assert rail._bg_tasks == set()


@pytest.mark.asyncio
async def test_drain_cancels_raw_task_after_timeout_and_keeps_it_until_exit():
    cancelled, proceed = asyncio.Event(), asyncio.Event()

    async def writer():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await proceed.wait()

    task = asyncio.create_task(writer())
    rail = SimpleNamespace(_bg_tasks={task})
    try:
        await _drain_ttse_rail_background_tasks(rail, timeout_secs=0.01)
        await asyncio.wait_for(cancelled.wait(), 2)
        assert task in rail._bg_tasks
        assert not task.done()
    finally:
        proceed.set()
        await asyncio.wait_for(task, 2)
    await _drain_ttse_rail_background_tasks(rail, timeout_secs=0.01)
