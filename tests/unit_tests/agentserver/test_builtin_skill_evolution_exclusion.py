# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for excluding package builtin skills from self-evolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers
from jiuwenswarm.server.runtime.agent_adapter.evolution_slash import (
    EvolutionSlashContext,
    handle_evolution_slash_command,
)


@pytest.mark.unit
def test_merge_evolution_disabled_skills_includes_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "get_builtin_skill_names",
        lambda: {"xlsx", "skill-creator"},
    )
    assert evolution_helpers.merge_evolution_disabled_skills(["custom-off"]) == [
        "custom-off",
        "skill-creator",
        "xlsx",
    ]


@pytest.mark.unit
def test_filter_evolution_eligible_skill_names_excludes_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "is_builtin_skill",
        lambda name: name == "xlsx",
    )
    assert evolution_helpers.filter_evolution_eligible_skill_names(
        ["xlsx", "custom-skill"]
    ) == ["custom-skill"]


@pytest.mark.unit
def test_validate_evolution_skill_rejects_builtin(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "is_builtin_skill",
        lambda name: name == "xlsx",
    )
    error = evolution_helpers.validate_evolution_skill(
        store=SimpleNamespace(),
        skill_name="xlsx",
        require_skill_md=False,
    )
    assert error is not None
    assert "内置官方技能" in error
    assert "不参与" in error


@pytest.mark.unit
def test_sync_evolution_disabled_skills_keeps_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "get_builtin_skill_names",
        lambda: {"xlsx"},
    )
    rail = SimpleNamespace(disabled_skills={"old"})
    evolution_helpers.sync_evolution_disabled_skills(rail, ["manual-off"])
    assert rail.disabled_skills == {"manual-off", "xlsx"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_evolve_slash_rejects_builtin_skill(monkeypatch, tmp_path):
    skill_dir = tmp_path / "skills" / "xlsx"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text("# xlsx\n", encoding="utf-8")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.evolution_helpers.is_builtin_skill",
        lambda name: name == "xlsx",
    )

    result = await handle_evolution_slash_command(
        "/evolve xlsx improve charts",
        EvolutionSlashContext(
            mode="agent",
            session_id="s1",
            skills_dir=str(tmp_path / "skills"),
            evolution_enabled=True,
        ),
    )
    assert result is not None
    assert result["result_type"] == "error"
    assert "内置官方技能" in result["output"]
