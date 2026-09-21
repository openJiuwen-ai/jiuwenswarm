"""Keep permission admission on execution, independent of cached facade lookup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime import agent_manager as manager_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_manager import AgentManager, _make_agent_cache_key


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_state", ["pending", "failed", "cancelled", "schedule_failed"])
@pytest.mark.parametrize("request_kind", ["lookup", "manual", "code", "team", "ordinary_answer"])
async def test_cached_facade_lookup_does_not_wait_for_permission_reload(
    monkeypatch: pytest.MonkeyPatch, reload_state: str, request_kind: str,
) -> None:
    manager = AgentManager()
    # Model an already cached facade without invoking unrelated construction IO.
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = SimpleNamespace(
        has_auto_permission_session=lambda _sid: request_kind == "ordinary_answer",
    )
    mode = request_kind if request_kind in {"code", "team"} else "agent"
    manager.agents = {"web": {_make_agent_cache_key(mode, None, None): facade}}
    monkeypatch.setattr(manager_module, "get_config", lambda: {
        "permissions": {"enabled": True, "mode": "manual"},
    })
    tail = asyncio.get_running_loop().create_future()
    manager._permissions_reload_tail = tail
    if reload_state == "failed":
        tail.set_exception(RuntimeError("reload failed"))
        tail.exception()
    elif reload_state == "cancelled":
        tail.cancel()
    elif reload_state == "schedule_failed":
        tail.set_result(None)
        manager._permissions_reload_schedule_failure = (object(), RuntimeError("schedule failed"))

    params = {"mode": mode}
    if request_kind == "ordinary_answer":
        params.update({
            "source": "ask_user_interrupt", "request_id": "ask-1",
            "answers": [{"selected_options": ["continue"]}],
        })
    request = SimpleNamespace(channel_id="web", session_id="session-1", params=params)
    try:
        lookup = (
            manager.get_agent(channel_id="web", mode=mode)
            if request_kind == "lookup" else manager.get_agent_for_request(request)
        )
        assert await asyncio.wait_for(lookup, timeout=1) is facade
        assert not manager._reload_lock.locked()
        if reload_state == "pending":
            assert not tail.done()
        elif reload_state == "schedule_failed":
            assert manager._permissions_reload_schedule_failure is not None
    finally:
        if not tail.done():
            tail.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_state", ["absent", "success", "failed", "cancelled", "schedule_failed"])
async def test_external_input_builder_preserves_latest_reload_outcome(reload_state: str) -> None:
    manager = AgentManager()
    install = AsyncMock()
    failure = RuntimeError("reload failed")
    if reload_state != "absent":
        tail = asyncio.get_running_loop().create_future()
        manager._permissions_reload_tail = tail
        if reload_state == "failed":
            tail.set_exception(failure)
        elif reload_state == "cancelled":
            tail.cancel()
        else:
            tail.set_result(None)
        if reload_state == "schedule_failed":
            manager._permissions_reload_schedule_failure = (object(), failure)

    async def enter() -> None:
        async with manager.build_permissions_external_input_context(install)():
            assert manager._reload_lock.locked()

    if reload_state in {"failed", "schedule_failed"}:
        expected = "permission reload scheduling failed" if reload_state == "schedule_failed" else "reload failed"
        with pytest.raises(RuntimeError, match=expected) as caught:
            await enter()
        if reload_state == "schedule_failed":
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        install.assert_not_awaited()
    elif reload_state == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await enter()
        install.assert_not_awaited()
    else:
        await enter()
        install.assert_awaited_once_with()
    assert not manager._reload_lock.locked()


class _ChangingTailLock(asyncio.Lock):
    """Publish a new notification at the admission lock boundary."""

    def __init__(self, manager: AgentManager, changes: int) -> None:
        super().__init__()
        self.manager = manager
        self.changes = changes
        self.entries = 0

    async def __aenter__(self):
        await super().__aenter__()
        self.entries += 1
        if self.entries <= self.changes:
            tail = asyncio.get_running_loop().create_future()
            tail.set_result(None)
            self.manager._permissions_reload_tail = tail
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
async def test_admission_waits_for_older_notification_after_latest_finishes(monkeypatch, outcome):
    manager = AgentManager()
    started, release = asyncio.Event(), asyncio.Event()
    install = AsyncMock()

    async def reload(config, *_args, **_kwargs):
        if config.get("older"):
            started.set()
            await release.wait()
            if outcome == "failed":
                raise RuntimeError("older notification failed")

    monkeypatch.setattr(manager, "reload_agents_config", reload)
    older = manager.schedule_permissions_reload({"older": True})
    await asyncio.wait_for(started.wait(), 2)
    await manager.schedule_permissions_reload({})

    async def enter():
        async with manager.build_permissions_external_input_context(install)():
            pass

    admission = asyncio.create_task(enter())
    await asyncio.sleep(0)
    assert not admission.done()
    install.assert_not_awaited()
    if outcome == "cancelled":
        older.cancel()
    release.set()
    await asyncio.gather(older, return_exceptions=True)
    if outcome == "success":
        await asyncio.wait_for(admission, 2)
        install.assert_awaited_once_with()
    else:
        with pytest.raises(RuntimeError, match="permission reload scheduling failed"):
            await asyncio.wait_for(admission, 2)
        install.assert_not_awaited()
        await manager.schedule_permissions_reload({})
        await enter()
        install.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [1, 2, 3])
async def test_external_input_builder_checks_latest_tail_with_three_attempt_limit(changes: int) -> None:
    manager = AgentManager()
    lock = _ChangingTailLock(manager, changes)
    manager._reload_lock = lock
    install = AsyncMock()

    async def enter() -> None:
        async with manager.build_permissions_external_input_context(install)():
            assert lock.entries == changes + 1

    if changes == 3:
        with pytest.raises(RuntimeError, match="^permission_reload_not_stable$"):
            await enter()
        assert lock.entries == 3
        install.assert_not_awaited()
    else:
        await enter()
        install.assert_awaited_once_with()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_admission_does_not_cancel_shared_reload() -> None:
    manager = AgentManager()
    tail = asyncio.get_running_loop().create_future()
    manager._permissions_reload_tail = tail
    install = AsyncMock()
    context = manager.build_permissions_external_input_context(install)()
    admission = asyncio.create_task(context.__aenter__())
    await asyncio.sleep(0)
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission
    assert not tail.done()
    assert not manager._reload_lock.locked()
    install.assert_not_awaited()
    tail.set_result(None)
