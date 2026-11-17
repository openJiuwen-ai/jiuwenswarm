# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session swarmflow overlay must not clobber team-template fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.server.runtime.agent_adapter import team_helpers


def test_resolve_returns_none_without_session_override(monkeypatch):
    monkeypatch.setattr(team_helpers, "restore_session_swarmflow_config", lambda _sid: None)
    result = team_helpers._resolve_session_swarmflow_config(
        None,
        {
            "modes": {
                "team": {
                    "jiuwen_team": {"enable_swarmflow": True, "swarmflow_budget": 100},
                    "other_team": {"enable_swarmflow": False},
                }
            }
        },
        session_id="sess-1",
        project_dir=None,
    )
    assert result is None


def test_resolve_omits_worktree_when_project_dir_absent(monkeypatch):
    monkeypatch.setattr(team_helpers, "restore_session_swarmflow_config", lambda _sid: None)
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": True, "swarmflow_budget": 50},
        {"modes": {"team": {"jiuwen_team": {"enable_swarmflow": False}}}},
        session_id="sess-1",
        project_dir="",
    )
    assert result == {
        "enable_swarmflow": True,
        "swarmflow_budget": 50,
    }
    assert "swarmflow_worktree" not in result


def test_resolve_arms_worktree_when_project_dir_set(monkeypatch):
    monkeypatch.setattr(team_helpers, "restore_session_swarmflow_config", lambda _sid: None)
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": False},
        {},
        session_id="sess-1",
        project_dir="/tmp/proj",
    )
    assert result["enable_swarmflow"] is False
    assert result["swarmflow_worktree"] is True
    assert "swarmflow_budget" not in result


def test_resolve_omits_budget_when_params_have_no_budget(monkeypatch):
    """enable_swarmflow alone must not emit swarmflow_budget:null (CR-2)."""
    monkeypatch.setattr(team_helpers, "restore_session_swarmflow_config", lambda _sid: None)
    result = team_helpers._resolve_session_swarmflow_config(
        {"enable_swarmflow": True},
        {"modes": {"team": {"jiuwen_team": {"swarmflow_budget": 100}}}},
        session_id="sess-1",
        project_dir=None,
    )
    assert result == {"enable_swarmflow": True}
    assert "swarmflow_budget" not in result


def test_resolve_omits_budget_when_persisted_budget_null(monkeypatch):
    monkeypatch.setattr(
        team_helpers,
        "restore_session_swarmflow_config",
        lambda _sid: {"enable_swarmflow": True, "swarmflow_budget": None},
    )
    result = team_helpers._resolve_session_swarmflow_config(
        None,
        {},
        session_id="sess-1",
        project_dir=None,
    )
    assert result == {"enable_swarmflow": True}
    assert "swarmflow_budget" not in result


def test_resolve_uses_persisted_metadata(monkeypatch):
    monkeypatch.setattr(
        team_helpers,
        "restore_session_swarmflow_config",
        lambda _sid: {"enable_swarmflow": True, "swarmflow_budget": 9},
    )
    result = team_helpers._resolve_session_swarmflow_config(
        None,
        {"modes": {"team": {"jiuwen_team": {"enable_swarmflow": False}}}},
        session_id="sess-1",
        project_dir=None,
    )
    assert result == {
        "enable_swarmflow": True,
        "swarmflow_budget": 9,
    }


@pytest.mark.asyncio
async def test_overlay_preserves_team_fields_when_config_none(monkeypatch):
    manager = TeamManager.__new__(TeamManager)
    worktree = SimpleNamespace(enabled=True)

    async def _skip_pg(_config):
        return None

    monkeypatch.setattr(manager, "_ensure_postgresql_for_leader", _skip_pg)
    monkeypatch.setattr(
        manager,
        "_load_session_team_spec",
        lambda *args, **kwargs: (
            SimpleNamespace(
                enable_swarmflow=False,
                swarmflow_budget=123,
                worktree=worktree,
                team_name="other_team",
            ),
            True,
        ),
    )
    monkeypatch.setattr(manager, "_apply_session_scoped_team_name", lambda *a, **k: None)
    monkeypatch.setattr(manager, "apply_team_plan_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: spec,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )

    spec = await manager.get_swarm_enriched_team_spec(
        session_id="sess-1",
        mode="team",
        swarmflow_config=None,
    )
    assert spec.enable_swarmflow is False
    assert spec.swarmflow_budget == 123
    assert spec.worktree is worktree


@pytest.mark.asyncio
async def test_overlay_applies_only_present_keys(monkeypatch):
    manager = TeamManager.__new__(TeamManager)
    worktree = SimpleNamespace(enabled=True)

    async def _skip_pg(_config):
        return None

    monkeypatch.setattr(manager, "_ensure_postgresql_for_leader", _skip_pg)
    monkeypatch.setattr(
        manager,
        "_load_session_team_spec",
        lambda *args, **kwargs: (
            SimpleNamespace(
                enable_swarmflow=False,
                swarmflow_budget=123,
                worktree=worktree,
                team_name="other_team",
            ),
            True,
        ),
    )
    monkeypatch.setattr(manager, "_apply_session_scoped_team_name", lambda *a, **k: None)
    monkeypatch.setattr(manager, "apply_team_plan_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: spec,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )

    # worktree-only overlay (project_dir selected, no enable/budget override)
    spec = await manager.get_swarm_enriched_team_spec(
        session_id="sess-1",
        mode="team",
        swarmflow_config={"swarmflow_worktree": True},
    )
    assert spec.enable_swarmflow is False
    assert spec.swarmflow_budget == 123
    assert spec.worktree is not None
    assert spec.worktree.enabled is True


@pytest.mark.asyncio
async def test_overlay_null_budget_does_not_clear_team_budget(monkeypatch):
    """Legacy / buggy overlays with swarmflow_budget:null must keep team budget."""
    manager = TeamManager.__new__(TeamManager)

    async def _skip_pg(_config):
        return None

    monkeypatch.setattr(manager, "_ensure_postgresql_for_leader", _skip_pg)
    monkeypatch.setattr(
        manager,
        "_load_session_team_spec",
        lambda *args, **kwargs: (
            SimpleNamespace(
                enable_swarmflow=False,
                swarmflow_budget=123,
                worktree=None,
                team_name="other_team",
            ),
            True,
        ),
    )
    monkeypatch.setattr(manager, "_apply_session_scoped_team_name", lambda *a, **k: None)
    monkeypatch.setattr(manager, "apply_team_plan_mode", lambda *a, **k: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm",
        lambda spec, **kwargs: spec,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_config",
        lambda: {},
    )

    spec = await manager.get_swarm_enriched_team_spec(
        session_id="sess-1",
        mode="team",
        swarmflow_config={"enable_swarmflow": True, "swarmflow_budget": None},
    )
    assert spec.enable_swarmflow is True
    assert spec.swarmflow_budget == 123
