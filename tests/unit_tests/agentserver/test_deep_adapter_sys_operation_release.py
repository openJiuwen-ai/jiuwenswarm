# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for adapter-owned SysOperation release on cleanup."""

from __future__ import annotations

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
    adapter._local_sys_operation = None
    adapter._sys_operation = None
    adapter._agent_id = "test-agent"
    return adapter


@pytest.fixture(name="resource_mgr")
def _resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    fake = _FakeResourceMgr()
    monkeypatch.setattr(interface_deep.Runner, "resource_mgr", fake)
    interface_deep._SYS_OPERATION_REFCOUNTS.clear()
    yield fake
    interface_deep._SYS_OPERATION_REFCOUNTS.clear()


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


def test_local_sysop_survives_agent_sysop_rebuild(
    resource_mgr: _FakeResourceMgr,
) -> None:
    """沙箱 agent sysop 重建时，读盘用的本地 sysop 不得被连带注销。

    回归：``_create_sys_operation`` 曾 release 全部 previously_retained，
    把仍被 rail 使用的 local sysop 卸掉；缓存命中路径又不再 retain。
    """
    adapter = _make_adapter()
    adapter._local_sys_operation = None
    adapter._agent_id = "agent-1"

    sandbox = resource_mgr.register(_FakeSysOperation("sandbox_1"))
    local = resource_mgr.register(_FakeSysOperation("local_1"))
    adapter._resolve_sys_operation = lambda: sandbox

    assert adapter._create_sys_operation() is sandbox
    adapter._sys_operation = sandbox
    adapter._retain_sys_operation("local_1")
    adapter._local_sys_operation = local
    assert adapter._retained_sys_operation_ids == ["sandbox_1", "local_1"]

    # 模拟二次 create_instance：只重建 agent sysop
    assert adapter._create_sys_operation() is sandbox

    assert "local_1" in adapter._retained_sys_operation_ids
    assert resource_mgr.get_sys_operation("local_1") is local
    assert "local_1" not in resource_mgr.removed
    assert adapter._local_sys_operation is local

    # 缓存命中仍可用，且仍在 retain 列表里
    reused = adapter._create_local_sys_operation()
    assert reused is local
    assert "local_1" in adapter._retained_sys_operation_ids


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
