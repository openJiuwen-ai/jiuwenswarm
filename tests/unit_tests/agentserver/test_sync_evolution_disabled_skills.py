# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for evolution rail disabled_skills sync (execution-disabled ∪ package builtins)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers


@pytest.mark.unit
def test_evolution_disabled_skill_names_unions_package_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator", "swarmskill-creator"},
    )
    assert evolution_helpers.evolution_disabled_skill_names(["manual-off"]) == {
        "manual-off",
        "skill-creator",
        "swarmskill-creator",
    }
    assert evolution_helpers.evolution_disabled_skill_names(None) == {
        "skill-creator",
        "swarmskill-creator",
    }


@pytest.mark.unit
def test_sync_evolution_disabled_skills_updates_set_in_place(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: set(),
    )
    rail = SimpleNamespace(disabled_skills={"old"})
    evolution_helpers.sync_evolution_disabled_skills(rail, ["manual-off", "xlsx"])
    assert rail.disabled_skills == {"manual-off", "xlsx"}


@pytest.mark.unit
def test_sync_evolution_disabled_skills_keeps_package_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator"},
    )
    rail = SimpleNamespace(disabled_skills=set())
    evolution_helpers.sync_evolution_disabled_skills(rail, ["manual-off"])
    assert rail.disabled_skills == {"manual-off", "skill-creator"}


@pytest.mark.unit
def test_sync_evolution_disabled_skills_ignores_non_set(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator"},
    )
    rail = SimpleNamespace(disabled_skills=["not-a-set"])
    evolution_helpers.sync_evolution_disabled_skills(rail, ["manual-off"])
    assert rail.disabled_skills == ["not-a-set"]


@pytest.mark.unit
def test_get_builtin_skill_names_lists_package_dirs(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils as utils_mod

    root = tmp_path / "skills"
    (root / "skill-creator").mkdir(parents=True)
    (root / "swarmskill-creator").mkdir()
    (root / "README.txt").write_text("ignore", encoding="utf-8")
    monkeypatch.setattr(utils_mod, "get_builtin_skills_dir", lambda: root)
    assert utils_mod.get_builtin_skill_names() == {
        "skill-creator",
        "swarmskill-creator",
    }


@pytest.mark.unit
def test_is_package_builtin_skill(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator"},
    )
    assert evolution_helpers.is_package_builtin_skill("skill-creator") is True
    assert evolution_helpers.is_package_builtin_skill("user-skill") is False
    assert evolution_helpers.is_package_builtin_skill("") is False


@pytest.mark.unit
def test_validate_evolution_skill_rejects_package_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator", "swarmskill-creator"},
    )
    store = SimpleNamespace(skill_exists=lambda _name: True)
    err = evolution_helpers.validate_evolution_skill(
        store, "skill-creator", require_skill_md=True
    )
    assert err is not None
    assert "skill-creator" in err
    assert "不支持自演进" in err
    assert (
        evolution_helpers.validate_evolution_skill(
            store, "user-skill", require_skill_md=False
        )
        is None
    )


@pytest.mark.unit
def test_available_skill_names_omits_package_builtins(monkeypatch):
    monkeypatch.setattr(
        evolution_helpers,
        "_safe_package_builtin_skill_names",
        lambda: {"skill-creator"},
    )
    monkeypatch.setattr(
        evolution_helpers,
        "filter_visible_skill_names",
        lambda names: names,
    )
    store = SimpleNamespace(
        list_skill_names=lambda: ["skill-creator", "user-skill"],
    )
    assert evolution_helpers._available_skill_names(store) == "user-skill"
