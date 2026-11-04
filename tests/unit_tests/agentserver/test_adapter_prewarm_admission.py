"""Root adapter warmup shares the Worker owner and foreground admission."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.worker.preparation import (
    PreparationRegistry,
    current_preparation_registry,
)


@pytest.mark.asyncio
async def test_root_prewarm_cannot_bypass_foreground_pause_or_cancel_shared_owner():
    began = asyncio.Event()
    finish = asyncio.Event()

    async def warm():
        began.set()
        await finish.wait()

    owner = PreparationRegistry()
    owner.register("mcp", warm)
    owner.bind_current_loop()
    token = owner.pause_background()
    owner.mark_minimum_ready()
    adapter = SimpleNamespace(
        _mcp_prewarm_task=None, _agent_name="first-chat", _do_mcp_prewarm=AsyncMock()
    )
    try:
        JiuWenSwarmDeepAdapter._start_mcp_prewarm(adapter)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not began.is_set()
        adapter._do_mcp_prewarm.assert_not_awaited()
        owner.resume_background(token)
        await asyncio.wait_for(began.wait(), timeout=1)
        shared = owner.start("mcp")
        adapter._mcp_prewarm_task.cancel()
        await asyncio.gather(adapter._mcp_prewarm_task, return_exceptions=True)
        assert not shared.cancelled()
        finish.set()
        await asyncio.wait_for(shared, timeout=1)
        assert owner.snapshot()["mcp"] == "ready"
    finally:
        await owner.close()
    assert current_preparation_registry() is None
