# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for design_rail.config_loader — YAML load + 5-rule validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.config_loader import (
    ConfigLoadError,
    load,
    validate,
)

pytestmark = [pytest.mark.unit]


def _valid_config() -> dict:
    """A config that mirrors design_rail/config.yaml and passes all rules."""
    return {
        "stages": {
            "init": {"next": ["analysis"]},
            "analysis": {
                "skill": "aet-req-analysis",
                "artifacts": ["requirements-analysis.md"],
                "next": ["analysis_review"],
            },
            "analysis_review": {
                "skill": "aet-req-review",
                # no artifacts — review is interactive (ask_user), not a file
                "next": ["design"],
            },
            "design": {
                "skill": "aet-req-design",
                "artifacts": ["requirements-design.md"],
                "next": ["design_review"],
            },
            "design_review": {
                "skill": "aet-req-review",
                # no artifacts — review is interactive (ask_user), not a file
                "next": ["done"],
            },
            "done": {"next": []},
        },
        "skills_dir": "skills/",
        "priority": 60,
    }


def test_validate_valid_config() -> None:
    """A well-formed config passes validation."""
    result = validate(_valid_config())
    assert result.ok is True
    assert result.errors == []


def test_validate_missing_core_stage() -> None:
    """Missing a core stage -> error contains 'missing_core_stages'."""
    cfg = _valid_config()
    del cfg["stages"]["analysis"]
    result = validate(cfg)
    assert result.ok is False
    assert any("missing_core_stages" in e for e in result.errors)


def test_validate_next_is_string_not_list() -> None:
    """A 'next' written as a string (not list) is rejected."""
    cfg = _valid_config()
    cfg["stages"]["init"]["next"] = "analysis"  # string, not list
    result = validate(cfg)
    assert result.ok is False
    assert any("must be list" in e for e in result.errors)


def test_validate_no_path_init_to_done() -> None:
    """A cycle that makes 'done' unreachable from 'init' is rejected."""
    cfg = _valid_config()
    # Redirect design_review back to design (cycle), done now unreachable.
    cfg["stages"]["design_review"]["next"] = ["design"]
    result = validate(cfg)
    assert result.ok is False
    assert any("no_path_init_to_done" in e for e in result.errors)


def test_validate_missing_init_stage() -> None:
    """Missing 'init' stage -> dedicated 'missing_init_stage' error."""
    cfg = _valid_config()
    del cfg["stages"]["init"]
    result = validate(cfg)
    assert result.ok is False
    assert any("missing_init_stage" in e for e in result.errors)


def test_validate_missing_done_stage() -> None:
    """Missing 'done' stage -> dedicated 'missing_done_stage' error."""
    cfg = _valid_config()
    del cfg["stages"]["done"]
    result = validate(cfg)
    assert result.ok is False
    assert any("missing_done_stage" in e for e in result.errors)


def test_validate_empty_config() -> None:
    """An empty config dict is rejected with multiple errors."""
    result = validate({})
    assert result.ok is False
    assert len(result.errors) > 0


def test_load_valid_yaml(tmp_path: Path) -> None:
    """load() reads a valid YAML file into a dict."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("stages: {}\nintent_keywords: {}\n", encoding="utf-8")
    data = load(cfg_file)
    assert data == {"stages": {}, "intent_keywords": {}}


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """load() on a non-existent file raises ConfigLoadError."""
    with pytest.raises(ConfigLoadError):
        load(tmp_path / "nope.yaml")


def test_load_corrupt_yaml_raises(tmp_path: Path) -> None:
    """load() on invalid YAML raises ConfigLoadError."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text(": : : not valid yaml", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load(cfg_file)


def test_validate_skill_file_not_found(tmp_path: Path) -> None:
    """F4 Rule 6: when rail_pkg_dir is provided, a declared skill whose SKILL.md
    is missing is flagged (catches typos like aet-req-analisys at mount time,
    before the rail mounts and silently falls back to bootstrap at runtime)."""
    cfg = _valid_config()
    # tmp_path has no skills/ subdir → all declared skill files are missing
    result = validate(cfg, rail_pkg_dir=tmp_path)
    assert result.ok is False
    assert any("skill_file_not_found" in e for e in result.errors)


def test_validate_skill_file_found_when_rail_pkg_dir_points_at_real_skills() -> None:
    """F4 Rule 6: when rail_pkg_dir points at the shipped design_rail package
    (which has skills/), the real skill files exist → no skill_file_not_found."""
    shipped_pkg = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "code"
        / "rails"
        / "sdd"
        / "design_rail"
    )
    cfg = _valid_config()
    result = validate(cfg, rail_pkg_dir=shipped_pkg)
    assert not any("skill_file_not_found" in e for e in result.errors)
