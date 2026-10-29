# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for adapter-owned SysOperation release on cleanup."""
# Lifecycle tests intentionally inspect internal reference ownership.
# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeSysOperation:
    """Minimal stand-in exposing only the id the adapter reads."""

    def __init__(self, sys_operation_id: str) -> None:
        self.id = sys_operation_id


class _FakeResourceMgr:
    """Registry double recording what the adapter registers and removes."""

    def __init__(self) -> None:
        self.registered: dict[str, _FakeSysOperation] = {}
        self.removed: list[str] = []

    def register(self, sys_operation: _FakeSysOperation) -> _FakeSysOperation:
        self.registered[sys_operation.id] = sys_operation
        return sys_operation

    def get_sys_operation(self, sys_operation_id: str) -> _FakeSysOperation | None:
        return self.registered.get(sys_operation_id)

    def remove_sys_operation(self, sys_operation_id: str) -> None:
        self.removed.append(sys_operation_id)
        self.registered.pop(sys_operation_id, None)


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter carrying only the sys_operation bookkeeping state."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._retained_sys_operation_ids = []
    return adapter


@pytest.fixture(name="resource_mgr")
def _resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    fake = _FakeResourceMgr()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", fake)
    interface_deep._SYS_OPERATION_REFCOUNTS.clear()
    interface_deep._SYS_OPERATION_RELEASE_TASKS.clear()
    yield fake
    interface_deep._SYS_OPERATION_REFCOUNTS.clear()
    interface_deep._SYS_OPERATION_RELEASE_TASKS.clear()


def _bind_resolver(
    adapter: JiuWenSwarmDeepAdapter,
    resource_mgr: _FakeResourceMgr,
    sys_operation_id: str,
) -> None:
    """Make ``_resolve_sys_operation`` hand back a registered fake sys operation."""
    sys_operation = resource_mgr.register(_FakeSysOperation(sys_operation_id))
    adapter._resolve_sys_operation = lambda: sys_operation


def test_cleanup_releases_locally_owned_sys_operation(resource_mgr: _FakeResourceMgr) -> None:
    """A local sys operation has a single holder, so its adapter unregisters it."""
    adapter = _make_adapter()
    _bind_resolver(adapter, resource_mgr, "local_a")

    assert adapter._create_sys_operation() is not None
    assert adapter._retained_sys_operation_ids == ["local_a"]

    adapter._release_sys_operations()

    assert resource_mgr.removed == ["local_a"]
    assert adapter._retained_sys_operation_ids == []
    assert interface_deep._SYS_OPERATION_REFCOUNTS == {}


def test_shared_sys_operation_survives_until_last_holder(resource_mgr: _FakeResourceMgr) -> None:
    """A sandbox sys operation reused across sessions outlives the first cleanup."""
    first = _make_adapter()
    second = _make_adapter()
    _bind_resolver(first, resource_mgr, "sandbox_shared")
    second._resolve_sys_operation = first._resolve_sys_operation

    first._create_sys_operation()
    second._create_sys_operation()

    first._release_sys_operations()
    assert resource_mgr.removed == []
    assert resource_mgr.get_sys_operation("sandbox_shared") is not None

    second._release_sys_operations()
    assert resource_mgr.removed == ["sandbox_shared"]


def test_rebuild_on_same_id_keeps_registration(resource_mgr: _FakeResourceMgr) -> None:
    """Re-running create_instance on the same id must not unregister it."""
    adapter = _make_adapter()
    _bind_resolver(adapter, resource_mgr, "sandbox_shared")

    adapter._create_sys_operation()
    adapter._create_sys_operation()

    assert resource_mgr.removed == []
    assert adapter._retained_sys_operation_ids == ["sandbox_shared"]

    adapter._release_sys_operations()
    assert resource_mgr.removed == ["sandbox_shared"]


def test_release_is_noop_without_instance(resource_mgr: _FakeResourceMgr) -> None:
    """The root adapter never builds an agent, so it has nothing to release."""
    adapter = _make_adapter()

    adapter._release_sys_operations()

    assert resource_mgr.removed == []
    assert adapter._retained_sys_operation_ids == []


def test_release_is_idempotent(resource_mgr: _FakeResourceMgr) -> None:
    """A second cleanup pass must not re-remove an already dropped registration."""
    adapter = _make_adapter()
    _bind_resolver(adapter, resource_mgr, "local_a")
    adapter._create_sys_operation()

    adapter._release_sys_operations()
    adapter._release_sys_operations()

    assert resource_mgr.removed == ["local_a"]


def test_releasing_an_unheld_id_leaves_it_registered(resource_mgr: _FakeResourceMgr) -> None:
    """Releasing a reference never taken must not unregister a live resource.

    A sys operation registered outside the adapter path carries no refcount
    entry; treating that as "last holder released" would pull the resource out
    from under whoever actually owns it.
    """
    adapter = _make_adapter()
    resource_mgr.register(_FakeSysOperation("externally_owned"))

    adapter._release_sys_operations(["externally_owned"])

    assert resource_mgr.removed == []
    assert resource_mgr.get_sys_operation("externally_owned") is not None


def test_failed_resolution_retains_nothing(resource_mgr: _FakeResourceMgr) -> None:
    """A None resolution leaves the refcount table untouched."""
    adapter = _make_adapter()
    adapter._resolve_sys_operation = lambda: None

    assert adapter._create_sys_operation() is None
    assert adapter._retained_sys_operation_ids == []
    assert interface_deep._SYS_OPERATION_REFCOUNTS == {}


def _sandbox_owners(resource_mgr):
    first, second = _make_adapter(), _make_adapter()
    _bind_resolver(first, resource_mgr, "shared-sandbox")
    second._resolve_sys_operation = first._resolve_sys_operation
    operation = first._resolve_sys_operation()
    operation._run_config = SimpleNamespace(
        isolation_key_template="scope",
        config=SimpleNamespace(launcher_config=SimpleNamespace(
            sandbox_type="jiuwenbox", base_url="http://localhost:8321",
            extra_params={"sandbox_id": "old"},
        )),
    )
    first._create_sys_operation()
    second._create_sys_operation()
    return first, second


@pytest.mark.asyncio
async def test_last_session_owner_awaits_remote_cleanup(resource_mgr, monkeypatch):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    release = AsyncMock(return_value=["old"])
    monkeypatch.setattr(jiuwenbox, "release_jiuwenbox_scope", release)
    first, second = _sandbox_owners(resource_mgr)
    await first._cleanup_sys_operations()
    release.assert_not_awaited()
    assert resource_mgr.get_sys_operation("shared-sandbox") is not None
    await second._cleanup_sys_operations()
    release.assert_awaited_once_with("http://localhost:8321", "scope")
    assert resource_mgr.get_sys_operation("shared-sandbox") is None
    assert not interface_deep._SYS_OPERATION_RELEASE_TASKS


@pytest.mark.asyncio
async def test_failed_remote_cleanup_retains_owner_and_blocks_new_borrowers(
    resource_mgr, monkeypatch,
):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    release = AsyncMock(side_effect=RuntimeError("ACL revoke failed"))
    monkeypatch.setattr(jiuwenbox, "release_jiuwenbox_scope", release)
    first, second = _sandbox_owners(resource_mgr)
    await first._cleanup_sys_operations()
    with pytest.raises(RuntimeError, match="ACL revoke failed"):
        await second._cleanup_sys_operations()
    assert second._retained_sys_operation_ids == ["shared-sandbox"]
    assert resource_mgr.get_sys_operation("shared-sandbox") is not None
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        first._retain_sys_operation("shared-sandbox")
    assert first._retained_sys_operation_ids == []
    release.side_effect = None
    await second._cleanup_sys_operations()
    assert not second._retained_sys_operation_ids
    assert not interface_deep._SYS_OPERATION_RELEASE_TASKS


@pytest.mark.asyncio
async def test_cancelled_cleanup_keeps_remote_release_owned_until_complete(
    resource_mgr, monkeypatch,
):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    entered, finish = asyncio.Event(), asyncio.Event()

    async def release(*_args):
        entered.set()
        await finish.wait()

    monkeypatch.setattr(jiuwenbox, "release_jiuwenbox_scope", release)
    first, second = _sandbox_owners(resource_mgr)
    await first._cleanup_sys_operations()
    task = asyncio.create_task(second._cleanup_sys_operations())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resource_mgr.get_sys_operation("shared-sandbox") is not None
    pending = interface_deep._SYS_OPERATION_RELEASE_TASKS["shared-sandbox"]
    finish.set()
    await pending
    assert resource_mgr.get_sys_operation("shared-sandbox") is None


@pytest.mark.asyncio
async def test_root_cleanup_preserves_failed_child_for_retry_and_closes_own_resources():
    parent = _make_adapter()
    failed = SimpleNamespace(
        stop_interaction=AsyncMock(),
        cleanup=AsyncMock(side_effect=RuntimeError("ACL revoke failed")),
    )
    completed = SimpleNamespace(stop_interaction=AsyncMock(), cleanup=AsyncMock())
    parent._is_session_scoped_adapter = False
    parent._session_adapters = {"failed": failed, "completed": completed}
    for name in (
        "_session_adapter_locks", "_session_adapter_last_used",
        "_session_adapter_versions", "_session_adapter_reload_failures",
    ):
        setattr(parent, name, {"failed": object(), "completed": object()})
    parent._teardown_agent_owned_tools = Mock()
    parent._cleanup_sys_operations = AsyncMock(side_effect=RuntimeError("own scope failed"))
    parent._close_a2x_client = AsyncMock()
    card = parent._sys_operation_card = object()
    reindex = parent._memory_reindex_task = Mock()
    reindex.done.return_value = False

    with pytest.raises(RuntimeError, match="ACL revoke failed.*own scope failed"):
        await parent.cleanup()
    assert parent._session_adapters == {"failed": failed}
    assert parent._sys_operation_card is card
    assert "completed" not in parent._session_adapter_locks
    reindex.cancel.assert_called_once()
    parent._close_a2x_client.assert_awaited_once()

    failed.cleanup.side_effect = None
    parent._cleanup_sys_operations.side_effect = None
    await parent.cleanup()
    assert not parent._session_adapters
    assert not parent._session_adapter_locks
    assert parent._sys_operation_card is None
