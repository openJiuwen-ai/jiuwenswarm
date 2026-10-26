# tests/unit_tests/test_team_snapshot_refresh.py
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for ``reconcile_session_team_snapshot`` drift reconciliation."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime import team_snapshot_refresh
from jiuwenswarm.agents.harness.team.config_loader import TeamTemplateNotFoundError
from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError

FROZEN = {
    "team_name": "t",
    "predefined_members": [{"member_name": "a"}],
    "agents": {},
}
LIVE_DRIFTED = {
    "team_name": "t",
    "predefined_members": [{"member_name": "a"}, {"member_name": "b"}],
    "agents": {},
}


def _patch_deps(
    monkeypatch,
    *,
    live=LIVE_DRIFTED,
    normalize_identity=True,
    normalize_raises=None,
    write_raises=None,
):
    def fake_get(config_base, *, template_id):
        if isinstance(live, BaseException):
            raise live
        return live

    monkeypatch.setattr(team_snapshot_refresh, "get_team_template_snapshot", fake_get)

    if normalize_raises is not None:
        def bad_normalize(snap, cb):
            raise normalize_raises

        monkeypatch.setattr(
            team_snapshot_refresh, "normalize_team_entity_snapshot", bad_normalize
        )
    elif normalize_identity:
        monkeypatch.setattr(
            team_snapshot_refresh, "normalize_team_entity_snapshot",
            lambda snap, cb: snap,
        )

    store = MagicMock()
    if write_raises is not None:
        store.write.side_effect = write_raises
    monkeypatch.setattr(team_snapshot_refresh, "get_team_entity_store", lambda: store)

    writer = MagicMock()
    monkeypatch.setattr(
        team_snapshot_refresh, "write_session_team_template_snapshot", writer
    )
    return store, writer


def _reconcile(monkeypatch, **live_kw):
    store, writer = _patch_deps(monkeypatch, **live_kw)
    from jiuwenswarm.server.runtime.team_snapshot_refresh import (
        reconcile_session_team_snapshot,
    )

    result = reconcile_session_team_snapshot(
        session_id="s1",
        team_name="t",
        template_id="t",
        frozen_snapshot=FROZEN,
        config_base={},
        sessions_root=None,
    )
    return result, store, writer


def test_drift_refreshes_both_and_returns_live(monkeypatch):
    result, store, writer = _reconcile(monkeypatch, live=LIVE_DRIFTED)
    assert result == LIVE_DRIFTED
    store.write.assert_called_once()
    writer.assert_called_once()


def test_no_drift_writes_nothing(monkeypatch):
    result, store, writer = _reconcile(monkeypatch, live=FROZEN)
    assert result == FROZEN
    store.write.assert_not_called()
    writer.assert_not_called()


def test_template_not_found_keeps_frozen(monkeypatch):
    result, store, writer = _reconcile(
        monkeypatch, live=TeamTemplateNotFoundError("nf")
    )
    assert result == FROZEN
    store.write.assert_not_called()
    writer.assert_not_called()


def test_normalize_error_keeps_frozen(monkeypatch):
    result, store, writer = _reconcile(
        monkeypatch, normalize_raises=TeamEntityStoreError("bad")
    )
    assert result == FROZEN
    store.write.assert_not_called()
    writer.assert_not_called()


def test_write_oserror_keeps_frozen(monkeypatch):
    result, store, writer = _reconcile(
        monkeypatch, live=LIVE_DRIFTED, write_raises=OSError("disk full")
    )
    assert result == FROZEN


# ---- resolve_dissolve_keep_members ----------------------------------------

SNAP = {
    "leader": {"member_name": "office", "display_name": "通用助手"},
    "predefined_members": [
        {"member_name": "assistant"},
        {"member_name": "agentteams"},
        {"member_name": ""},  # skipped: no member_name (mirrors config_loader)
        {"display_name": "no-mn"},  # skipped: no member_name
    ],
}
STALE_FROZEN = {
    "leader": {"member_name": "office"},
    "predefined_members": [{"member_name": "assistant"}, {"member_name": "ghost"}],
}


def _patch_resolve(
    monkeypatch,
    *,
    frozen=None,
    inline=None,
    entity_snapshot=None,
    reconcile_return=None,
    reconcile_raises=None,
    live=None,
    normalize=None,
):
    """Patch the deps ``resolve_dissolve_keep_members`` reads.

    ``get_session_metadata`` / ``get_session_team_template_snapshot`` are
    lazy-imported inside the function, so they are patched on the
    ``session_metadata`` module; the rest are patched on ``team_snapshot_refresh``.
    """
    import jiuwenswarm.server.runtime.session.session_metadata as sm

    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, **kw: (
            {"team_template_snapshot": inline} if inline is not None else {}
        ),
    )
    monkeypatch.setattr(
        sm,
        "get_session_team_template_snapshot",
        lambda session_id, **kw: frozen,
    )
    store = MagicMock()
    if entity_snapshot is not None:
        entity = MagicMock()
        entity.template_snapshot = entity_snapshot
        store.get.return_value = entity
    else:
        store.get.return_value = None
    monkeypatch.setattr(team_snapshot_refresh, "get_team_entity_store", lambda: store)

    if reconcile_raises is not None:
        def fake_reconcile(**kw):  # noqa: ANN002
            raise reconcile_raises

        monkeypatch.setattr(
            team_snapshot_refresh, "reconcile_session_team_snapshot", fake_reconcile
        )
    elif reconcile_return is not None:
        monkeypatch.setattr(
            team_snapshot_refresh,
            "reconcile_session_team_snapshot",
            lambda **kw: reconcile_return,
        )

    if live is not None:
        monkeypatch.setattr(
            team_snapshot_refresh,
            "get_team_template_snapshot",
            lambda config_base, *, template_id: live,
        )
    if normalize is not None:
        monkeypatch.setattr(
            team_snapshot_refresh,
            "normalize_team_entity_snapshot",
            lambda snap, cb: normalize,
        )
    return store


def _resolve_keep(monkeypatch, **patch_kw):
    _patch_resolve(monkeypatch, **patch_kw)
    from jiuwenswarm.server.runtime.team_snapshot_refresh import (
        resolve_dissolve_keep_members,
    )
    return resolve_dissolve_keep_members(
        session_id="s1",
        team_name="t",
        template_id="t",
        config_base={},
        sessions_root=None,
    )


def test_resolve_drift_returns_live_keep(monkeypatch):
    # Frozen is stale (has "ghost"); reconcile returns the drifted live SNAP.
    keep = _resolve_keep(monkeypatch, frozen=STALE_FROZEN, reconcile_return=SNAP)
    assert keep == {"office", "assistant", "agentteams"}


def test_resolve_no_frozen_uses_live(monkeypatch):
    keep = _resolve_keep(monkeypatch, live=SNAP, normalize=SNAP)
    assert keep == {"office", "assistant", "agentteams"}


def test_resolve_template_not_found_returns_none(monkeypatch):
    def raise_not_found(config_base, *, template_id):
        raise TeamTemplateNotFoundError("nf")

    _patch_resolve(monkeypatch)  # no frozen, no live, no normalize
    monkeypatch.setattr(
        team_snapshot_refresh, "get_team_template_snapshot", raise_not_found
    )
    from jiuwenswarm.server.runtime.team_snapshot_refresh import (
        resolve_dissolve_keep_members,
    )
    keep = resolve_dissolve_keep_members(
        session_id="s1", team_name="t", template_id="t",
        config_base={}, sessions_root=None,
    )
    assert keep is None


def test_resolve_reconcile_raises_returns_none(monkeypatch):
    keep = _resolve_keep(
        monkeypatch, frozen=STALE_FROZEN, reconcile_raises=RuntimeError("boom")
    )
    assert keep is None


def test_resolve_missing_template_id_returns_none():
    from jiuwenswarm.server.runtime.team_snapshot_refresh import (
        resolve_dissolve_keep_members,
    )
    keep = resolve_dissolve_keep_members(
        session_id="s1", team_name="t", template_id="",
        config_base={}, sessions_root=None,
    )
    assert keep is None


def test_resolve_empty_keep_set_returns_none(monkeypatch):
    empty_leader_snap = {"leader": {}, "predefined_members": []}
    keep = _resolve_keep(monkeypatch, live=empty_leader_snap, normalize=empty_leader_snap)
    assert keep is None


def test_resolve_accepts_handler_metadata_and_skips_reread(monkeypatch):
    # The handler already holds metadata; passing it must avoid a redundant
    # get_session_metadata re-read.
    import jiuwenswarm.server.runtime.session.session_metadata as sm

    def boom(session_id, **kw):  # noqa: ANN001
        raise AssertionError("get_session_metadata must not run when metadata is passed")

    _patch_resolve(monkeypatch, frozen=SNAP, reconcile_return=SNAP)
    monkeypatch.setattr(sm, "get_session_metadata", boom)
    from jiuwenswarm.server.runtime.team_snapshot_refresh import (
        resolve_dissolve_keep_members,
    )
    keep = resolve_dissolve_keep_members(
        session_id="s1", team_name="t", template_id="t",
        config_base={}, sessions_root=None,
        metadata={"team_template_snapshot": None},
    )
    assert keep == {"office", "assistant", "agentteams"}
