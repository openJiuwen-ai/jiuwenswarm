# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Integration test: DesignRail with real config + sdd_advance tool flow.

Exercises the end-to-end tool-driven path:
  DesignRail constructed from real config.yaml + skills/ -> init(agent)
  registers sdd_advance -> _handle_advance transitions state ->
  before_model_call injects the methodology for the new stage.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.rail import DesignRail

pytestmark = [pytest.mark.integration]

_RAIL_PKG_DIR = (
    Path(__file__).resolve().parents[4]
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "code"
    / "rails"
    / "sdd"
    / "design_rail"
)


def _make_builder() -> MagicMock:
    builder = MagicMock()
    builder.added_sections = []

    def _add(section):
        builder.added_sections = [s for s in builder.added_sections if s.name != section.name]
        builder.added_sections.append(section)
        return builder

    def _remove(name):
        builder.added_sections = [s for s in builder.added_sections if s.name != name]
        return builder

    builder.add_section = MagicMock(side_effect=_add)
    builder.remove_section = MagicMock(side_effect=_remove)
    return builder


def _make_agent(builder: MagicMock) -> MagicMock:
    agent = MagicMock()
    agent.system_prompt_builder = builder
    agent.ability_manager = MagicMock()
    agent.ability_manager.add_ability = MagicMock(return_value=SimpleNamespace(added=True))
    return agent


@pytest.mark.asyncio
async def test_full_chain_init_to_analysis_via_sdd_advance(tmp_path: Path) -> None:
    """sdd_advance(stage='analysis') from init -> state=analysis + skill injected.

    Replaces the old keyword-matching path: the LLM calls sdd_advance (the
    base class's tool) instead of saying "需求分析". The tool handler
    validates + transitions; the next before_model_call injects the skill.
    """
    rail = DesignRail(
        rail_pkg_dir=_RAIL_PKG_DIR,
        project_dir=tmp_path,
        priority=60,
    )
    builder = _make_builder()
    agent = _make_agent(builder)
    rail.init(agent)

    # LLM calls sdd_advance(stage="analysis")
    result = rail._handle_advance({"stage": "analysis"})
    assert result["ok"] is True
    assert rail._stage == "analysis"

    # Next before_model_call injects the analysis methodology
    ctx = SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")
    await rail.before_model_call(ctx)

    sdd = [s for s in builder.added_sections if s.name == "sdd_skill"]
    assert len(sdd) == 1
    content = sdd[0].content
    assert "DesignRail Methodology" in content.get("cn", "")
    assert "requirements-analysis.md" in content.get("cn", "")
    # front-matter stripped so the skill name isn't exposed as a toolkit-loadable skill
    assert "name: aet-req-analysis" not in content.get("cn", "")
    # the frame tells the LLM to call sdd_advance (not the skill toolkit)
    assert "sdd_advance" in content.get("cn", "")
    assert "Do NOT call skill toolkit" in content.get("cn", "")


def test_full_chain_artifacts_gate_blocks_without_ras(tmp_path: Path) -> None:
    """sdd_advance from analysis is blocked until requirements-analysis.md exists.

    The artifacts gate (in the base class) requires the current stage's
    declared artifact before allowing the forward transition.
    """
    rail = DesignRail(rail_pkg_dir=_RAIL_PKG_DIR, project_dir=tmp_path, priority=60)
    rail._stage = "analysis"  # needs .aet/features/<name>/design/requirements-analysis.md

    # No feature dir created -> artifacts gate blocks with detailed error
    result = rail._handle_advance({"stage": "analysis_review"})
    assert result["ok"] is False
    assert "Cannot advance from 'analysis'" in result["error"]
    assert "requirements-analysis.md" in result["error"]
    assert rail._stage == "analysis"  # unchanged


def test_full_chain_artifacts_gate_passes_after_ras_created(tmp_path: Path) -> Path:
    """After the LLM creates requirements-analysis.md, the artifacts gate passes."""
    # Set up a feature dir with the RAS artifact
    feature_dir = tmp_path / ".aet" / "features" / "test-feat" / "design"
    feature_dir.mkdir(parents=True)
    (feature_dir / "requirements-analysis.md").write_text("# RAS\n", encoding="utf-8")

    rail = DesignRail(rail_pkg_dir=_RAIL_PKG_DIR, project_dir=tmp_path, priority=60)
    rail._stage = "analysis"

    result = rail._handle_advance({"stage": "analysis_review"})
    assert result["ok"] is True
    assert rail._stage == "analysis_review"
