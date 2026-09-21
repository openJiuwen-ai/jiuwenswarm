from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueueError,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from tests.unit_tests.agentserver.permissions.test_permission_lifecycle_integration import (
    lifecycle as lifecycle,
)


def _config(marker: str = "A") -> dict:
    return {"permissions": {"enabled": True, "mode": "auto", "tools": {marker: "deny"}}}


@pytest.fixture(autouse=True)
def smart_runtime(monkeypatch):
    monkeypatch.setattr(interface_deep, "get_config", _config)


def _root() -> JiuWenSwarmDeepAdapter:
    root = JiuWenSwarmDeepAdapter()
    root._session_instance_mode = "agent"
    return root


def _child(session_id="session-1") -> JiuWenSwarmDeepAdapter:
    child = JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped(session_id)
    child._session_instance_mode = "agent"
    child._config_base_cache = _config()
    return child


class _ObservedLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempts = 0
        self.second_attempt = asyncio.Event()

    async def acquire(self) -> None:
        self.attempts += 1
        if self.attempts >= 2:
            self.second_attempt.set()
        await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        self.attempts += 1
        if self.attempts >= 2:
            self.second_attempt.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self._lock.release()


def _host() -> tuple[
    AgentManager, JiuWenSwarmDeepAdapter, JiuWenSwarmDeepAdapter
]:
    manager = AgentManager()
    root = _root()
    child = _child()
    root._session_adapters["session-1"] = child
    root._session_adapter_versions["session-1"] = 0
    root.set_permissions_external_input_context_builder(
        manager.build_permissions_external_input_context
    )
    return manager, root, child


def _external_request(request_id: str = "external-1") -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "start", "mode": "agent"},
    )


def _permission_resume_request(channel_id: str) -> AgentRequest:
    return AgentRequest(
        request_id=f"resume-{channel_id}",
        channel_id=channel_id,
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "resume",
            "mode": "agent",
            "source": "permission_interrupt",
            "request_id": "permission-card",
            "answers": [{"approved": True}],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", ["web", "tui", "cli"])
async def test_permission_resume_reuses_exact_session_adapter(
    channel_id: str,
) -> None:
    root = _root()
    child = _child()
    root._session_adapters["session-1"] = child

    selected = await root._get_session_adapter_for_request(
        _permission_resume_request(channel_id),
        reserve_activity=False,
    )

    assert selected is child
    assert root._session_adapters == {"session-1": child}


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", ["web", "tui", "cli"])
@pytest.mark.usefixtures("internal_auto_mode")
async def test_permission_resume_without_original_adapter_fails_closed(
    channel_id: str,
) -> None:
    root = _root()

    with pytest.raises(
        RootPermissionQueueError,
        match="permission_resume_owner_missing",
    ):
        await root._get_session_adapter_for_request(
            _permission_resume_request(channel_id),
            reserve_activity=False,
        )

    assert root._session_adapters == {}


@pytest.mark.asyncio
async def test_three_reloads_install_only_latest_config() -> None:
    manager, root, child = _host()
    snapshots = [_config(marker) for marker in ("B", "C", "D")]
    installed = []

    async def publish_reload(*_args, **_kwargs) -> None:
        root._mark_session_adapters_stale_for_reload(snapshots.pop(0), None)

    async def install_config(config, _env, **_kwargs) -> None:
        installed.append(next(iter(config["permissions"]["tools"])))
        child._config_base_cache = config

    manager.reload_agents_config = publish_reload
    child.reload_agent_config = install_config

    tails = [manager.schedule_permissions_reload({}) for _ in range(3)]
    await asyncio.wait_for(tails[-1], 5)
    selected = await root._get_session_adapter_for_request(
        _external_request("external-D"),
        reserve_activity=True,
    )

    assert installed == ["D"]
    assert root._session_adapter_versions["session-1"] == 3
    assert selected is child
    child._unregister_session_agent_task("session-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal_auto_mode")
async def test_targeted_permission_reload_advances_one_global_lazy_version() -> None:
    root = _root()
    root._session_adapters = {
        "session-a": _child("session-a"),
        "session-b": _child("session-b"),
    }
    targeted = AsyncMock()
    root._reload_target_session_adapter = targeted
    latest = {"permissions": {"enabled": True, "mode": "auto"}}

    await root._fan_out_reload_to_session_adapters(
        latest,
        None,
        "session-a",
        permission_delta=True,
    )

    targeted.assert_not_awaited()
    assert root._session_adapter_config_version == 1
    assert root._pending_session_reload_config_base == latest
    assert root._session_adapter_versions == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal_auto_mode")
async def test_nonfresh_lookup_never_installs_pending_permission_config() -> None:
    root = _root()
    child = _child()
    root._session_adapter_config_version = 1
    root._pending_session_reload_config_base = {
        "permissions": {"enabled": True, "mode": "auto"}
    }
    child.reload_agent_config = AsyncMock()

    await root._reload_session_adapter_if_stale("session-1", child)

    child.reload_agent_config.assert_not_awaited()
    assert root._session_adapter_versions.get("session-1", 0) == 0


@pytest.mark.parametrize(
    ("params", "metadata", "channel_id", "method", "expected"),
    [
        ({"query": "new"}, {}, "web", ReqMethod.CHAT_SEND, True),
        (
            {"query": "follow", "input_mode": "follow_up"},
            {},
            "web",
            ReqMethod.CHAT_SEND,
            True,
        ),
        (
            {"query": "steer", "input_mode": "steer"},
            {},
            "web",
            ReqMethod.CHAT_SEND,
            True,
        ),
        # Preserve develop Permission Engine timing: TUI control text is still
        # an external CHAT_SEND update opportunity.  The safe boundary protects
        # live Permission callbacks/transactions, not a Goal-wide policy epoch.
        ({"query": "/goal resume"}, {}, "tui", ReqMethod.CHAT_SEND, True),
        (
            {
                "query": "answer",
                "source": "permission_interrupt",
                "request_id": "approval-1",
                "answers": [{"approved": True}],
            },
            {},
            "web",
            ReqMethod.CHAT_SEND,
            False,
        ),
        (
            {
                "query": "answer",
                "source": "ask_user_interrupt",
                "request_id": "ask-1",
                "answers": [{"answer": "yes"}],
            },
            {},
            "web",
            ReqMethod.CHAT_SEND,
            False,
        ),
        (
            {
                "query": "approve",
                "source": "confirm_interrupt",
                "request_id": "plan-1",
                "approved": True,
            },
            {},
            "web",
            ReqMethod.CHAT_SEND,
            False,
        ),
        ({"query": "retry"}, {"skip_a2ui": True}, "web", ReqMethod.CHAT_SEND, False),
        ({"query": "tick"}, {}, "cron", ReqMethod.CHAT_SEND, False),
        ({"query": "tick"}, {}, "heartbeat", ReqMethod.CHAT_SEND, False),
        (
            {"query": "internal", "source": "internal_dispatch"},
            {},
            "web",
            ReqMethod.CHAT_SEND,
            False,
        ),
        ({"query": "status"}, {}, "web", ReqMethod.COMMAND_STATUS, False),
        ({"query": "resume"}, {}, "web", ReqMethod.COMMAND_GOAL, False),
    ],
)
def test_host_permission_update_input_contract(
    params,
    metadata,
    channel_id,
    method,
    expected,
) -> None:
    request = AgentRequest(
        request_id="request-1",
        channel_id=channel_id,
        session_id="session-1",
        req_method=method,
        params=params,
        metadata=metadata,
    )

    assert JiuWenSwarmDeepAdapter._is_host_permission_update_input(request) is expected


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal_auto_mode")
async def test_reload_registered_during_cutover_waits_until_enqueue() -> None:
    manager, root, child = _host()
    manager._reload_lock = _ObservedLock()
    install_started = asyncio.Event()
    install_release = asyncio.Event()
    tail_acquired = asyncio.Event()

    async def install_config(*_args, **_kwargs) -> None:
        install_started.set()
        await asyncio.wait_for(install_release.wait(), 5)

    child.reload_agent_config = install_config
    root._mark_session_adapters_stale_for_reload(_config("B"), None)
    select = asyncio.create_task(
        root._get_session_adapter_for_request(
            _external_request("external-B"),
            reserve_activity=True,
        )
    )
    async def later_reload(*_args, **_kwargs) -> None:
        async with manager._reload_lock:
            assert install_release.is_set()
            tail_acquired.set()

    tail = None
    try:
        await asyncio.wait_for(install_started.wait(), 5)
        manager.reload_agents_config = later_reload
        tail = manager.schedule_permissions_reload({})
        await asyncio.wait_for(manager._reload_lock.second_attempt.wait(), 5)
        assert not tail_acquired.is_set()
        assert not child.is_session_active("session-1")
        install_release.set()
        assert await asyncio.wait_for(select, 5) is child
        await asyncio.wait_for(tail, 5)
        assert tail_acquired.is_set()
    finally:
        install_release.set()
        for task in (select, tail):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (select, tail) if task is not None), return_exceptions=True)
        child._unregister_session_agent_task("session-1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("internal_auto_mode")
async def test_partial_reload_failure_enqueues_nothing() -> None:
    manager, root, child = _host()
    installs = 0

    async def partial_failure(*_args, **_kwargs) -> None:
        root._mark_session_adapters_stale_for_reload(
            _config("partial"), None
        )
        raise RuntimeError("reload failed after partial publication")

    async def install_config(*_args, **_kwargs) -> None:
        nonlocal installs
        installs += 1

    manager.reload_agents_config = partial_failure
    child.reload_agent_config = install_config
    manager.schedule_permissions_reload({})

    with pytest.raises(RuntimeError, match="permission reload scheduling failed") as caught:
        await root._get_session_adapter_for_request(
            _external_request("external-partial"),
            reserve_activity=True,
        )

    assert "partial publication" in str(caught.value.__cause__)
    assert root._pending_session_reload_config_base is not None
    assert installs == 0
    assert child._active_session_ids.get("session-1", 0) == 0


@pytest.mark.asyncio
async def test_successful_permission_reload_does_not_reenter_interaction(lifecycle, monkeypatch):
    h = lifecycle
    instance = h.instance
    interaction = {}
    for name in ("send_input", "attach_output", "start", "stop"):
        spy = AsyncMock(wraps=getattr(instance, name))
        monkeypatch.setattr(instance, name, spy)
        interaction[name] = spy
    goal = instance.goal_manager
    config = instance.deep_config
    h.change()
    await h.reload()
    assert instance.deep_config is config and instance.goal_manager is goal
    assert instance.is_registered_rail(h.adapter._permission_rail)
    assert h.callbacks()
    for spy in interaction.values():
        spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_session_lock_wait_releases_and_retries() -> None:
    manager, root, child = _host()
    session_lock = _ObservedLock()
    await session_lock.acquire()
    root._session_adapter_locks["session-1"] = session_lock

    select = asyncio.create_task(
        root._get_session_adapter_for_request(
            _external_request("cancelled"),
            reserve_activity=True,
        )
    )
    try:
        await asyncio.wait_for(session_lock.second_attempt.wait(), 5)
        select.cancel()
        with pytest.raises(asyncio.CancelledError):
            await select
        assert manager._reload_lock.locked() is False
        assert child._active_session_ids.get("session-1", 0) == 0
    finally:
        if not select.done():
            select.cancel()
        await asyncio.gather(select, return_exceptions=True)
        session_lock.release()

    selected = await root._get_session_adapter_for_request(
        _external_request("retry"),
        reserve_activity=True,
    )
    assert selected is child
    child._unregister_session_agent_task("session-1")


@pytest.mark.asyncio
async def test_publication_error_preserves_error_and_releases_locks() -> None:
    manager, root, child = _host()
    expected = RuntimeError("publication failed")

    async def fail_reload(*_args, **_kwargs) -> None:
        raise expected

    root._reload_session_adapter_if_stale = fail_reload
    with pytest.raises(RuntimeError) as raised:
        await root._get_session_adapter_for_request(
            _external_request("broken"),
            reserve_activity=True,
        )

    assert raised.value is expected
    assert manager._reload_lock.locked() is False
    assert root._session_adapter_locks["session-1"].locked() is False
    assert child._active_session_ids.get("session-1", 0) == 0
