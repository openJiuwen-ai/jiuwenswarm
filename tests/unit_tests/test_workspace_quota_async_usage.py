# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""配额用量：定时缓存 + 近限同步 du + 手动强制刷新。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.workspace import quota as quota_mod
from jiuwenswarm.common.workspace.quota import (
    WorkspaceQuotaExceeded,
    check_workspace_write,
    clear_used_bytes_cache,
    get_cached_used,
    list_usage_reconcile_targets,
    mark_usage_active,
    measure_and_cache,
    reconcile_interval_seconds,
    resolve_effective_quota,
    set_cached_used,
    set_db_policy_cache,
)


@pytest.fixture(autouse=True)
def _quota_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WORKSPACE_QUOTA_ENABLED", "true")
    clear_used_bytes_cache()
    policy = {
        "policy_id": "p1",
        "enabled": True,
        "priority": 1,
        "match_expr": True,
        "limit_bytes": 1000,
        "soft_percent": 80,
        "hard_percent": 100,
    }
    set_db_policy_cache([policy])
    monkeypatch.setattr(quota_mod, "_pick_policy", lambda *_a, **_k: policy)
    monkeypatch.setattr(
        quota_mod,
        "resolve_tenant_root",
        lambda tenant_root=None: Path(tenant_root) if tenant_root else tmp_path,
    )
    yield
    clear_used_bytes_cache()
    set_db_policy_cache([])
    quota_mod._DB_LOADED = False


def test_default_reconcile_interval_is_five_minutes() -> None:
    assert reconcile_interval_seconds() == 300.0


def test_ok_path_reads_cache_without_du(monkeypatch, tmp_path: Path) -> None:
    called = {"n": 0}

    def _boom(_root):
        called["n"] += 1
        raise AssertionError("du must not run on ok path")

    monkeypatch.setattr(quota_mod, "measure_used_bytes", _boom)
    set_cached_used(tmp_path, 100, user_id="u1", bot_id="b1")
    snap = resolve_effective_quota(user_id="u1", bot_id="b1", tenant_root=tmp_path)
    assert snap.used_bytes == 100
    assert snap.status == "ok"
    check_workspace_write(additional_bytes=10, tenant_root=tmp_path)
    assert called["n"] == 0


def test_near_limit_syncs_du(monkeypatch, tmp_path: Path) -> None:
    called = {"n": 0}

    def _du(_root):
        called["n"] += 1
        return 850

    monkeypatch.setattr(quota_mod, "measure_used_bytes", _du)
    set_cached_used(tmp_path, 900)  # warn on cache
    snap = resolve_effective_quota(user_id="u1", bot_id="b1", tenant_root=tmp_path)
    assert called["n"] == 1
    assert snap.used_bytes == 850
    assert snap.status == "warn"


def test_display_path_skips_near_limit_du(monkeypatch, tmp_path: Path) -> None:
    called = {"n": 0}

    def _boom(_root):
        called["n"] += 1
        raise AssertionError("display must not sync du")

    monkeypatch.setattr(quota_mod, "measure_used_bytes", _boom)
    set_cached_used(tmp_path, 900)
    snap = resolve_effective_quota(
        user_id="u1",
        bot_id="b1",
        tenant_root=tmp_path,
        sync_on_near_limit=False,
    )
    assert snap.used_bytes == 900
    assert called["n"] == 0


def test_force_refresh_runs_du(monkeypatch, tmp_path: Path) -> None:
    called = {"n": 0}

    def _du(_root):
        called["n"] += 1
        return 123

    monkeypatch.setattr(quota_mod, "measure_used_bytes", _du)
    set_cached_used(tmp_path, 10)
    snap = resolve_effective_quota(
        user_id="u1",
        bot_id="b1",
        tenant_root=tmp_path,
        force_refresh=True,
        sync_on_near_limit=False,
    )
    assert called["n"] == 1
    assert snap.used_bytes == 123
    assert get_cached_used(tmp_path) == 123


def test_gate_blocks_after_near_limit_du(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 1000)
    set_cached_used(tmp_path, 950)
    with pytest.raises(WorkspaceQuotaExceeded):
        check_workspace_write(additional_bytes=1, tenant_root=tmp_path)


def test_active_mark_enqueues_reconcile(tmp_path: Path) -> None:
    mark_usage_active(tmp_path, user_id="u", bot_id="b")
    targets = list_usage_reconcile_targets(min_interval_seconds=0)
    assert any(t.root == str(tmp_path.resolve()) for t in targets)


@pytest.mark.asyncio
async def test_reconciler_updates_cache(monkeypatch, tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.workspace import usage_reconciler as ur

    mark_usage_active(tmp_path, user_id="u1", bot_id="b1")
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 77)
    pushed: list[dict] = []

    async def _push(**kwargs):
        pushed.append(kwargs)

    monkeypatch.setattr(ur, "_push_gateway_usage", _push)
    entry = list_usage_reconcile_targets(min_interval_seconds=0)[0]
    await ur._reconcile_one(entry)
    assert get_cached_used(tmp_path) == 77
    assert pushed and pushed[0]["used_bytes"] == 77


def test_measure_and_cache_helper(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 55)
    assert measure_and_cache(tmp_path, user_id="u", bot_id="b") == 55
    assert get_cached_used(tmp_path) == 55
