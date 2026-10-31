"""Slow builtin resource installation must leave the runtime loop responsive."""
import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import service
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.server.runtime import extension_package_manager


@pytest.mark.asyncio
async def test_builtin_install_keeps_runtime_loop_responsive(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_install():
        entered.set()
        assert release.wait(2), 'test installation was not released'

    monkeypatch.setattr(service, '_acquire_process_runtime_extensions', AsyncMock(return_value=True))
    monkeypatch.setattr(extension_package_manager, 'ensure_builtin_agent_groups_installed', slow_install)
    runtime = object.__new__(AgentRuntime)
    task = asyncio.create_task(runtime._ensure_extensions())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not task.done(), 'builtin installation blocked the runtime event loop'
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 0.2)
    finally:
        release.set()
        await task
    assert runtime._shared_extensions_acquired
