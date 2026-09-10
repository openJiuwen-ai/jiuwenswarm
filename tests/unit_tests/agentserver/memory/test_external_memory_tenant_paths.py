# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for external memory tenant LTM paths and MEMORY_* tip priority."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.agents.harness.common.memory import external_memory_config as emc
from jiuwenswarm.common.local_env_config import (
    bind_task_env_overlay,
    reset_local_env_state_for_tests,
    reset_task_env_overlay,
)
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
)


def test_resolve_ltm_dir_isolates_tenants(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        emc,
        "get_multi_tenant_user_workspace_dir",
        lambda workspace_key: tmp_path / f"workspace_{workspace_key}",
    )
    office = emc._resolve_ltm_dir(workspace_key="office")
    default = emc._resolve_ltm_dir(workspace_key="default")
    assert office != default
    assert "workspace_office" in str(office)
    assert office.name == "ltm"
    assert office.exists()


def test_resolve_openjiuwen_store_paths_tip_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(
        emc,
        "_resolve_ltm_dir",
        lambda *a, **k: tmp_path / "ltm",
    )
    reset_local_env_state_for_tests()
    token = bind_task_env_overlay(
        {
            "MEMORY_KV_PATH": str(tmp_path / "tip_kv"),
            "MEMORY_VECTOR_DIR": str(tmp_path / "tip_vec"),
            "MEMORY_DB_PATH": str(tmp_path / "tip.db"),
        }
    )
    try:
        kv, vec, db = emc.resolve_openjiuwen_store_paths({})
        assert kv == str(tmp_path / "tip_kv")
        assert vec == str(tmp_path / "tip_vec")
        assert db == str(tmp_path / "tip.db")
    finally:
        reset_task_env_overlay(token)
        reset_local_env_state_for_tests()


def test_resolve_openjiuwen_store_paths_falls_back_to_tenant_ltm(tmp_path, monkeypatch):
    monkeypatch.setattr(
        emc,
        "_resolve_ltm_dir",
        lambda *, workspace_key=None: tmp_path / (workspace_key or "default") / "ltm",
    )
    reset_local_env_state_for_tests()
    kv, vec, db = emc.resolve_openjiuwen_store_paths(
        {},
        workspace_key="svc_bot",
    )
    assert kv.endswith(str(Path("svc_bot") / "ltm" / "kv"))
    assert vec.endswith(str(Path("svc_bot") / "ltm" / "chroma"))
    assert db.endswith(str(Path("svc_bot") / "ltm" / "ltm.db"))


def test_resolve_ltm_dir_uses_workspace_key(tmp_path, monkeypatch):
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        emc,
        "get_multi_tenant_user_workspace_dir",
        lambda workspace_key: tmp_path / f"workspace_{workspace_key}",
    )
    path = emc._resolve_ltm_dir(workspace_key="bound_aid")
    assert "workspace_bound_aid" in str(path)
    assert path.name == "ltm"


def test_resolve_ltm_dir_reads_bound_workspace_key(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.tenant_context import (
        bind_workspace_key,
        clear_tenant_bindings,
        reset_workspace_key,
    )

    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        emc,
        "get_multi_tenant_user_workspace_dir",
        lambda workspace_key: tmp_path / f"workspace_{workspace_key}",
    )
    clear_tenant_bindings()
    token = bind_workspace_key("bound_wk")
    try:
        path = emc._resolve_ltm_dir()
        assert "workspace_bound_wk" in str(path)
        assert path.name == "ltm"
    finally:
        reset_workspace_key(token)
        clear_tenant_bindings()
