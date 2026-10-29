# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for DesignRail (tool-driven, inherits RailStateMachineBase).

Tests the ``sdd_advance`` tool registration + handler logic + ask_user
review approve/reject, against the real ``RailStateMachineBase`` + real
``DesignRail`` (no helper mock — the base class absorbs the helper's logic).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.rail import DesignRail

pytestmark = [pytest.mark.unit]

VALID_CONFIG_DIR = Path(__file__).resolve().parents[4] / "jiuwenswarm" / "agents" / "harness" / "code" / "rails" / "sdd" / "design_rail"


def _make_builder() -> MagicMock:
    """A MagicMock system_prompt_builder tracking added/removed sections."""
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
    """A mock agent with system_prompt_builder + ability_manager (records add_ability)."""
    agent = MagicMock()
    agent.system_prompt_builder = builder
    agent.ability_manager = MagicMock()
    agent.ability_manager.add_ability = MagicMock(return_value=SimpleNamespace(added=True))
    return agent


def _make_rail(project_dir: Path) -> DesignRail:
    """Construct a real DesignRail pointing at the shipped config.yaml + skills/."""
    return DesignRail(
        rail_pkg_dir=VALID_CONFIG_DIR,
        project_dir=project_dir,
        priority=60,
    )


# ── sdd_advance tool registration ──


def test_init_registers_sdd_advance_tool(tmp_path: Path) -> None:
    """init(agent) registers the sdd_advance tool with ability_manager."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)

    agent.ability_manager.add_ability.assert_called_once()
    card = agent.ability_manager.add_ability.call_args.args[0]
    assert card.name == "sdd_advance"
    assert "sdd_advance" in rail._owned_tool_names


def test_uninit_unregisters_sdd_advance_tool(tmp_path: Path) -> None:
    """uninit(agent) removes the registered tool (rail-owns-tools lifecycle)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)

    rail.uninit(agent)

    agent.ability_manager.remove_ability.assert_called_once_with("sdd_advance")
    assert rail._owned_tool_names == set()


# ── sdd_advance handler (tool-driven transition) ──


def test_sdd_advance_transitions_to_valid_next(tmp_path: Path) -> None:
    """sdd_advance(stage='analysis') from init -> transition (artifacts vacuous)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)

    result = rail._handle_advance({"stage": "analysis"})

    assert result["ok"] is True
    assert result["from"] == "init"
    assert result["to"] == "analysis"
    assert rail._stage == "analysis"


def test_sdd_advance_rejects_invalid_next(tmp_path: Path) -> None:
    """sdd_advance to a non-valid-next stage is rejected (prevents stage-skipping)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"  # analysis.next = [analysis_review, design]

    result = rail._handle_advance({"stage": "done"})

    assert result["ok"] is False
    assert "not a valid next" in result["error"]
    assert rail._stage == "analysis"  # unchanged


def test_sdd_advance_blocks_when_artifacts_missing(tmp_path: Path) -> None:
    """sdd_advance from analysis (needs requirements-analysis.md) is blocked
    when the artifact is absent. Error message includes the missing artifact
    name and actionable guidance."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"  # needs .aet/features/<name>/design/requirements-analysis.md

    result = rail._handle_advance({"stage": "analysis_review"})

    assert result["ok"] is False
    assert "Cannot advance from 'analysis'" in result["error"]
    assert "requirements-analysis.md" in result["error"]
    assert "sdd_advance" in result["error"]
    assert rail._stage == "analysis"  # unchanged


def test_sdd_advance_done_reset_for_new_flow(tmp_path: Path) -> None:
    """In done, sdd_advance(stage='analysis') resets to start a new flow."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "done"

    result = rail._handle_advance({"stage": "analysis"})

    assert result["ok"] is True
    assert result["from"] == "done"
    assert result["to"] == "analysis"
    assert rail._stage == "analysis"  # reset for new flow


# ── before_model_call skill injection ──


@pytest.mark.asyncio
async def test_before_model_call_injects_skill_for_analysis(tmp_path: Path) -> None:
    """In analysis stage, before_model_call injects the sdd_skill section."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    ctx = SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")
    await rail.before_model_call(ctx)

    sdd = [s for s in builder.added_sections if s.name == "sdd_skill"]
    assert len(sdd) == 1
    content = sdd[0].content
    assert "DesignRail Methodology" in content.get("cn", "")
    assert "requirements-analysis.md" in content.get("cn", "")


@pytest.mark.asyncio
async def test_before_model_call_skips_injection_for_done(tmp_path: Path) -> None:
    """In done stage, before_model_call does NOT inject sdd_skill."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "done"

    ctx = SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")
    await rail.before_model_call(ctx)

    assert not any(s.name == "sdd_skill" for s in builder.added_sections)


@pytest.mark.asyncio
async def test_before_model_call_injects_bootstrap_at_init(tmp_path: Path) -> None:
    """At init (no skill), before_model_call injects a bootstrap that tells
    the LLM to call sdd_advance to start the flow (bootstrapping the
    tool-driven approach — without this, the LLM wouldn't know the tool)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = None  # fresh — defaults to "init"

    ctx = SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")
    await rail.before_model_call(ctx)

    sdd = [s for s in builder.added_sections if s.name == "sdd_skill"]
    assert len(sdd) == 1
    content = sdd[0].content
    # bootstrap mentions the advance tool + the valid next stage
    assert "sdd_advance" in content.get("cn", "")
    assert "analysis" in content.get("cn", "")  # init's valid next


# ── ask_user review handling (design-specific) ──


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_approve_does_not_auto_advance(tmp_path: Path) -> None:
    """ask_user with an approve answer does NOT auto-advance — the agent
    must call sdd_advance explicitly (R4 step). This prevents double-advance
    (after_tool_call transitions + agent calls sdd_advance → error)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis_review"  # analysis_review.next = [design]

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="通过，进入下一阶段",
    )
    await rail.after_tool_call(ctx)

    # Stage unchanged — agent must call sdd_advance to advance
    assert rail._stage == "analysis_review"


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_reject_triggers_rework(tmp_path: Path) -> None:
    """ask_user with a reject answer -> rework to previous stage."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "design_review"  # rework target = design

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="返工",
    )
    await rail.after_tool_call(ctx)

    assert rail._stage == "design"


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_empty_answer_no_transition(tmp_path: Path) -> None:
    """ask_user with no answer (failed / non-interactive) -> NO transition."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="",
    )
    await rail.after_tool_call(ctx)

    assert rail._stage == "analysis"  # unchanged


# ── Regression: R3 "需要复审" / "跳过复审" options (#5494) ──


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_需要复审_does_not_advance(
    tmp_path: Path,
) -> None:
    """#5494: R3 option "需要复审" must NOT be treated as approve.

    The SKILL.md R3 condition says "需要复审" → return to R1 for re-gate
    (internal to the review skill). The rail must NOT transition (re-gate
    is inside the skill) and must NOT log "approved" (which would mislead
    the agent into calling sdd_advance prematurely, skipping the re-gate
    loop and stalling the flow at design stage).
    """
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis_review"

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="需要复审",
    )
    await rail.after_tool_call(ctx)

    # Stage unchanged — re-gate is internal to the review skill
    assert rail._stage == "analysis_review"


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_跳过复审_waits_for_sdd_advance(
    tmp_path: Path,
) -> None:
    """#5494: R3 option "跳过复审" is approve — the rail waits for the
    agent to call sdd_advance at R4. "跳过复审" contains "复审" but must
    NOT trigger regate (the regate keyword is the full "需要复审" phrase)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis_review"

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="跳过复审",
    )
    await rail.after_tool_call(ctx)

    # Stage unchanged — agent must call sdd_advance to advance (R4)
    assert rail._stage == "analysis_review"


def test_methodology_frame_review_stage_does_not_conflict_with_r3_regate(
    tmp_path: Path,
) -> None:
    """#5494: the review-stage methodology frame must NOT say "after
    completing the review, call sdd_advance" unconditionally — that
    conflicts with the SKILL's R3 "需要复审" → R1 loop. The frame must
    tell the agent to call sdd_advance only at R4, and to loop back to
    R1 (without sdd_advance) when the user selects "需要复审"."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    # The actual next stage name is still present (test_review_stage_*
    # _includes_next_stage_name relies on this)
    assert "stage=design" in payload

    # The conflicting "after completing the review" hint is gone
    assert "After completing the review" not in payload

    # The frame explicitly routes "需要复审" to R1 without sdd_advance
    assert "需要复审" in payload
    assert "R4" in payload
    assert "loop back to R1" in payload


# ── Regression: important findings F1/F2/F3/R1/P1 ──


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_in_production_stage_no_transition(
    tmp_path: Path,
) -> None:
    """F1: ask_user during a PRODUCTION stage (not analysis_review/design_review)
    must NOT transition — only review stages drive approve/reject via ask_user.
    Without this guard, a clarification ask_user during `analysis` with a
    non-reject answer would prematurely jump to analysis_review, skipping the
    requirements-analysis.md artifact gate."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"  # production stage, NOT a review stage

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result="通过，进入下一阶段",  # non-reject answer
    )
    await rail.after_tool_call(ctx)

    assert rail._stage == "analysis"  # unchanged — no premature advance


def test_transition_to_rejects_invalid_stage(tmp_path: Path) -> None:
    """F2: _transition_to refuses to set an invalid stage name (not in stages),
    so typos in _REWORK_TARGETS / config next don't silently stall the machine."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    rail._transition_to("bogus_stage")

    assert rail._stage == "analysis"  # unchanged — invalid stage rejected


def test_sdd_advance_done_reset_with_feature_name_creates_feature_dir(
    tmp_path: Path,
) -> None:
    """F3: done-reset with feature_name creates .aet/features/<name>/design/
    so the new flow resolves to that feature — the feature_name param is
    functional (not a dead parameter)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "done"

    result = rail._handle_advance(
        {"stage": "analysis", "feature_name": "new-feat"}
    )

    assert result["ok"] is True
    assert rail._stage == "analysis"
    feature_design_dir = tmp_path / ".aet" / "features" / "new-feat" / "design"
    assert feature_design_dir.exists()  # feature dir created → param is functional


def test_sdd_advance_done_reset_ignores_unsafe_feature_name(
    tmp_path: Path,
) -> None:
    """F3 path-safety: feature_name with path-traversal components is rejected
    (no escaped dir created); the stage reset still succeeds."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)

    for malicious in ("../escape", "/abs/path", "foo/bar", ".."):
        rail._stage = "done"
        rail._handle_advance(
            {"stage": "analysis", "feature_name": malicious}
        )
        # reset happened, but no escaped dir created
        assert not (tmp_path / ".aet" / "features" / "escape").exists()
        assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_before_model_call_does_not_write_trace_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: the TEMP TRACE debug instrumentation must not ship — before_model_call
    must NOT open/write a trace file even when SDD_TRACE is set."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    trace_file = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SDD_TRACE", str(trace_file))

    ctx = SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")
    await rail.before_model_call(ctx)

    assert not trace_file.exists()  # no trace file written — TEMP code removed


def test_load_skill_methodology_caches_after_first_call(tmp_path: Path) -> None:
    """P1: SKILL.md is read from disk at most once per stage (cached) — the hot
    path before_model_call must not re-read on every call. A cached impl returns
    the identical object on the second call."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    payload1 = rail._load_skill_methodology("analysis")
    assert payload1 is not None

    payload2 = rail._load_skill_methodology("analysis")
    assert payload1 is payload2  # same object → cached, no re-read


# ── skill file index (absolute paths only — no content inlining) ──


def test_methodology_payload_includes_file_index(tmp_path: Path) -> None:
    """The injected payload includes a file-path index listing skill-package
    files with their absolute paths — the agent uses read_file with these
    paths to load workflow/reference content on demand."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    assert "--- Skill File Index ---" in payload
    assert "--- End of File Index ---" in payload
    assert "workflows/sop-elicitation.md" in payload
    assert "workflows/sop-generation.md" in payload
    assert "workflows/sop-load-template.md" in payload
    assert "workflows/sop-review.md" in payload
    assert "references/deliverable-review.md" in payload


def test_file_index_uses_absolute_paths(tmp_path: Path) -> None:
    """Index entries map relative paths to absolute paths in the skill package,
    not project-relative paths (which would cause 'Failed read' errors)."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    idx_section = payload.split("--- Skill File Index ---")[1]
    idx_section = idx_section.split("--- End of File Index ---")[0]
    for line in idx_section.splitlines():
        if "→" not in line:
            continue
        path = line.split("→")[1].strip()
        assert path.startswith("/"), f"Path not absolute: {path}"
        assert ".aet/workflows/" not in path  # no project-relative paths


def test_file_index_lists_mjs_as_paths(tmp_path: Path) -> None:
    """scripts/*.mjs are listed as absolute paths — the agent passes these
    paths to a subagent for execution via `node`, doesn't read them itself."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    assert "scripts/assemble-checklist.mjs" in payload
    assert "scripts/assemble-template.mjs" in payload
    assert "→" in payload  # path-arrow format

    mjs_line = [line for line in payload.splitlines()
                if "assemble-checklist.mjs" in line and "→" in line]
    assert len(mjs_line) == 1
    path_part = mjs_line[0].split("→")[1].strip()
    assert path_part.startswith("/")


def test_file_index_excludes_templates_dir(tmp_path: Path) -> None:
    """scripts/_templates/ is a DO-NOT-READ directory — nothing under it
    appears in the file index."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    idx_section = payload.split("--- Skill File Index ---")[1]
    idx_section = idx_section.split("--- End of File Index ---")[0]
    for line in idx_section.splitlines():
        if "→" not in line:
            continue
        path = line.split("→")[1].strip()
        assert "_templates" not in path
        assert "DO-NOT-READ" not in path


def test_file_index_empty_for_stage_without_skill(tmp_path: Path) -> None:
    """init stage has no skill file — _build_skill_file_index returns empty
    string, so the methodology payload for init has no file-index section."""
    rail = _make_rail(tmp_path)
    assert rail._build_skill_file_index("") == ""
    assert rail._build_skill_file_index("nonexistent-skill") == ""


# ── review stage methodology frame ──


def test_review_stage_methodology_uses_review_object_not_produce(tmp_path: Path) -> None:
    """Review stages (analysis_review, design_review) have no artifacts — the
    methodology frame says 'Review target' not 'Output file path'."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    assert "Review target" in payload
    assert "Output file path" not in payload
    assert "requirements-analysis.md" in payload  # previous stage's artifact


def test_review_stage_methodology_includes_next_stage_name(tmp_path: Path) -> None:
    """The methodology frame for review stages includes the actual next stage
    name (e.g. 'design' for analysis_review) so the agent can call sdd_advance
    without guessing."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    assert "stage=design" in payload  # actual next stage, not placeholder

    payload_dr = rail._load_skill_methodology("design_review")
    assert payload_dr is not None
    assert "stage=done" in payload_dr


def test_production_stage_methodology_includes_next_stage_name(tmp_path: Path) -> None:
    """Production stages (analysis, design) also get the actual next stage
    name in the advance instruction."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    assert "Output file path" in payload
    assert "stage=analysis_review" in payload


def test_review_skill_has_sdd_advance_exit_instruction(tmp_path: Path) -> None:
    """aet-req-review SKILL.md must instruct the agent to call sdd_advance
    after the review pipeline ends (R4) — without this, the agent gets stuck
    in the review stage forever."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    assert "sdd_advance" in payload
    assert "R4" in payload or "[R4]" in payload
    assert "文档审查完成" in payload  # end signal


def test_review_skill_no_external_skill_dependency(tmp_path: Path) -> None:
    """aet-req-review SKILL.md must NOT reference aet-req-user-review (external
    skill) — it should use the built-in ask_user tool instead. This was the
    root cause of the review-stage deadlock bug."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "aet-req-user-review" not in body
    assert "ask_user" in body  # uses built-in tool instead


def test_analysis_skill_a3_uses_correct_document_name(tmp_path: Path) -> None:
    """aet-req-analysis SKILL.md A3 should say '需求分析文档' (requirements
    analysis doc), not '设计文档' (design doc) — the old wording misled users
    into thinking the next stage was 'system design'."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "需求分析文档的生成" in body
    assert "设计文档的生成" not in body  # old misleading wording
    assert "sdd_advance" in body  # instructs to call advance tool


# ── Regression: skip semantics + reachable "skip the review" path (#5495) ──


def test_sdd_advance_allows_skipping_review_stage(tmp_path: Path) -> None:
    """#5495: the user declining the review must be able to reach the next
    production stage directly.

    The review stage has no "skip review" entry point before its own user
    gate (R1/R2 run first), so "skip the review" has to be expressible as a
    forward edge: analysis -> design (and design -> done).
    """
    design_dir = tmp_path / ".aet" / "features" / "feat" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "requirements-analysis.md").write_text("# RAS\n", encoding="utf-8")

    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"

    result = rail._handle_advance({"stage": "design"})

    assert result["ok"] is True
    assert result["from"] == "analysis"
    assert result["to"] == "design"
    assert rail._stage == "design"


def test_sdd_advance_allows_skipping_review_stage_from_design(
    tmp_path: Path,
) -> None:
    """#5495: same for design -> done (the user declines the design review)."""
    design_dir = tmp_path / ".aet" / "features" / "feat" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "requirements-design.md").write_text("# RDS\n", encoding="utf-8")

    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "design"

    result = rail._handle_advance({"stage": "done"})

    assert result["ok"] is True
    assert result["to"] == "done"
    assert rail._stage == "done"


def test_sdd_advance_skip_review_still_enforces_artifact_gate(
    tmp_path: Path,
) -> None:
    """#5495: skipping the review must not bypass the artifact gate — the
    produced deliverable is still required before advancing."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"  # no requirements-analysis.md on disk

    result = rail._handle_advance({"stage": "design"})

    assert result["ok"] is False
    assert "requirements-analysis.md" in result["error"]
    assert rail._stage == "analysis"


def test_production_frame_lists_review_and_skip_targets(tmp_path: Path) -> None:
    """#5495: the analysis frame names both forward targets so the agent can
    choose "skip the review" instead of guessing (or entering the review
    stage in order to skip it)."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    assert "stage=analysis_review" in payload
    assert "stage=design" in payload
    assert "skip the review stage" in payload


def test_production_frame_states_skip_does_not_skip_the_flow(
    tmp_path: Path,
) -> None:
    """#5495: a skipped clarification question must not be read as "skip the
    SDD flow" — the frame states the artifact must still be produced."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    assert "never means skipping the SDD flow" in payload
    assert "you must still produce the artifact" in payload


def test_review_frame_provides_absolute_path_and_existence(
    tmp_path: Path,
) -> None:
    """#5495: the review frame carries the resolved absolute path and whether
    it exists, so the agent does not probe the filesystem with a
    non-portable shell test (`test -f` fails on Windows PowerShell)."""
    design_dir = tmp_path / ".aet" / "features" / "feat" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "requirements-analysis.md").write_text("# RAS\n", encoding="utf-8")

    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    assert "Resolved absolute path:" in payload
    assert str(design_dir / "requirements-analysis.md") in payload
    assert "Exists: yes" in payload
    assert "test -f" in payload  # the warning names the trap explicitly


def test_review_frame_reports_missing_deliverable(tmp_path: Path) -> None:
    """#5495: when the deliverable is missing the frame says so."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    assert "Exists: no" in payload


def test_review_skill_r3_uses_structured_question(tmp_path: Path) -> None:
    """#5495: the R3 user gate must be a choice, not a free-text box — an empty
    free-text confirmation is dropped by the transport and the gate then
    re-asks the same question forever."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert '"options"' in body
    assert "无需修改，进入下一阶段" in body
    assert "需要修改（请补充具体意见）" in body


def test_review_skill_defines_empty_skip_cancel_semantics(tmp_path: Path) -> None:
    """#5495: an empty confirmation, a skip and a cancel all mean "no
    modifications" and must go straight to R4 — never re-ask."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "用户已跳过此题，未选择任何选项。" in body
    assert "用户已取消本次问答，未作答。" in body
    assert "NEVER call `ask_user` again" in body


def test_analysis_skill_a3_defines_reachable_skip_path(tmp_path: Path) -> None:
    """#5495: A3 must offer a reachable "skip the review" path (stage=design)
    instead of the unreachable "enter the review stage then skip"."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "stage=design" in body
    assert "跳过审查" in body
    assert "跳过复审" not in body  # the unreachable instruction is gone


def test_analysis_skill_a1_defines_skip_handling(tmp_path: Path) -> None:
    """#5495: A1 must define what a skipped clarification question means."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "Skip Handling" in body
    assert "public assumption" in body
    assert "NEVER re-ask" in body


def test_elicitation_sop_defines_skip_handling(tmp_path: Path) -> None:
    """#5495: the elicitation SOP must treat a skipped question as answered
    "no preference" instead of blocking document generation."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    sop_path = Path(
        rail._rail_pkg_dir
    ) / "skills" / "aet-req-analysis" / "workflows" / "sop-elicitation.md"
    sop = sop_path.read_text(encoding="utf-8")

    assert "Skip handling" in sop
    assert "public assumption" in sop
    assert "Never re-ask a skipped question" in sop


def test_design_skill_a2_confirmation_and_a4_skip_are_structured(
    tmp_path: Path,
) -> None:
    """#5495: the design confirmation (A2.2) and the review authorization
    (A4) are free-text gates too — both must be choices so an empty answer
    cannot be lost."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("design")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert '"options"' in body
    assert "同意此设计，继续生成设计文档" in body
    assert "stage=done" in body
    assert "跳过复审" not in body  # the unreachable instruction is gone


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_revision_requested_does_not_advance(
    tmp_path: Path,
) -> None:
    """#5495: the R3 option "需要修改（请补充具体意见）" means the user asks for
    changes — the rail must not treat it as approve (which would log
    "approved" while the review is still unfinished)."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis_review"

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result=(
            "是否需要修改？如有其他意见请说明，无需修改请直接确认。: "
            "需要修改（请补充具体意见）"
        ),
    )
    await rail.after_tool_call(ctx)

    # Stage unchanged — the skill owns the in-place revision
    assert rail._stage == "analysis_review"


@pytest.mark.asyncio
async def test_after_tool_call_ask_user_approve_option_is_not_revision(
    tmp_path: Path,
) -> None:
    """#5495: the approving option must not be misread as a revision request —
    the R3 question text itself contains "是否需要修改"."""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis_review"

    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="ask_user"),
        extra={},
        tool_result=(
            "是否需要修改？如有其他意见请说明，无需修改请直接确认。: "
            "无需修改，进入下一阶段"
        ),
    )
    await rail.after_tool_call(ctx)

    assert rail._stage == "analysis_review"
    assert rail._is_gate_revision(
        "是否需要修改？如有其他意见请说明，无需修改请直接确认。: 无需修改，进入下一阶段"
    ) is False


# ── Contract: the SKILL's structured questions pass the ask_user validation ──


def _skill_question_block(stage: str) -> dict:
    """Extract the JSON question block a SKILL.md tells the agent to send."""
    rail = _make_rail(Path("/tmp"))  # only used to read the shipped SKILL.md
    payload = rail._load_skill_methodology(stage)
    assert payload is not None
    blocks = []
    in_block = False
    buffer: list[str] = []
    for line in payload.splitlines():
        if line.strip() == "```json":
            in_block = True
            buffer = []
            continue
        if in_block and line.strip() == "```":
            in_block = False
            blocks.append("\n".join(buffer))
            continue
        if in_block:
            buffer.append(line)
    assert blocks, f"no json question block found in {stage}"
    return json.loads(blocks[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["analysis", "design", "analysis_review"])
async def test_skill_structured_questions_pass_ask_user_validation(
    stage: str,
) -> None:
    """#5495: the structured questions the skills prescribe must be accepted by
    the ask_user tool contract — a rejected question never reaches the user,
    and the gate silently stalls."""
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

    from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
        StructuredAskUserRail,
    )

    arguments = _skill_question_block(stage)
    tool_call = ToolCall(
        id="tc_ask",
        type="function",
        name="ask_user",
        arguments=json.dumps(arguments),
    )

    decision = await StructuredAskUserRail().resolve_interrupt(
        MagicMock(), tool_call, None
    )

    assert not isinstance(decision, RejectResult), getattr(
        decision, "tool_result", ""
    )


@pytest.mark.asyncio
async def test_r3_question_carries_the_other_free_text_slot() -> None:
    """#5495: the R3 gate needs the free-text slot so the user can describe
    the modification; the label must stay the literal 'Other' the frontend
    recognises."""
    arguments = _skill_question_block("analysis_review")
    labels = [
        option["label"]
        for question in arguments["questions"]
        for option in question.get("options", [])
    ]

    assert "无需修改，进入下一阶段" in labels
    assert "Other" in labels


def test_review_skill_r1_uses_the_system_prompt_subagent_call(tmp_path: Path) -> None:
    """#5495: the R1 gate must delegate with the subagent call the system
    prompt prescribes.

    In code mode `explore_agent` is a runtime subagent (subagent_spawn /
    subagent_wait); the synchronous `task_tool` only accepts the synchronous
    types, so instructing the agent to call `task_tool` with `explore_agent`
    is rejected before the gate starts.
    """
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis_review")
    assert payload is not None

    body_start = payload.find("--- Methodology Body Start ---")
    body_end = payload.find("--- Methodology Body End ---")
    body = payload[body_start:body_end]

    assert "subagent_spawn" in body
    assert "subagent_wait" in body
    assert "Use `task_tool` with an available subagent type" not in body


# ── Regression: chunked write protection prevents max_tokens truncation ──


def test_analysis_generation_sop_requires_chunked_write(tmp_path: Path) -> None:
    """Large documents must be split across multiple write_file/edit_file
    calls so a single JSON string cannot be truncated by max_output_tokens
    (truncated JSON → 'Unterminated string' → write_file never executes)."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    sop_path = (
        Path(rail._rail_pkg_dir)
        / "skills"
        / "aet-req-analysis"
        / "workflows"
        / "sop-generation.md"
    )
    sop = sop_path.read_text(encoding="utf-8")

    assert "Chunked Write Protection" in sop
    assert "3000 characters" in sop
    assert "edit_file" in sop


def test_design_generation_sop_requires_chunked_write(tmp_path: Path) -> None:
    """Same protection for the design skill — design docs with mermaid
    diagrams and module-detail tables are especially truncation-prone."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("design")
    assert payload is not None

    sop_path = (
        Path(rail._rail_pkg_dir)
        / "skills"
        / "aet-req-design"
        / "workflows"
        / "sop-generation.md"
    )
    sop = sop_path.read_text(encoding="utf-8")

    assert "Chunked Write Protection" in sop
    assert "3000 characters" in sop
    assert "edit_file" in sop
    assert "mermaid" in sop.lower()


# ── Regression: chunked write hard rule in SKILL <constraint> (#truncation) ──


def test_analysis_skill_constraint_has_chunked_write_hard_rule(tmp_path: Path) -> None:
    """The chunked-write rule must live in the SKILL <constraint> section so
    the model sees it on every invocation — not just in the SOP sub-file the
    model may skim. A single oversized write_file is truncated by the model's
    output token budget ('Unterminated string') and the file is never written.
    """
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("analysis")
    assert payload is not None

    constraint_start = payload.find("<constraint>")
    constraint_end = payload.find("</constraint>")
    assert constraint_start != -1 and constraint_end != -1
    constraint = payload[constraint_start:constraint_end]

    assert "HARD RULE" in constraint
    assert "2000 characters" in constraint
    assert "Chunked file writes" in constraint
    assert "Unterminated string" in constraint


def test_design_skill_constraint_has_chunked_write_hard_rule(tmp_path: Path) -> None:
    """Same hard rule for the design skill, with explicit mermaid warning."""
    rail = _make_rail(tmp_path)
    payload = rail._load_skill_methodology("design")
    assert payload is not None

    constraint_start = payload.find("<constraint>")
    constraint_end = payload.find("</constraint>")
    assert constraint_start != -1 and constraint_end != -1
    constraint = payload[constraint_start:constraint_end]

    assert "HARD RULE" in constraint
    assert "2000 characters" in constraint
    assert "mermaid" in constraint.lower()
    assert "Unterminated string" in constraint
