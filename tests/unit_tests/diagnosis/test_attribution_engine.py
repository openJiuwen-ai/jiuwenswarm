# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests of the attribution core diagnosis engine.

The three core entry points are faked for orchestration tests (their own
behaviour is covered in agent-core); the tests here exercise the adapter: node
preparation, stage mapping, event ordering, result translation and the failure
semantics the host relies on.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.agent_evolving.attribution import (
    DiagnosticSignal,
    NodeBackedRuntime,
    RewardOverall,
    SignalSource,
    TaskParseError,
    TaskParseResult,
    TrajectoryNode,
)

from jiuwenswarm.diagnosis.attribution_engine import (
    AttributionCoreEngine,
    _QueueStatusSink,
    _core_snapshot,
    _flatten_span,
    _prepare_nodes,
    _render_llm_line,
    _translate_attribution,
)
from jiuwenswarm.diagnosis.engine import EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import (
    ArtifactEval,
    DiagnosisError,
    ModeProfile,
    ModelInfo,
    RootCauseCategory,
    StepRef,
)
from jiuwenswarm.diagnosis.snapshot import (
    SubjectRange,
    SubjectTrajectory,
    TrajectoryRecord,
    TrajectorySnapshot,
)
from jiuwenswarm.diagnosis.service import DiagnosisService, filter_engine_result
from tests.unit_tests.diagnosis.diagnosis_fixtures import (
    SESSION_ID,
    TRACE_A,
    make_record,
    make_span,
    span_id,
)


def _wrap(span: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}


def _snapshot(records: list[TrajectoryRecord]) -> TrajectorySnapshot:
    return TrajectorySnapshot(
        captured_at_ns=0,
        subjects=[
            SubjectTrajectory(
                subject_id="main",
                display_name="Main",
                kind="main_agent",
                records=records,
            ),
        ],
    )


def _chat_record(index: int, start: int, end: int, text: str = "reply") -> TrajectoryRecord:
    span = make_span(index, start, end, operation="chat", output_text=text)
    return TrajectoryRecord(
        trace_id=span["traceId"],
        span_id=span["spanId"],
        parent_span_id=None,
        start_nano=start,
        end_nano=end,
        otlp=_wrap(span),
    )


def _inputs(snapshot: TrajectorySnapshot, ranges: list[SubjectRange]) -> EngineInputs:
    return EngineInputs(
        session_id=SESSION_ID,
        mode_profile=ModeProfile.AGENT_WORK,
        locale="zh",
        user_note="a note",
        snapshot=snapshot,
        ranges=ranges,
        model=ResolvedDiagnosisModel(info=ModelInfo(type="model", id="m", display_name="fake-model"), resolved=None),
    )


def _parsed() -> TaskParseResult:
    return TaskParseResult(
        task_description="装好依赖并跑测试",
        message_artifacts=[("main:0", "先看 README")],
        file_artifacts=["/ws/out.txt"],
        file_candidates=[("/ws/out.txt", "main:1", "write_file")],
    )


def _signal(signal_id: str, tier: str = "weak") -> DiagnosticSignal:
    return DiagnosticSignal.create(
        text=f"信号 {signal_id}",
        source=SignalSource.KEYWORD_TOOL,
        tier=tier,
        node_ids=["main:0"],
        signal_id=signal_id,
    )


@pytest.mark.asyncio
async def test_prepare_nodes_flattens_numbers_and_slices() -> None:
    snapshot = _snapshot([_chat_record(0, 1000, 2000), _chat_record(1, 2000, 3500, "later")])
    inputs = _inputs(snapshot, [SubjectRange("main", 500, 2500)])

    sliced = await asyncio.to_thread(_prepare_nodes, inputs)

    nodes = sliced["main"]
    assert [node.node_id for node in nodes] == ["main:0"]
    assert nodes[0].trace_id == TRACE_A
    assert nodes[0].span_id == span_id(0)


@pytest.mark.asyncio
async def test_prepare_nodes_ids_are_deterministic_without_mapping() -> None:
    snapshot = _snapshot([_chat_record(0, 1000, 2000), _chat_record(1, 2000, 3500, "later")])
    inputs = _inputs(snapshot, [SubjectRange("main", 0, 10**9)])

    first = await asyncio.to_thread(_prepare_nodes, inputs)
    second = await asyncio.to_thread(_prepare_nodes, inputs)

    assert [node.node_id for node in first["main"]] == ["main:0", "main:1"]
    assert [node.node_id for node in second["main"]] == ["main:0", "main:1"]


def test_core_snapshot_carries_only_consumed_keys() -> None:
    snapshot = _snapshot([_chat_record(0, 1000, 2000)])

    core = _core_snapshot(snapshot, SESSION_ID)

    assert set(core) == {"session_id", "subjects"}
    subject = core["subjects"][0]
    assert set(subject) == {"subject_id", "display_name", "kind", "records"}
    assert subject["records"][0]["traceId"] == TRACE_A


def test_flatten_span_returns_the_single_span() -> None:
    span = make_span(0, 1000, 2000, operation="chat", output_text="hi")

    assert _flatten_span(_wrap(span)) == span
    assert _flatten_span({"resourceSpans": [{"scopeSpans": [{"spans": []}]}]}) is None
    assert _flatten_span({}) is None


@pytest.mark.asyncio
async def test_empty_pool_raises_empty_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    inputs = _inputs(snapshot, [SubjectRange("main", 5000, 6000)])

    with pytest.raises(EngineError) as exc:
        await engine.run(inputs, emit)
    assert exc.value.code == "EMPTY_SCOPE"
    assert events == []


@pytest.mark.asyncio
async def test_task_parse_errors_map_to_engine_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    inputs = _inputs(snapshot, [SubjectRange("main", 0, 10**9)])

    async def emit(_event: EngineEvent) -> None:
        return

    for error_type, code in (
        ("incomplete_task", "TASK_INCOMPLETE"),
        ("multiple_unrelated_tasks", "MULTIPLE_UNRELATED_TASKS"),
        ("invalid_response", "MODEL_OUTPUT_INVALID"),
    ):

        async def fail(*_args: Any, **_kwargs: Any) -> TaskParseResult:
            raise TaskParseError(error_type, "boom")

        monkeypatch.setattr(module, "run_task_parse", fail)
        with pytest.raises(EngineError) as exc:
            await engine.run(inputs, emit)
        assert exc.value.code == code

    async def crash(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        raise RuntimeError("network down")

    monkeypatch.setattr(module, "run_task_parse", crash)
    with pytest.raises(EngineError) as exc:
        await engine.run(inputs, emit)
    assert exc.value.code == "MODEL_CALL_FAILED"


@pytest.mark.asyncio
async def test_no_signals_aborts_diagnose(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    inputs = _inputs(snapshot, [SubjectRange("main", 0, 10**9)])

    async def emit(_event: EngineEvent) -> None:
        return

    async def parse(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        return _parsed()

    async def diagnose(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(signals=[], reward_overall=None, signal_mode="rule_jev", run_meta={})

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    with pytest.raises(EngineError) as exc:
        await engine.run(inputs, emit)
    assert exc.value.code == "NO_SIGNALS"


@pytest.mark.asyncio
async def test_engine_emits_three_stages_in_order_with_llm_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    async def parse(*_args: Any, **kwargs: Any) -> TaskParseResult:
        sink = kwargs["status_sink"]
        sink.report(SimpleNamespace(request_id="llm-1", stage="task_parse", status="running", iteration=None, model="fake-model", response_preview=None, error=None))
        sink.report(SimpleNamespace(request_id="llm-1", stage="task_parse", status="succeeded", iteration=1, model="fake-model", response_preview="任务描述内容", error=None))
        return _parsed()

    async def diagnose(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        sink = kwargs["status_sink"]
        sink.report(SimpleNamespace(request_id="llm-2", stage="keyword_diag", status="succeeded", iteration=None, model="fake-model", response_preview=None, error=None))
        return SimpleNamespace(
            signals=[_signal("diag-1")],
            reward_overall=RewardOverall(comment="基本完成", total_score=87),
            signal_mode="reward",
            run_meta={"reward": {"submitted": True, "budget_exceeded": False, "tokens": 10}},
            reward_payload=None,
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_attribution("diag-1", [{"attr_id": "attr-12345678", "text_md": "根因文本", "start_node_id": "main:0"}])
        runtime.record_artifact_improvement("改进意见 md")
        return SimpleNamespace(
            status="success",
            failure_kind=None,
            error_detail=None,
            completed=True,
            pending_signal_ids=[],
            skipped_signal_ids=[],
            wake_attempted=False,
            counts={},
            token_usage={},
        )

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    result = await engine.run(_inputs(snapshot, [SubjectRange("main", 0, 10**9)]), emit)

    stage_ids = [event.stage_id for event in events]
    assert stage_ids.count("task_parse") == 7  # start + 2 llm + description + 2 artifacts + finished
    kinds = [(event.stage_id, event.kind) for event in events]
    assert ("task_parse", "stage_started") == kinds[0]
    assert ("attribute", "stage_finished") == kinds[-1]
    # llm lines land in the right stage's output stream, between its events
    task_lines = [event.text_delta for event in events if event.stage_id == "task_parse" and event.kind == "output_delta"]
    assert any("llm-1" in line for line in task_lines)
    assert any("iter 1" in line for line in task_lines)
    diagnose_lines = [event.text_delta for event in events if event.stage_id == "diagnose" and event.kind == "output_delta"]
    assert any("llm-2" in line and "keyword_diag" in line for line in diagnose_lines)
    # ordering of stage boundaries
    starts = [index for index, kind in enumerate(kinds) if kind[1] == "stage_started"]
    finishes = [index for index, kind in enumerate(kinds) if kind[1] == "stage_finished"]
    assert starts == [0, kinds.index(("diagnose", "stage_started")), kinds.index(("attribute", "stage_started"))]
    assert finishes[0] < starts[1] < finishes[1] < starts[2] < finishes[2]
    assert result.headline == "总分 87 · 待归因 1 项"
    assert result.error is None
    assert result.root_causes[0].id == "attr-12345678"
    assert result.root_causes[0].category is RootCauseCategory.OTHER
    assert result.root_causes[0].confidence is None
    assert result.root_causes[0].evidence == "信号 diag-1"
    assert result.suggestions.artifact_fix is not None
    assert result.artifact_eval == ArtifactEval(applied=False, skipped_reason="not_produced")


@pytest.mark.asyncio
async def test_headline_without_reward_score(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    inputs = _inputs(snapshot, [SubjectRange("main", 0, 10**9)])

    async def emit(_event: EngineEvent) -> None:
        return

    async def parse(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        return _parsed()

    async def diagnose(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            signals=[_signal("diag-1"), _signal("diag-2", tier="perfect")],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status="success",
            failure_kind=None,
            error_detail=None,
            completed=True,
            pending_signal_ids=[],
            skipped_signal_ids=["diag-2"],
            wake_attempted=False,
            counts={},
            token_usage={},
        )

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    result = await engine.run(inputs, emit)
    assert result.headline == "待归因 1 项"


@pytest.mark.asyncio
async def test_attribution_failure_keeps_results_and_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    async def parse(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        return _parsed()

    async def diagnose(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            signals=[_signal("diag-1")],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_attribution("diag-1", [{"attr_id": "attr-aaaaaaaa", "text_md": "半成品根因", "start_node_id": "main:0"}])
        return SimpleNamespace(
            status="failed",
            failure_kind="timeout",
            error_detail=None,
            completed=False,
            pending_signal_ids=["diag-2"],
            skipped_signal_ids=[],
            wake_attempted=True,
            counts={},
            token_usage={},
        )

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    result = await engine.run(_inputs(snapshot, [SubjectRange("main", 0, 10**9)]), emit)

    assert result.root_causes and result.root_causes[0].summary == "半成品根因"
    assert result.error is not None
    assert result.error.code == "ATTRIBUTION_INCOMPLETE"
    assert "超时" in result.error.message
    finished = next(event for event in events if event.stage_id == "attribute" and event.kind == "stage_finished")
    assert finished.status == "failed"


def test_unanchored_entries_are_counted() -> None:
    node = TrajectoryNode(
        node_id="main:0",
        subagent_key="main",
        display_name="Main",
        kind="main_agent",
        role="assistant",
        content="hi",
        trace_id=TRACE_A,
        span_id=span_id(0),
        start_ts_ns=1000,
        end_ts_ns=2000,
        source_kind="llm",
    )
    nodes = {"main": [node]}
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-1", [
        {"attr_id": "attr-00000001", "text_md": "可定位", "start_node_id": "main:0"},
        {"attr_id": "attr-00000002", "text_md": "锚点丢失", "start_node_id": "main:99"},
    ])
    signals = [_signal("diag-1")]

    causes, suggestions, dropped = _translate_attribution(nodes, runtime, signals)

    assert len(causes) == 2
    assert dropped == 1
    assert causes[0].refs == [StepRef(trace_id=TRACE_A, span_id=span_id(0), subject_id="main", node_id="main:0")]
    assert causes[1].refs == []
    assert causes[0].confidence is None
    assert causes[0].evidence == "信号 diag-1"
    assert suggestions.skills == []


def test_render_llm_line_parts() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS

    event = SimpleNamespace(
        request_id="llm-3",
        stage="attribution",
        status="failed",
        iteration=2,
        model="fake-model",
        response_preview=None,
        error="boom",
    )
    line = _render_llm_line(event, _TEXTS["zh"])
    assert line == "llm-3 · attribution · iter 2 · 失败 · fake-model · boom"


def test_sink_drops_unknown_stages() -> None:
    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    sink = _QueueStatusSink(queue, {"llm_iter": "iter {iteration}", "llm_status_succeeded": "成功"})
    sink.report(SimpleNamespace(request_id="llm-1", stage="unknown_stage", status="succeeded", iteration=None, model="m", response_preview=None, error=None))
    assert queue.empty()


def test_filter_engine_result_passes_error_through() -> None:
    from jiuwenswarm.diagnosis.snapshot import ScopeIndex

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    ranges = [SubjectRange("main", 0, 10**9)]
    index = ScopeIndex.build(snapshot, ranges)
    result = EngineResult(error=DiagnosisError(code="ATTRIBUTION_INCOMPLETE", message="归因未完成"))

    filtered = filter_engine_result(result, index=index, profile=ModeProfile.AGENT_WORK)

    assert filtered.error is not None
    assert filtered.error.code == "ATTRIBUTION_INCOMPLETE"


def test_apply_result_stores_engine_error() -> None:
    record = make_record()
    result = EngineResult(error=DiagnosisError(code="ATTRIBUTION_INCOMPLETE", message="归因未完成：超时"))

    DiagnosisService._apply_result(record, result)

    assert record.error is not None
    assert record.error.code == "ATTRIBUTION_INCOMPLETE"
    assert record.evidence.artifact_eval is not None
    assert record.evidence.artifact_eval.applied is False
