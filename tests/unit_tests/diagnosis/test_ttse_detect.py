# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests of the TTSE experience detection used by the attribution engine."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.agent_evolving.attribution import TrajectoryNode

from jiuwenswarm.diagnosis import ttse_detect
from jiuwenswarm.diagnosis.attribution_engine import AttributionCoreEngine
from jiuwenswarm.diagnosis.engine import EngineResult
from jiuwenswarm.diagnosis.models import (
    ALLOWED_ROOT_CAUSE_CATEGORIES,
    TTSE_ROOT_CAUSE_CATEGORIES,
    ModeProfile,
    RootCause,
    RootCauseCategory,
)
from jiuwenswarm.diagnosis.ttse_detect import detect_experience_cause
from tests.unit_tests.diagnosis.diagnosis_fixtures import TRACE_A, span_id


class _FakeModel:
    """Model client that answers the feedback check and the Judge separately."""

    def __init__(self, feedback: dict[str, Any] | None = None, judge: str | None = None) -> None:
        self._feedback = feedback or {"is_feedback": False}
        self._judge = judge
        self.judge_calls = 0
        self.feedback_calls = 0

    async def invoke(self, *, model: str, messages: list[dict[str, Any]], **_: Any) -> SimpleNamespace:
        prompt = str(messages[0]["content"])
        if "is_feedback" in prompt:
            self.feedback_calls += 1
            return SimpleNamespace(content=json.dumps(self._feedback))
        self.judge_calls += 1
        if self._judge is None:
            raise RuntimeError("judge unavailable")
        return SimpleNamespace(content=self._judge)


def _judge_reply(outcome: str) -> str:
    return json.dumps(
        {
            "outcome": outcome,
            "reason": "reply covers the goal",
            "goals": [{"goal": "answer the question", "verdict": "SATISFIED", "reason": "answered"}],
        },
    )


def _node(index: int, role: str, content: str, **extra: Any) -> TrajectoryNode:
    return TrajectoryNode(
        node_id=f"main:{index}",
        subagent_key="main",
        display_name="Main",
        kind="main_agent",
        role=role,
        content=content,
        trace_id=TRACE_A,
        span_id=span_id(index),
        **extra,
    )


def _tool_round(start: int, result: str, call_id: str = "c1") -> list[TrajectoryNode]:
    call = {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": "{}"}}
    return [
        _node(start, "assistant", "", tool_calls=[call]),
        _node(start + 1, "tool", result, name="bash", tool_call_id=call_id),
    ]


def _run_detect(nodes: list[TrajectoryNode], model: _FakeModel, locale: str = "zh") -> RootCause | None:
    return asyncio.run(detect_experience_cause(nodes, model=model, model_name="fake", locale=locale))


def _long_run(tool_rounds: int) -> list[TrajectoryNode]:
    nodes = [_node(0, "user", "please summarize the repository")]
    for round_index in range(tool_rounds):
        nodes.extend(_tool_round(1 + round_index * 2, "all good", call_id=f"c{round_index}"))
    nodes.append(_node(1 + tool_rounds * 2, "assistant", "here is the summary"))
    return nodes


def test_tool_failure_reports_partial_anchored_on_failing_rows() -> None:
    nodes = [_node(0, "user", "run the build"), *_tool_round(1, "Traceback: build failed"), _node(3, "assistant", "x")]
    model = _FakeModel()

    cause = _run_detect(nodes, model)

    assert cause is not None
    assert cause.category is RootCauseCategory.TTSE_PARTIAL
    assert [ref.node_id for ref in cause.refs] == ["main:2"]
    assert cause.refs[0].span_id == span_id(2)
    assert cause.subject_id == "main"
    assert "bash" in cause.summary and "Traceback" in cause.summary
    assert "bash" in cause.evidence and "Traceback" in cause.evidence
    assert model.feedback_calls == 0
    assert model.judge_calls == 0


def test_tool_error_reason_is_one_short_line_around_the_matched_keyword() -> None:
    output = "\n".join(["line"] * 200 + ["deploy step 4 failed: port 8080 is busy"] + ["tail"] * 200)
    nodes = [_node(0, "user", "deploy it"), *_tool_round(1, output), _node(3, "assistant", "x")]

    cause = _run_detect(nodes, _FakeModel(), locale="en")

    assert cause is not None
    assert "port 8080 is busy" in cause.summary
    assert "\n" not in cause.summary
    assert len(cause.summary) < 200
    basis, detail = cause.evidence.split("\n")
    assert basis.startswith("Basis:")
    assert "\"failed\"" in detail and "port 8080 is busy" in detail
    assert len(detail) < 300


def test_summary_quotes_raw_tool_output_so_markdown_leaves_it_alone() -> None:
    nodes = [
        _node(0, "user", "run it"),
        *_tool_round(1, "Error: OPENAI_IMAGE_MODEL `x` [single] a | b"),
        _node(3, "assistant", "x"),
    ]

    cause = _run_detect(nodes, _FakeModel(), locale="en")

    assert cause is not None
    quoted = cause.summary.split(": ", 1)[1]
    assert quoted.startswith("`") and quoted.endswith("`")
    assert quoted.count("`") == 2
    assert "OPENAI_IMAGE_MODEL 'x' [single] a | b" in quoted


def test_several_tool_errors_are_counted_in_the_headline() -> None:
    nodes = [
        _node(0, "user", "run it"),
        *_tool_round(1, "Error: first", call_id="a"),
        *_tool_round(3, "Error: second", call_id="b"),
        _node(5, "assistant", "x"),
    ]

    cause = _run_detect(nodes, _FakeModel(), locale="zh")

    assert cause is not None
    assert [ref.node_id for ref in cause.refs] == ["main:2", "main:4"]
    assert "另有 1 处" in cause.summary


def test_user_feedback_reports_partial_anchored_on_matching_user_row() -> None:
    nodes = [
        _node(0, "user", "write the report"),
        _node(1, "assistant", "done"),
        _node(2, "user", "that is wrong, redo it"),
        _node(3, "assistant", "sorry"),
    ]
    model = _FakeModel(feedback={"is_feedback": True, "excerpt": "that is wrong"})

    cause = _run_detect(nodes, model, locale="en")

    assert cause is not None
    assert cause.category is RootCauseCategory.TTSE_PARTIAL
    assert [ref.node_id for ref in cause.refs] == ["main:2"]
    assert "that is wrong" in cause.summary
    assert "that is wrong" in cause.evidence
    assert model.judge_calls == 0


def test_few_tool_calls_skip_without_judge() -> None:
    model = _FakeModel(judge=_judge_reply("success"))

    assert _run_detect(_long_run(2), model) is None
    assert model.judge_calls == 0


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        ("success", RootCauseCategory.TTSE_SUCCESS),
        ("partial", RootCauseCategory.TTSE_PARTIAL),
        ("fail", RootCauseCategory.TTSE_FAIL),
    ],
)
def test_judge_outcome_maps_to_category_and_anchors_on_final_reply(
    outcome: str,
    category: RootCauseCategory,
) -> None:
    nodes = _long_run(5)
    cause = _run_detect(nodes, _FakeModel(judge=_judge_reply(outcome)))

    assert cause is not None
    assert cause.category is category
    assert [ref.node_id for ref in cause.refs] == [nodes[-1].node_id]
    assert "reply covers the goal" in cause.summary
    assert "answer the question" in cause.evidence
    assert "工具调用 5 次" in cause.evidence
    assert cause.confidence is None


def test_judge_reply_in_code_fence_is_accepted() -> None:
    reply = "```json\n" + _judge_reply("success") + "\n```"

    cause = _run_detect(_long_run(5), _FakeModel(judge=reply))

    assert cause is not None
    assert cause.category is RootCauseCategory.TTSE_SUCCESS


@pytest.mark.parametrize("judge", [None, "not json", json.dumps({"outcome": "great"})])
def test_unusable_judge_yields_no_cause(judge: str | None) -> None:
    assert _run_detect(_long_run(5), _FakeModel(judge=judge)) is None


def test_summary_follows_locale() -> None:
    zh = _run_detect(_long_run(5), _FakeModel(judge=_judge_reply("fail")), locale="zh")
    en = _run_detect(_long_run(5), _FakeModel(judge=_judge_reply("fail")), locale="en")

    assert zh is not None and en is not None
    assert zh.summary.startswith("最终回复评判为「未完成」")
    assert en.summary.startswith("Final reply judged as \"not completed\"")


def test_judge_without_a_reason_still_names_the_outcome() -> None:
    reply = json.dumps({"outcome": "partial", "goals": []})

    cause = _run_detect(_long_run(5), _FakeModel(judge=reply), locale="en")

    assert cause is not None
    assert cause.summary == "Final reply judged as \"partly completed\""


def test_rows_without_span_identity_cannot_anchor_a_verdict() -> None:
    nodes = [replace(node, trace_id=None, span_id=None) for node in _long_run(5)]

    assert _run_detect(nodes, _FakeModel(judge=_judge_reply("success"))) is None


def test_system_rows_are_ignored_by_detection() -> None:
    nodes = [_node(0, "system", "Traceback: build failed"), _node(1, "user", "hi"), _node(2, "assistant", "hello")]

    assert _run_detect(nodes, _FakeModel()) is None


def test_ttse_categories_are_agent_work_only() -> None:
    assert TTSE_ROOT_CAUSE_CATEGORIES <= ALLOWED_ROOT_CAUSE_CATEGORIES[ModeProfile.AGENT_WORK]
    assert not TTSE_ROOT_CAUSE_CATEGORIES & ALLOWED_ROOT_CAUSE_CATEGORIES[ModeProfile.AGENT_CODE]
    assert not TTSE_ROOT_CAUSE_CATEGORIES & ALLOWED_ROOT_CAUSE_CATEGORIES[ModeProfile.TEAM]


def _engine_inputs(mode: ModeProfile = ModeProfile.AGENT_WORK) -> SimpleNamespace:
    subjects = [SimpleNamespace(subject_id="main", kind="main_agent")]
    return SimpleNamespace(mode_profile=mode, snapshot=SimpleNamespace(subjects=subjects), locale="zh")


def _existing_cause(category: RootCauseCategory) -> RootCause:
    return RootCause(id="rc-0", category=category, subject_id="main", refs=[], summary="s")


def _append(
    monkeypatch: pytest.MonkeyPatch,
    inputs: SimpleNamespace,
    result: EngineResult,
    detected: RootCause | Exception | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_detect(nodes: Any, **kwargs: Any) -> RootCause | None:
        calls.append({"nodes": nodes, **kwargs})
        if isinstance(detected, Exception):
            raise detected
        return detected

    monkeypatch.setattr(ttse_detect, "detect_experience_cause", fake_detect)
    nodes = {"main": [_node(0, "user", "hi")], "other": [_node(1, "user", "x")]}
    asyncio.run(AttributionCoreEngine._append_experience_cause(inputs, object(), "m", nodes, result))
    return calls


def test_engine_appends_experience_cause_for_main_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = _existing_cause(RootCauseCategory.TTSE_SUCCESS)
    result = EngineResult(root_causes=[_existing_cause(RootCauseCategory.OTHER)])

    calls = _append(monkeypatch, _engine_inputs(), result, cause)

    assert [item.category for item in result.root_causes] == [RootCauseCategory.OTHER, RootCauseCategory.TTSE_SUCCESS]
    assert [node.node_id for node in calls[0]["nodes"]] == ["main:0"]
    assert calls[0]["locale"] == "zh"


def test_engine_skips_detection_when_a_ttse_cause_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    result = EngineResult(root_causes=[_existing_cause(RootCauseCategory.TTSE_FAIL)])

    calls = _append(monkeypatch, _engine_inputs(), result, _existing_cause(RootCauseCategory.TTSE_SUCCESS))

    assert calls == []
    assert len(result.root_causes) == 1


@pytest.mark.parametrize("mode", [ModeProfile.AGENT_CODE, ModeProfile.TEAM])
def test_engine_skips_detection_outside_agent_work(monkeypatch: pytest.MonkeyPatch, mode: ModeProfile) -> None:
    result = EngineResult()

    calls = _append(monkeypatch, _engine_inputs(mode), result, _existing_cause(RootCauseCategory.TTSE_SUCCESS))

    assert calls == []
    assert result.root_causes == []


def test_engine_keeps_result_when_detection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    result = EngineResult(root_causes=[_existing_cause(RootCauseCategory.OTHER)])

    _append(monkeypatch, _engine_inputs(), result, RuntimeError("boom"))

    assert [item.category for item in result.root_causes] == [RootCauseCategory.OTHER]
