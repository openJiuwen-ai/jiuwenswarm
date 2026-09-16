from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager


async def _completed_task() -> None:
    return None


class _ObservedLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.entering = asyncio.Event()

    async def acquire(self) -> None:
        await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        self.entering.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self._lock.release()


@pytest.mark.asyncio
async def test_external_input_context_retries_latest_tail_and_holds_reload_lock() -> None:
    manager = AgentManager()
    manager._reload_lock = _ObservedLock()
    first = asyncio.create_task(_completed_task())
    await first
    manager._permissions_reload_tail = first
    await manager._reload_lock.acquire()

    installs = 0

    async def install_session_config() -> None:
        nonlocal installs
        installs += 1
        assert manager._reload_lock.locked()

    context = manager.build_permissions_external_input_context(
        install_session_config
    )()
    enter = asyncio.create_task(context.__aenter__())
    await manager._reload_lock.entering.wait()

    second_release = asyncio.Event()

    async def second_reload() -> None:
        await second_release.wait()

    second = asyncio.create_task(second_reload())
    manager._permissions_reload_tail = second
    manager._reload_lock.release()

    assert enter.done() is False
    assert installs == 0

    second_release.set()
    await enter
    assert installs == 1
    assert manager._reload_lock.locked()

    await context.__aexit__(None, None, None)
    assert manager._reload_lock.locked() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("old_outcome", ["failed", "cancelled"])
async def test_external_input_context_ignores_superseded_tail_failure(
    old_outcome: str,
) -> None:
    manager = AgentManager()
    old_started = asyncio.Event()
    old_release = asyncio.Event()
    latest_release = asyncio.Event()

    async def old_reload() -> None:
        old_started.set()
        await old_release.wait()
        raise RuntimeError("superseded reload failed")

    async def latest_reload() -> None:
        await latest_release.wait()

    old_tail = asyncio.create_task(old_reload())
    manager._permissions_reload_tail = old_tail
    install = AsyncMock()
    context = manager.build_permissions_external_input_context(install)()
    enter = asyncio.create_task(context.__aenter__())
    await old_started.wait()

    latest_tail = asyncio.create_task(latest_reload())
    manager._permissions_reload_tail = latest_tail
    if old_outcome == "cancelled":
        old_tail.cancel()
    else:
        old_release.set()
    latest_release.set()

    await enter
    install.assert_awaited_once_with()
    await context.__aexit__(None, None, None)
    assert manager._reload_lock.locked() is False


@pytest.mark.asyncio
async def test_external_input_context_latest_failure_blocks_install() -> None:
    manager = AgentManager()

    async def fail_reload() -> None:
        raise RuntimeError("latest reload failed")

    failed = asyncio.create_task(fail_reload())
    manager._permissions_reload_tail = failed
    install = AsyncMock()

    with pytest.raises(RuntimeError, match="latest reload failed"):
        async with manager.build_permissions_external_input_context(install)():
            raise AssertionError("external input context entered after reload failure")

    install.assert_not_awaited()
    assert manager._reload_lock.locked() is False


@pytest.mark.asyncio
async def test_external_input_context_schedule_failure_blocks_install() -> None:
    manager = AgentManager()
    failure = RuntimeError("scheduler unavailable")
    manager._permissions_reload_schedule_failure = (object(), failure)
    install = AsyncMock()

    with pytest.raises(
        RuntimeError,
        match="permission reload scheduling failed",
    ) as raised:
        async with manager.build_permissions_external_input_context(install)():
            raise AssertionError("external input context entered after scheduling failure")

    assert raised.value.__cause__ is failure
    install.assert_not_awaited()
    assert manager._reload_lock.locked() is False


def test_root_adapter_registers_replaces_and_clears_external_input_context() -> None:
    root = JiuWenSwarmDeepAdapter()
    first_builder = object()
    second_builder = object()

    root.set_permissions_external_input_context_builder(first_builder)
    assert root._permissions_external_input_context_builder is first_builder
    root.set_permissions_external_input_context_builder(second_builder)
    assert root._permissions_external_input_context_builder is second_builder
    root.set_permissions_external_input_context_builder(None)
    assert root._permissions_external_input_context_builder is None


@pytest.mark.asyncio
async def test_external_input_install_uses_manager_and_session_locks() -> None:
    manager = AgentManager()
    root = JiuWenSwarmDeepAdapter()
    child = JiuWenSwarmDeepAdapter()
    child._enable_auto_permission = True
    root._session_adapters["session-1"] = child
    root.set_permissions_external_input_context_builder(
        manager.build_permissions_external_input_context
    )

    async def reload_session(
        session_id, adapter, *, host_external_input=False
    ) -> None:
        assert manager._reload_lock.locked()
        assert root._session_adapter_locks[session_id].locked()
        assert session_id == "session-1"
        assert adapter is child
        assert host_external_input is True

    root._reload_session_adapter_if_stale = reload_session
    request = AgentRequest(
        request_id="external-1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "start", "mode": "agent"},
    )

    selected = await root._get_session_adapter_for_request(
        request,
        reserve_activity=True,
    )

    assert selected is child
    assert child.is_session_active("session-1")
    child._unregister_session_agent_task("session-1")
    assert not child.is_session_active("session-1")
    assert manager._reload_lock.locked() is False
    assert root._session_adapter_locks["session-1"].locked() is False
