# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests of the simple LLM diagnosis engine, driven by a scripted fake model."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.diagnosis import spans as _spans
from jiuwenswarm.diagnosis.engine import EngineError, EngineEvent, EngineInputs, LlmDiagnosisEngine
from jiuwenswarm.diagnosis.llm_engine.nodes import render_transcript, select_nodes
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import ModeProfile, ModelInfo, RootCauseCategory, TtseSignalOutcome
from jiuwenswarm.diagnosis.service import filter_engine_result
from jiuwenswarm.diagnosis.snapshot import (
    ScopeIndex,
    SubjectRange,
    SubjectTrajectory,
    TrajectoryRecord,
    TrajectorySnapshot,
)
from tests.unit_tests.diagnosis.diagnosis_fixtures import make_span

# Importing the harness rails is slow the first time; do it before any test runs.
_spans._tool_result_has_error("warm-up")

_SKILL_NAME = "pdf-reader"


class FakeModel:
    """Model that answers from a script and records every prompt it receives."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def invoke(self, messages: list[Any], **options: Any) -> Any:
        self.prompts.append(messages[-1].content)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply if isinstance(reply, str) else json.dumps(reply))


def _record(index: int, start: int, **span: Any) -> TrajectoryRecord:
    item = make_span(index, start, start + 10, **span)
    return TrajectoryRecord(
        trace_id=item["traceId"],
        span_id=item["spanId"],
        parent_span_id=None,
        start_nano=start,
        end_nano=start + 10,
        otlp={"resourceSpans": [{"scopeSpans": [{"spans": [item]}]}]},
    )


def _snapshot() -> TrajectorySnapshot:
    records = [
        _record(1, 100, operation="chat", output_text="I will open the file", tool_calls=True),
        _record(
            2,
            200,
            operation="execute_tool",
            tool_name="read_file",
            tool_arguments='{"path": "a.pdf"}',
            tool_result=f"Error: permission denied while using {_SKILL_NAME}",
            status_code=2,
        ),
        _record(3, 300, operation="chat", output_text="Sorry, I could not read the file"),
    ]
    return TrajectorySnapshot(
        captured_at_ns=1000,
        subjects=[SubjectTrajectory(subject_id="main", display_name="Main", kind="main_agent", records=records)],
    )


def _inputs(
    profile: ModeProfile = ModeProfile.AGENT_WORK,
    *,
    ranges: list[SubjectRange] | None = None,
    locale: str = "en",
) -> EngineInputs:
    return EngineInputs(
        session_id="session-1",
        mode_profile=profile,
        locale=locale,  # type: ignore[arg-type]
        user_note=None,
        snapshot=_snapshot(),
        ranges=[SubjectRange("main", 0, 1000)] if ranges is None else ranges,
        model=ResolvedDiagnosisModel(info=ModelInfo(type="model", id="m"), resolved=None),
    )


def _engine(model: FakeModel, **options: Any) -> LlmDiagnosisEngine:
    return LlmDiagnosisEngine(model_factory=lambda _resolved: model, **options)


class _Events:
    def __init__(self) -> None:
        self.items: list[EngineEvent] = []

    async def __call__(self, event: EngineEvent) -> None:
        self.items.append(event)


_ATTRIBUTION = {
    "task_outcome": "fail",
    "headline": "The file could not be read",
    "artifact_eval": {
        "verdict": "fail",
        "score": 0.1,
        "summary": "The user got no result",
        "issues": [{"description": "No file content was returned", "step_ids": ["main:2"]}],
    },
    "root_causes": [
        {
            "category": "skill_defect",
            "step_ids": ["main:0", "main:1"],
            "summary": "The Skill did not mention the permission check",
            "evidence": "read_file failed with permission denied",
            "confidence": 0.8,
        },
    ],
    "focus": [{"step_ids": ["main:0", "main:1"], "reason": "the failing read"}],
}

_SUGGESTIONS = {
    "artifact_fix": {"instruction": "Retry the read with the right permissions", "root_cause_ids": ["rc-1"]},
    "skills": [
        {
            "skill_name": _SKILL_NAME,
            "change_summary": "Document the permission check",
            "rationale": "The read failed",
            "message": "Please update the pdf-reader Skill",
            "root_cause_ids": ["rc-1"],
            "confidence": 0.7,
        },
    ],
    "ttse": [
        {
            "outcome": "fail",
            "score": 0.0,
            "reason": "The agent lacked the permission fact",
            "subject_id": "main",
            "start_step_id": "main:0",
            "end_step_id": "main:1",
            "root_cause_ids": ["rc-1"],
            "confidence": 0.6,
        },
    ],
}


@pytest.mark.asyncio
async def test_run_produces_a_grounded_result_in_three_stages() -> None:
    model = FakeModel([_ATTRIBUTION, _SUGGESTIONS])
    events = _Events()

    result = await _engine(model).run(_inputs(), events)

    assert [(item.stage_id, item.kind) for item in events.items if item.kind != "output_delta"] == [
        ("evidence", "stage_started"),
        ("evidence", "stage_finished"),
        ("attribution", "stage_started"),
        ("attribution", "stage_finished"),
        ("suggestions", "stage_started"),
        ("suggestions", "stage_finished"),
    ]
    assert result.headline == "The file could not be read"
    assert result.artifact_eval is not None and result.artifact_eval.verdict == "fail"
    assert result.artifact_eval.issues[0].refs[0].node_id == "main:2"
    cause = result.root_causes[0]
    assert cause.id == "rc-1"
    assert cause.category is RootCauseCategory.SKILL_DEFECT
    assert [ref.node_id for ref in cause.refs] == ["main:0", "main:1"]
    assert result.focus_ranges[0].start_unix_nano == "100"
    assert result.focus_ranges[0].end_unix_nano == "210"
    assert result.suggestions.artifact_fix is not None
    assert result.suggestions.skills[0].skill_name == _SKILL_NAME
    signal = result.suggestions.ttse[0]
    assert signal.outcome is TtseSignalOutcome.FAIL
    assert (signal.start_unix_nano, signal.end_unix_nano) == ("100", "210")


def _stage_output(events: _Events, stage_id: str) -> str:
    return "".join(item.text_delta or "" for item in events.items if item.stage_id == stage_id and item.text_delta)


@pytest.mark.asyncio
async def test_each_stage_streams_readable_progress() -> None:
    events = _Events()

    await _engine(FakeModel([_ATTRIBUTION, _SUGGESTIONS])).run(_inputs(), events)

    evidence = _stage_output(events, "evidence")
    assert "3 steps in scope, 1 rule alerts" in evidence
    assert "[main:1]" in evidence
    attribution = _stage_output(events, "attribution")
    assert attribution.startswith("Asking the model to analyse 3 steps")
    assert "Task outcome: not completed" in attribution
    assert "Artifact evaluation: fail (score 0.10)" in attribution
    assert "Root cause 1 [Skill defect] The Skill did not mention the permission check" in attribution
    assert "Steps: main:0, main:1; confidence 80%" in attribution
    suggestions = _stage_output(events, "suggestions")
    assert "Artifact fix request: Retry the read" in suggestions
    assert "Skill suggestion 'pdf-reader'" in suggestions
    assert "TTSE trigger signal (fail, subject main)" in suggestions


@pytest.mark.asyncio
async def test_correction_round_is_reported_in_the_stage_output() -> None:
    bad = json.loads(json.dumps(_ATTRIBUTION))
    bad["root_causes"][0]["step_ids"] = ["main:99"]
    events = _Events()

    await _engine(FakeModel([bad, _ATTRIBUTION, _SUGGESTIONS])).run(_inputs(), events)

    attribution = _stage_output(events, "attribution")
    assert "asking it again (attempt 2/2)" in attribution
    assert "main:99" in attribution


@pytest.mark.asyncio
async def test_clean_run_says_why_suggestions_are_skipped() -> None:
    reply = {
        "task_outcome": "success",
        "artifact_eval": {"verdict": "pass", "score": 1, "summary": "done", "issues": []},
        "root_causes": [],
        "focus": [],
    }
    events = _Events()

    await _engine(FakeModel([reply])).run(_inputs(locale="zh"), events)

    assert "跳过建议生成" in _stage_output(events, "suggestions")
    assert "未发现需要处理的根因" in _stage_output(events, "attribution")


@pytest.mark.asyncio
async def test_prompts_carry_node_ids_and_rule_alerts() -> None:
    model = FakeModel([_ATTRIBUTION, _SUGGESTIONS])

    await _engine(model).run(_inputs(), _Events())

    attribution_prompt = model.prompts[0]
    assert "[main:1]" in attribution_prompt
    assert "TOOL read_file" in attribution_prompt
    assert "tool_error" in attribution_prompt
    assert "skill_defect" in attribution_prompt
    assert "rc-1" in model.prompts[1]


@pytest.mark.asyncio
async def test_result_passes_the_host_filter_unchanged() -> None:
    inputs = _inputs()
    result = await _engine(FakeModel([_ATTRIBUTION, _SUGGESTIONS])).run(inputs, _Events())

    filtered = filter_engine_result(
        result,
        index=ScopeIndex.build(inputs.snapshot, inputs.ranges),
        profile=ModeProfile.AGENT_WORK,
    )

    assert len(filtered.root_causes) == 1
    assert len(filtered.focus_ranges) == 1
    assert len(filtered.suggestions.ttse) == 1
    assert len(filtered.suggestions.skills) == 1


@pytest.mark.asyncio
async def test_unknown_step_id_is_sent_back_once_and_corrected() -> None:
    bad = json.loads(json.dumps(_ATTRIBUTION))
    bad["root_causes"][0]["step_ids"] = ["main:99"]
    model = FakeModel([bad, _ATTRIBUTION, _SUGGESTIONS])

    result = await _engine(model).run(_inputs(), _Events())

    assert len(model.prompts) == 3
    assert "main:99" in model.prompts[1]
    assert "rejected" in model.prompts[1]
    assert result.root_causes[0].refs[0].node_id == "main:0"


@pytest.mark.asyncio
async def test_uncorrected_reply_keeps_its_usable_part() -> None:
    bad = json.loads(json.dumps(_ATTRIBUTION))
    bad["root_causes"].append({"category": "other", "step_ids": ["main:99"], "summary": "ghost"})
    model = FakeModel([bad, bad, _SUGGESTIONS])

    result = await _engine(model).run(_inputs(), _Events())

    assert [item.summary for item in result.root_causes] == ["The Skill did not mention the permission check"]


@pytest.mark.asyncio
async def test_unparseable_reply_fails_with_output_invalid() -> None:
    model = FakeModel(["not json at all", "still not json"])

    with pytest.raises(EngineError) as raised:
        await _engine(model).run(_inputs(), _Events())

    assert raised.value.code == "MODEL_OUTPUT_INVALID"
    assert len(model.prompts) == 2


@pytest.mark.asyncio
async def test_model_exception_fails_with_call_failed() -> None:
    model = FakeModel([RuntimeError("quota exceeded")])

    with pytest.raises(EngineError) as raised:
        await _engine(model).run(_inputs(), _Events())

    assert raised.value.code == "MODEL_CALL_FAILED"
    assert "quota exceeded" in raised.value.message


@pytest.mark.asyncio
async def test_empty_scope_is_reported_without_calling_the_model() -> None:
    model = FakeModel([])

    with pytest.raises(EngineError) as raised:
        await _engine(model).run(_inputs(ranges=[SubjectRange("main", 5000, 6000)]), _Events())

    assert raised.value.code == "TASK_INCOMPLETE"
    assert model.prompts == []


@pytest.mark.asyncio
async def test_successful_run_skips_the_suggestion_call() -> None:
    reply = {
        "task_outcome": "success",
        "headline": "All good",
        "artifact_eval": {"verdict": "pass", "score": 0.95, "summary": "done", "issues": []},
        "root_causes": [],
        "focus": [],
    }
    model = FakeModel([reply])
    events = _Events()

    result = await _engine(model).run(_inputs(), events)

    assert len(model.prompts) == 1
    assert result.root_causes == []
    assert result.suggestions.artifact_fix is None
    assert [item.status for item in events.items if item.stage_id == "suggestions" and item.status] == ["completed"]


@pytest.mark.asyncio
async def test_code_mode_downgrades_forbidden_categories_and_ignores_other_outlets() -> None:
    model = FakeModel([_ATTRIBUTION, _SUGGESTIONS])

    result = await _engine(model).run(_inputs(ModeProfile.AGENT_CODE), _Events())

    assert result.root_causes[0].category is RootCauseCategory.OTHER
    assert result.suggestions.artifact_fix is not None
    assert result.suggestions.skills == []
    assert result.suggestions.ttse == []


@pytest.mark.asyncio
async def test_team_mode_ignores_ttse_signals() -> None:
    model = FakeModel([_ATTRIBUTION, _SUGGESTIONS])

    result = await _engine(model).run(_inputs(ModeProfile.TEAM), _Events())

    assert len(result.suggestions.skills) == 1
    assert result.suggestions.ttse == []


@pytest.mark.asyncio
async def test_skill_that_never_appears_in_the_run_is_dropped() -> None:
    invented = json.loads(json.dumps(_SUGGESTIONS))
    invented["skills"][0]["skill_name"] = "web-scraper"
    model = FakeModel([_ATTRIBUTION, invented, invented])

    result = await _engine(model).run(_inputs(), _Events())

    assert result.suggestions.skills == []
    assert "web-scraper" in model.prompts[2]


@pytest.mark.asyncio
async def test_ttse_signal_across_subjects_is_rejected() -> None:
    crossing = json.loads(json.dumps(_SUGGESTIONS))
    crossing["ttse"][0]["end_step_id"] = "other:2"
    model = FakeModel([_ATTRIBUTION, crossing, crossing])

    result = await _engine(model).run(_inputs(), _Events())

    assert result.suggestions.ttse == []


@pytest.mark.asyncio
async def test_chinese_locale_asks_for_chinese_text_and_localizes_stage_titles() -> None:
    model = FakeModel([_ATTRIBUTION, _SUGGESTIONS])
    events = _Events()

    await _engine(model).run(_inputs(locale="zh"), events)

    assert "Simplified Chinese" in model.prompts[0]
    assert [item.title for item in events.items if item.kind == "stage_started"] == [
        "收集证据",
        "归因分析",
        "生成建议",
    ]


def test_node_numbering_covers_the_whole_subject_not_only_the_range() -> None:
    nodes = select_nodes(_snapshot(), [SubjectRange("main", 250, 400)])

    assert [node.node_id for node in nodes] == ["main:2"]


def test_transcript_is_trimmed_to_the_budget_and_keeps_both_ends() -> None:
    records = [
        _record(index, index * 100, operation="execute_tool", tool_name="t", tool_result="x" * 400)
        for index in range(1, 200)
    ]
    snapshot = TrajectorySnapshot(
        captured_at_ns=1,
        subjects=[SubjectTrajectory(subject_id="main", display_name="Main", kind="main_agent", records=records)],
    )
    nodes = select_nodes(snapshot, [SubjectRange("main", 0, 10**9)])

    text = render_transcript(nodes, max_chars=6000)

    assert len(text) <= 6200
    assert "[main:0]" in text
    assert "[main:198]" in text
    assert "lines omitted" in text


def test_production_host_runs_diagnoses_with_the_attribution_core_engine() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import AttributionCoreEngine
    from jiuwenswarm.server.diagnosis.host import build_diagnosis_engine

    assert isinstance(build_diagnosis_engine(), AttributionCoreEngine)
