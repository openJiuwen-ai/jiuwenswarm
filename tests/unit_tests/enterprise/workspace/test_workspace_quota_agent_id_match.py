# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``agent_id`` participates in workspace quota policy matching (issue #124).

These tests do NOT mock ``_pick_policy`` — they let the real ``matches`` run
against an agent-scoped policy, so that a regression in any of the three
``_snapshot_for_used`` call sites (used_bytes direct / cache hit / need_du
re-measure) would surface as the agent policy being silently skipped and the
fallback policy (10 TiB) being applied instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.enterprise.capabilities.workspace import quota as quota_mod
from jiuwenswarm.enterprise.capabilities.workspace.quota import (
    clear_used_bytes_cache,
    measure_and_cache,
    resolve_effective_quota,
    set_cached_used,
    set_db_policy_cache,
)


# An agent-scoped policy: only matches when agent_id == "a1"; small limit so
# any non-trivial used bytes land in "warn"/"block". A regression that drops
# agent_id would fall back to fallback_limit_bytes() (10 TiB) → status "ok".
_AGENT_POLICY = {
    "policy_id": "agent-a1-policy",
    "enabled": True,
    "priority": 1,
    "match_expr": 'agent_id in ("a1",)',
    "limit_bytes": 1000,
    "soft_percent": 80,
    "hard_percent": 100,
}


@pytest.fixture(autouse=True)
def _quota_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WORKSPACE_QUOTA_ENABLED", "true")
    monkeypatch.setattr(quota_mod, "is_workspace_quota_enabled", lambda: True)
    clear_used_bytes_cache()
    set_db_policy_cache([_AGENT_POLICY])
    # NOTE: deliberately NOT mocking _pick_policy — let matches() run for real.
    monkeypatch.setattr(
        quota_mod,
        "resolve_tenant_root",
        lambda tenant_root=None: Path(tenant_root) if tenant_root else tmp_path,
    )
    yield
    clear_used_bytes_cache()
    set_db_policy_cache([])
    quota_mod._DB_LOADED = False


def test_used_bytes_direct_branch_matches_agent_policy(tmp_path: Path) -> None:
    """Branch 1: ``used_bytes is not None`` → _snapshot_for_used (quota.py:644)."""
    snap = resolve_effective_quota(
        user_id="u1", group_id="g1", bot_id="b1", agent_id="a1",
        used_bytes=900, tenant_root=tmp_path,
    )
    assert snap.source_policy_id == "agent-a1-policy"
    assert snap.limit_bytes == 1000
    assert snap.status == "warn"  # 900/1000 = 90% > 80% soft


def test_used_bytes_direct_branch_skips_when_agent_id_absent(tmp_path: Path) -> None:
    """Without agent_id the agent-scoped policy must NOT match (fallback)."""
    snap = resolve_effective_quota(
        user_id="u1", group_id="g1", bot_id="b1", agent_id="",
        used_bytes=900, tenant_root=tmp_path,
    )
    assert snap.source_policy_id != "agent-a1-policy"


def test_cache_hit_branch_matches_agent_policy(monkeypatch, tmp_path: Path) -> None:
    """Branch 2: cache hit, need_du=False → _snapshot_for_used (quota.py:651).

    A cached used well under the agent policy's 80% soft → status ok, but the
    source_policy_id must still be the agent policy (not fallback).
    """
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 100)
    set_cached_used(tmp_path, 100, user_id="u1", bot_id="b1")
    snap = resolve_effective_quota(
        user_id="u1", group_id="g1", bot_id="b1", agent_id="a1",
        tenant_root=tmp_path,
    )
    assert snap.source_policy_id == "agent-a1-policy"
    assert snap.used_bytes == 100
    assert snap.status == "ok"


def test_need_du_branch_matches_agent_policy(monkeypatch, tmp_path: Path) -> None:
    """Branch 3: need_du=True → measure_and_cache → _snapshot_for_used (quota.py:660).

    No cache → need_du triggers measure_and_cache(→900); agent policy must
    still be the one selected (status warn at 90%).
    """
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 900)
    snap = resolve_effective_quota(
        user_id="u1", group_id="g1", bot_id="b1", agent_id="a1",
        tenant_root=tmp_path,
    )
    assert snap.source_policy_id == "agent-a1-policy"
    assert snap.used_bytes == 900
    assert snap.status == "warn"


def test_force_refresh_branch_matches_agent_policy(monkeypatch, tmp_path: Path) -> None:
    """force_refresh=True forces need_du even with a cached ok value."""
    monkeypatch.setattr(quota_mod, "measure_used_bytes", lambda _r: 950)
    set_cached_used(tmp_path, 100, user_id="u1", bot_id="b1")  # stale cache
    snap = resolve_effective_quota(
        user_id="u1", group_id="g1", bot_id="b1", agent_id="a1",
        tenant_root=tmp_path, force_refresh=True,
    )
    assert snap.source_policy_id == "agent-a1-policy"
    assert snap.used_bytes == 950
    assert snap.status == "warn"


def test_resolve_effective_quota_reads_agent_id_from_tenant_context(tmp_path: Path) -> None:
    """When agent_id kwarg is absent, fall back to TenantContext.current_agent_id().

    This covers the write-path gate (check_path_write / fs_quota_guard) which
    does not carry the request envelope but inherits the TenantContext CV
    snapshot bound at bind_request time (issue #124 P2 #1).
    """
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    with TenantContext.scoped(
        "tk", group_id="g1", bot_id="b1", user_id="u1", agent_id="a1",
    ):
        snap = resolve_effective_quota(tenant_root=tmp_path)
    assert snap.source_policy_id == "agent-a1-policy"


def test_check_path_write_uses_tenant_context_agent_id(tmp_path: Path) -> None:
    """check_path_write must read agent_id from TenantContext (issue #124 P2 #1).

    Without this, the write gate falls back to the fallback policy while the
    session.create gate uses the agent policy — a quota-policy split.
    """
    from jiuwenswarm.enterprise.capabilities.workspace.quota import check_path_write
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    # Avoid real du: cache 0 used so need_du stays False (status ok at 0/1000).
    set_cached_used(tmp_path, 0, user_id="u1", bot_id="b1")
    with TenantContext.scoped(
        "tk", group_id="g1", bot_id="b1", user_id="u1", agent_id="a1",
    ):
        # 100 bytes → 10% of agent policy's 1000 → ok, agent policy selected.
        decision = check_path_write(
            "file.txt", "x" * 100, tenant_root=tmp_path,
        )
    assert decision.snapshot is not None
    assert decision.snapshot.source_policy_id == "agent-a1-policy"
    assert decision.snapshot.status == "ok"
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_fs_quota_guard_uses_tenant_context_agent_id(tmp_path: Path) -> None:
    """fs_quota_guard must read agent_id from TenantContext (issue #124 P2 #1).

    The guard wraps fs().write_file; when write_file is called during agent
    async tool execution, the request envelope is gone but TenantContext CV
    (bound at bind_request) is inherited by the async task.
    """
    from types import SimpleNamespace

    from jiuwenswarm.enterprise.capabilities.workspace.fs_quota_guard import (
        install_write_quota_guard,
    )
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    # Avoid real du: cache 0 used so need_du stays False.
    set_cached_used(tmp_path, 0, user_id="u1", bot_id="b1")
    written: list[bool] = []

    class _FakeFS:
        async def write_file(self, *args, **kwargs):
            written.append(True)
            return {"ok": True}

    fs = _FakeFS()
    sys_op = SimpleNamespace(fs=lambda: fs)
    install_write_quota_guard(sys_op)

    with TenantContext.scoped(
        "tk", group_id="g1", bot_id="b1", user_id="u1", agent_id="a1",
    ):
        # 100 bytes → 10% of agent policy's 1000 → ok, allowed.
        await fs.write_file("file.txt", "x" * 100)
    assert written == [True]


def test_nested_tenant_scope_preserves_outer_agent_id() -> None:
    """Inner TenantContext.bind must not clear the outer agent_id (CR-11164-03).

    Simulates the bind_request -> tenant_scope nesting: bind_request binds
    agent_id="a1", then Pool.tenant_scope rebinds the same tenant_key + triple.
    Before the fix, tenant_scope's bind omitted agent_id, so
    _TENANT_AGENT_ID_CV.set("") clobbered the outer value. fs_quota_guard
    reads current_agent_id() during agent execution inside tenant_scope, so
    an empty value means agent-scoped policies never match on the write-gate
    path (fallback to 10 TiB).

    This test asserts the fixed behavior: inner bind that re-passes agent_id
    preserves the outer value, while inner bind that omits agent_id (the
    pre-fix bug) clears it.
    """
    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

    # Outer bind (bind_request): agent_id="a1"
    with TenantContext.scoped(
        "tk", group_id="g1", bot_id="b1", user_id="u1", agent_id="a1",
    ):
        assert TenantContext.current_agent_id() == "a1"

        # Fixed tenant_scope: rebinds with agent_id=extract_agent_id(request)
        # (which yields "a1" again). The outer value is preserved.
        inner_ctx = TenantContext.bind(
            "tk", group_id="g1", bot_id="b1", user_id="u1", agent_id="a1",
        )
        try:
            assert TenantContext.current_agent_id() == "a1"
        finally:
            inner_ctx.reset()

        # Pre-fix regression check: inner bind WITHOUT agent_id clobbers
        # the outer value (this is the bug CR-11164-03 caught).
        buggy_ctx = TenantContext.bind(
            "tk", group_id="g1", bot_id="b1", user_id="u1",
        )
        try:
            assert TenantContext.current_agent_id() == ""
        finally:
            buggy_ctx.reset()

        # After buggy inner scope reset, outer scoped value is restored.
        assert TenantContext.current_agent_id() == "a1"
