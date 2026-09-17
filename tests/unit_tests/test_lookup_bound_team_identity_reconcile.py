# tests/unit_tests/test_lookup_bound_team_identity_reconcile.py
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests that _lookup_bound_team_identity reconciles drift on rebuild."""
from __future__ import annotations

from unittest.mock import patch

from jiuwenswarm.agents.harness.team.team_manager import TeamManager

SESSION_ID = "unit_test_session_001"
FROZEN = {
    "team_name": "t",
    "predefined_members": [{"member_name": "a"}],
    "agents": {},
}
RECONCILED = {
    "team_name": "t",
    "predefined_members": [{"member_name": "b"}],
    "agents": {},
}


def _patch_meta(monkeypatch, *, team_name="t", template_id="t", frozen=FROZEN):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda sid, **kw: {
            "team_name": team_name,
            "team_template_id": template_id,
            "runtime_team_name": team_name,
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_team_template_snapshot",
        lambda sid, **kw: frozen,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.resolve_session_runtime_team_name",
        lambda meta: team_name,
    )


def test_reconcile_called_and_its_result_returned(monkeypatch):
    _patch_meta(monkeypatch)
    monkeypatch.setattr(
        TeamManager,
        "_find_session_binding",
        staticmethod(lambda _session_id: None),
    )
    with patch(
        "jiuwenswarm.server.runtime.team_snapshot_refresh.reconcile_session_team_snapshot",
        return_value=RECONCILED,
    ) as mock_recon, patch(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store"
    ) as mock_bs:
        mock_bs.return_value.get.return_value = None
        _tn, _rtn, _tid, snapshot = TeamManager._lookup_bound_team_identity(
            SESSION_ID, config_base={}
        )
    mock_recon.assert_called_once()
    assert mock_recon.call_args.kwargs["frozen_snapshot"] is FROZEN
    assert snapshot is RECONCILED


def test_no_frozen_snapshot_skips_reconcile(monkeypatch):
    _patch_meta(monkeypatch, frozen=None, template_id="")
    monkeypatch.setattr(
        TeamManager,
        "_find_session_binding",
        staticmethod(lambda _session_id: None),
    )
    with patch(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store"
    ) as mock_bs, patch(
        "jiuwenswarm.server.runtime.team_snapshot_refresh.reconcile_session_team_snapshot"
    ) as mock_recon:
        mock_bs.return_value.get.return_value = None
        _tn, _rtn, _tid, snapshot = TeamManager._lookup_bound_team_identity(
            SESSION_ID, config_base={}
        )
    mock_recon.assert_not_called()
    assert snapshot is None
