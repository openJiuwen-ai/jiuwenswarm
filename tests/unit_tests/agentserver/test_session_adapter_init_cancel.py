import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def root_adapter(child):
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._session_adapter_locks = {}
    root._session_adapters = {}
    root._session_adapter_versions = {}
    root._session_adapter_reload_failures = {}
    root._session_adapter_last_used = {}
    root._session_instance_config = None
    root._session_instance_mode = "agent"
    root._session_instance_sub_mode = None
    root._new_session_scoped_adapter = lambda _: child
    root._reload_session_adapter_if_stale = AsyncMock()
    root._touch_session_adapter = lambda _: None
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, repeat_cancel", [("create", False), ("start", False), ("create", True)])
async def test_cancelled_initialization_cleans_unpublished_child(stage, repeat_cancel):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def wait(*_, **__):
        entered.set()
        await asyncio.Event().wait()

    async def cleanup():
        cleaning.set()
        if repeat_cancel:
            await release.wait()

    child = SimpleNamespace(create_instance=AsyncMock(side_effect=wait if stage == "create" else None),
                            start_interaction=AsyncMock(side_effect=wait if stage == "start" else None),
                            cleanup=AsyncMock(side_effect=cleanup))
    root = root_adapter(child)
    task = asyncio.create_task(root._get_or_create_session_adapter("cancel-init"))
    await entered.wait()
    task.cancel()
    if repeat_cancel:
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.cleanup.assert_awaited_once()
    assert not root._session_adapters
    assert not root._session_adapter_locks.get("cancel-init", asyncio.Lock()).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_fails, failure", [(False, asyncio.CancelledError), (True, RuntimeError)])
async def test_discarded_adapter_metadata_does_not_skip_reload_on_retry(
    monkeypatch, tmp_path, reload_fails, failure,
):
    from jiuwenswarm.server.request_context import (
        reset_current_stream_stop, set_current_stream_stop,
    )
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.agents.harness.common import session_ops_service

    class Child:
        def __init__(self, fail_reload=False):
            self.fail_reload = fail_reload
            self._instance = None
            self._sys_operation = SimpleNamespace(mode=interface_deep.OperationMode.LOCAL)
            self.cleanup = AsyncMock()

        async def create_instance(self, config, **_):
            self.config_marker = config["marker"]

        async def start_interaction(self, **_):
            pass

        async def reload_agent_config(self, config, *_, **__):
            if self.fail_reload:
                raise RuntimeError("controlled reload failure")
            self.config_marker = config["marker"]

    sid = "discard-retry"
    first, second = Child(reload_fails), Child()
    root = root_adapter(first)
    del root._reload_session_adapter_if_stale  # Exercise production version bookkeeping.
    root._session_instance_config = {"marker": "old"}
    root._session_adapter_config_version = 1
    root._pending_session_reload_config_base = {"marker": "new"}
    root._pending_session_reload_env_overrides = {}
    children = iter([first, second])
    root._new_session_scoped_adapter = lambda _: next(children)
    state_path = tmp_path / "runtime_state.json"
    state_path.write_text("keep runtime state", encoding="utf-8")
    monkeypatch.setattr(interface_deep, "get_runtime_state_path", lambda _: state_path)
    monkeypatch.setattr(interface_deep, "_history_tail_lagged", lambda *_: False)
    monkeypatch.setattr(session_ops_service, "warmup_session_context", AsyncMock(return_value=None))
    monkeypatch.setattr(session_ops_service, "refresh_session_context_if_stale", AsyncMock(return_value=None))

    def fail_before_publish(session_id):
        root._session_adapter_last_used[session_id] = 1
        raise failure()

    root._touch_session_adapter = fail_before_publish
    token = set_current_stream_stop(asyncio.Event())
    try:
        with pytest.raises(failure):
            await root._get_or_create_session_adapter(sid)
    finally:
        reset_current_stream_stop(token)
    first.cleanup.assert_awaited_once()
    assert sid not in root._session_adapters
    assert sid not in root._session_adapter_versions
    assert sid not in root._session_adapter_reload_failures
    assert sid not in root._session_adapter_last_used
    lock = root._session_adapter_locks[sid]
    assert not lock.locked()
    assert state_path.read_text(encoding="utf-8") == "keep runtime state"

    root._touch_session_adapter = lambda session_id: root._session_adapter_last_used.__setitem__(session_id, 2)
    rebuilt = await root._get_or_create_session_adapter(sid)
    assert rebuilt is second
    assert rebuilt.config_marker == "new"
    assert root._session_adapter_versions[sid] == 1
    assert root._session_adapter_locks[sid] is lock


@pytest.mark.asyncio
async def test_initialization_that_swallows_cancel_is_not_started_or_published():
    entered, release = asyncio.Event(), asyncio.Event()

    async def initialize(*_, **__):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    child = SimpleNamespace(create_instance=initialize, start_interaction=AsyncMock(), cleanup=AsyncMock())
    root = root_adapter(child)
    task = asyncio.create_task(root._get_or_create_session_adapter("late-init"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.start_interaction.assert_not_awaited()
    child.cleanup.assert_awaited_once()
    assert not root._session_adapters


@pytest.mark.asyncio
async def test_cached_adapter_reload_cannot_swallow_request_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()
    child = SimpleNamespace(_reconcile_expert_binding_on_reuse=AsyncMock())
    root = root_adapter(child)
    root._session_adapters['cached'] = child

    async def reload(*_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    root._reload_session_adapter_if_stale = reload
    task = asyncio.create_task(root._get_or_create_session_adapter('cached'))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    child._reconcile_expert_binding_on_reuse.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_creation_cleans_unpublished_instance_on_cancel(monkeypatch):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager
    from jiuwenswarm.server.runtime.agent_adapter import interface as facade
    entered = asyncio.Event()

    async def create(*_, **__):
        entered.set()
        await asyncio.Event().wait()

    agent = SimpleNamespace(create_instance=create, cleanup=AsyncMock())
    monkeypatch.setattr(facade, 'JiuWenSwarm', lambda: agent)
    manager = AgentManager()
    task = asyncio.create_task(manager._create_agent('desktop'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    agent.cleanup.assert_awaited_once()
    assert not manager.agents
