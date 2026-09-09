# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.plugins.rail_manager import (
    RailManager,
    RailManagerPool,
    get_rail_manager,
)
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey
from tests.unit_tests.tenant_workspace_test_helpers import tenant_workspace_key


@pytest.fixture(autouse=True)
def _reset_rail_manager_pool():
    RailManagerPool.reset_for_tests()
    yield
    RailManagerPool.reset_for_tests()


def test_get_rail_manager_requires_scope():
    with pytest.raises(TypeError, match="requires a non-None scope"):
        get_rail_manager(None)


def test_rail_manager_pool_isolates_tenants(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")

    key_a = tenant_workspace_key("svc-a", "agent-1")
    key_b = tenant_workspace_key("svc-b", "agent-2")

    def _workspace(workspace_key: str) -> Path:
        mapping = {
            key_a: tmp_path / "tenant-a",
            key_b: tmp_path / "tenant-b",
        }
        return mapping.get(workspace_key, tmp_path / str(workspace_key))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.plugins.rail_manager.get_multi_tenant_user_workspace_dir",
        _workspace,
    )

    scope_a = RuntimeScopeKey.from_ids(
        "svc-a", "agent-1", workspace_key=key_a
    )
    scope_b = RuntimeScopeKey.from_ids(
        "svc-b", "agent-2", workspace_key=key_b
    )

    mgr_a = get_rail_manager(scope_a)
    mgr_b = get_rail_manager(scope_b)

    assert mgr_a is not mgr_b
    assert mgr_a.extensions_dir == tmp_path / "tenant-a" / "agent" / "jiuwenclaw_workspace" / "extensions"
    assert mgr_b.extensions_dir == tmp_path / "tenant-b" / "agent" / "jiuwenclaw_workspace" / "extensions"

    assert get_rail_manager(scope_a) is mgr_a
    assert get_rail_manager(
        ("svc-b", "agent-2", key_b)
    ) is mgr_b


def test_rail_manager_pool_remove_disposes():
    disposed: list[tuple[str, str]] = []
    original_dispose = RailManager.dispose

    def _track_dispose(self: RailManager) -> None:
        disposed.append((self.service_id, self.agent_id))
        original_dispose(self)

    RailManager.dispose = _track_dispose  # type: ignore[method-assign]
    try:
        scope = RuntimeScopeKey.from_ids("svc-x", "agent-y")
        mgr = get_rail_manager(scope)
        mgr._registered_rails.add("demo")

        assert RailManagerPool.remove("svc-x", "agent-y") is True
        assert disposed == [("svc-x", "agent-y")]
        assert RailManagerPool.remove("svc-x", "agent-y") is False

        fresh = get_rail_manager(scope)
        assert fresh is not mgr
        assert "demo" not in fresh._registered_rails
    finally:
        RailManager.dispose = original_dispose  # type: ignore[method-assign]
