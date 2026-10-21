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
async def test_no_signals_finishes_normally_and_skips_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    inputs = _inputs(snapshot, [SubjectRange("main", 0, 10**9)])
    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    async def parse(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        return _parsed()

    async def diagnose(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            signals=[],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
            reward_payload=None,
        )

    async def attribute(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        raise AssertionError("attribution must not run without signals")

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    result = await engine.run(inputs, emit)

    assert result.error is None
    assert result.root_causes == []
    assert result.headline == "没有可归因的信号，无需归因"
    finished = {event.stage_id: event for event in events if event.kind == "stage_finished"}
    assert finished["diagnose"].status == "completed"
    assert finished["diagnose"].summary == "信号 0 条（规则模式 · Reward 已跳过：无任务产物）"
    assert finished["attribute"].status == "skipped"
    assert finished["attribute"].skip_reason == "诊断分析没有产出可归因的信号，已跳过归因"

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
            reward_payload={"overall": {"total_score": 87}},
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
    assert result.headline == "总分 87 · 定位 1 处问题"
    assert result.task_description == "装好依赖并跑测试"
    assert result.reward == {"overall": {"total_score": 87}}
    cause = result.root_causes[0]
    assert cause.signal is not None
    assert cause.signal.id == "diag-1"
    assert cause.signal.source == "keyword_tool"
    assert cause.signal.node_ids == ["main:0"]
    finished_attribute = next(
        event for event in events if event.stage_id == "attribute" and event.kind == "stage_finished"
    )
    assert finished_attribute.summary == "定位 1 处 · 来自 1 条信号 · 标无效 0 条 · 产物修复建议 1 条"
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

    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    async def parse(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        return _parsed()

    async def diagnose(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            signals=[_signal("diag-1"), _signal("diag-2", tier="perfect")],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
            reward_payload=None,
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
    # No cause was reported, attribution finished: the headline says so.
    assert result.headline == "未发现需要归因的问题"
    assert result.reward is None
    assert result.task_description == "装好依赖并跑测试"
    diagnose_finished = next(
        event for event in events if event.stage_id == "diagnose" and event.kind == "stage_finished"
    )
    assert diagnose_finished.summary == "信号 2 条（规则模式 · Reward 已跳过：无任务产物）"


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
            reward_payload=None,
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
    assert result.headline == "定位 1 处问题"
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


def _single_node() -> dict[str, list[TrajectoryNode]]:
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
    return {"main": [node]}


def _drain(queue: asyncio.Queue[EngineEvent | None]) -> list[EngineEvent]:
    events: list[EngineEvent] = []
    while not queue.empty():
        event = queue.get_nowait()
        assert event is not None
        events.append(event)
    return events


def _lines(queue: asyncio.Queue[EngineEvent | None]) -> list[str | None]:
    """Log lines of the drained events; root cause updates are not log lines."""
    return [event.text_delta for event in _drain(queue) if event.kind == "output_delta"]


def test_streaming_runtime_reports_root_causes_per_signal() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    signal = _signal("diag-1")
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"], [signal])

    runtime.record_attribution("diag-1", [{"attr_id": "attr-1", "text_md": "原因 A", "start_node_id": "main:0"}])
    first = [event for event in _drain(queue) if event.kind == "root_causes"]
    assert len(first) == 1
    assert first[0].stage_id == "attribute"
    assert first[0].signal_id == "diag-1"
    cause = first[0].root_causes[0]
    assert cause.id == "attr-1"
    assert cause.summary == "原因 A"
    assert cause.evidence == "信号 diag-1"
    assert cause.signal is not None and cause.signal.id == "diag-1"
    assert cause.refs == [StepRef(trace_id=TRACE_A, span_id=span_id(0), subject_id="main", node_id="main:0")]

    # A resubmission reports everything now held for the signal, so the host can replace it.
    runtime.record_attribution("diag-1", [
        {"attr_id": "attr-2", "text_md": "新", "start_node_id": "main:0"},
        {"attr_id": "attr-3", "text_md": "另一处", "start_node_id": "main:0"},
    ])
    second = [event for event in _drain(queue) if event.kind == "root_causes"]
    assert [item.id for item in second[0].root_causes] == ["attr-2", "attr-3"]


def test_streaming_runtime_reports_causes_without_a_known_signal() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"])

    runtime.record_attribution("diag-9", [{"attr_id": "attr-1", "text_md": "原因", "start_node_id": "main:0"}])

    event = next(item for item in _drain(queue) if item.kind == "root_causes")
    assert event.root_causes[0].signal is None
    assert event.root_causes[0].evidence == ""


def test_streaming_runtime_logs_each_submission_as_it_happens() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"])

    runtime.record_attribution("diag-1", [{"attr_id": "attr-1", "text_md": "原因 A", "start_node_id": "main:0"}])
    assert _lines(queue) == ["[attr-1] 原因 A（main:0）"]

    runtime.mark_signal_invalid("diag-2")
    runtime.record_artifact_improvement("改进")
    runtime.record_skill_improvement("demo-skill", "改进")
    lines = _lines(queue)
    assert lines == ["标记无效：diag-2", "已提交产物改进意见", "已提交 Skill 改进：demo-skill"]
    # The stored state still feeds the root causes.
    assert runtime.invalid_signals == {"diag-2"}
    assert runtime.artifact_improvement == "改进"
    assert runtime.skill_improvements == {"demo-skill": "改进"}


def test_streaming_runtime_marks_resubmissions_as_updated() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"])
    runtime.record_attribution("diag-1", [{"attr_id": "attr-1", "text_md": "旧", "start_node_id": "main:0"}])
    runtime.record_attribution("diag-1", [
        {"attr_id": "attr-2", "text_md": "新", "start_node_id": "main:0", "end_node_id": "main:3"},
    ])
    runtime.record_artifact_improvement("一")
    runtime.record_artifact_improvement("二")

    lines = _lines(queue)
    assert lines == [
        "[attr-1] 旧（main:0）",
        "（更新）[attr-2] 新（main:0 → main:3）",
        "已提交产物改进意见",
        "（更新）已提交产物改进意见",
    ]
    # An overwritten submission leaves only the last entries behind.
    assert [entry["attr_id"] for entry in runtime.attributions["diag-1"]] == ["attr-2"]


@pytest.mark.asyncio
async def test_attribute_stage_logs_submissions_before_the_run_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.diagnosis.attribution_engine as module

    engine = AttributionCoreEngine(model_factory=lambda _model: object())
    events: list[EngineEvent] = []
    seen_before_return: list[int] = []

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
            reward_payload=None,
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_attribution("diag-1", [{"attr_id": "attr-1", "text_md": "原因", "start_node_id": "main:0"}])
        await asyncio.sleep(0.05)
        # The writer drains concurrently, so the line is already out while the agent is still working.
        seen_before_return.append(
            sum(1 for event in events if event.stage_id == "attribute" and event.text_delta == "[attr-1] 原因（main:0）"),
        )
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

    await engine.run(_inputs(_snapshot([_chat_record(0, 1000, 2000)]), [SubjectRange("main", 0, 10**9)]), emit)

    assert seen_before_return == [1]
    # No second copy is written after the run.
    entry_lines = [event for event in events if event.text_delta == "[attr-1] 原因（main:0）"]
    assert len(entry_lines) == 1


def test_root_cause_carries_reward_signal_reference() -> None:
    nodes = _single_node()
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-r", [{"attr_id": "attr-00000001", "text_md": "原因", "start_node_id": "main:0"}])
    signal = DiagnosticSignal.create(
        text="测试应当通过\n仍有用例失败",
        source=SignalSource.REWARD,
        tier="weak",
        node_ids=["main:0"],
        signal_id="diag-r",
        score=60.0,
        metadata={"category": "正确性"},
    )

    causes, _suggestions, _dropped = _translate_attribution(nodes, runtime, [signal])

    ref = causes[0].signal
    assert ref is not None
    assert (ref.id, ref.source, ref.tier, ref.score, ref.category) == ("diag-r", "reward", "weak", 60.0, "正确性")
    assert ref.node_ids == ["main:0"]
    assert causes[0].evidence == "测试应当通过\n仍有用例失败"


def test_root_cause_signal_reference_for_rule_signal_has_no_score() -> None:
    nodes = _single_node()
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-1", [{"attr_id": "attr-00000001", "text_md": "原因", "start_node_id": "main:0"}])

    causes, _suggestions, _dropped = _translate_attribution(nodes, runtime, [_signal("diag-1", tier="uncertain")])

    ref = causes[0].signal
    assert ref is not None
    assert (ref.source, ref.tier, ref.score, ref.category) == ("keyword_tool", "uncertain", None, None)


def test_root_cause_without_known_signal_has_no_reference() -> None:
    nodes = _single_node()
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-x", [{"attr_id": "attr-00000001", "text_md": "原因", "start_node_id": "main:0"}])

    causes, _suggestions, _dropped = _translate_attribution(nodes, runtime, [])

    assert causes[0].signal is None
    assert causes[0].evidence == ""


def test_skill_suggestion_does_not_repeat_markdown() -> None:
    nodes = _single_node()
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-1", [{"attr_id": "attr-00000001", "text_md": "原因", "start_node_id": "main:0"}])
    runtime.record_skill_improvement("demo-skill", "## 改进\n- 加一步")

    _causes, suggestions, _dropped = _translate_attribution(nodes, runtime, [_signal("diag-1")])

    skill = suggestions.skills[0]
    assert skill.change_summary == skill.message == "## 改进\n- 加一步"
    assert skill.rationale == ""


def test_headline_variants() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _compose_headline

    zh = _TEXTS["zh"]
    en = _TEXTS["en"]
    assert _compose_headline(zh, score=90, located=2, failed=False) == "总分 90 · 定位 2 处问题"
    assert _compose_headline(zh, score=None, located=2, failed=False) == "定位 2 处问题"
    assert _compose_headline(zh, score=95, located=0, failed=False) == "总分 95 · 未发现需要归因的问题"
    assert _compose_headline(zh, score=None, located=0, failed=False) == "未发现需要归因的问题"
    assert _compose_headline(zh, score=70, located=0, failed=True) == "总分 70"
    assert _compose_headline(zh, score=None, located=0, failed=True) is None
    assert _compose_headline(en, score=90, located=2, failed=False) == "Score 90 · 2 issues located"
    assert _compose_headline(en, score=None, located=0, failed=False) == "No issues to attribute"


def test_clip_folds_whitespace_and_truncates() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _clip

    assert _clip("a\n\n b\t c", 20) == "a b c"
    assert _clip("x" * 10, 5) == "xxxxx…"


def test_attribute_summary_counts_entries_and_dropped() -> None:
    nodes = _single_node()
    runtime = NodeBackedRuntime(nodes, "zh")
    runtime.record_attribution("diag-1", [
        {"attr_id": "attr-00000001", "text_md": "可定位", "start_node_id": "main:0"},
        {"attr_id": "attr-00000002", "text_md": "第二条", "start_node_id": "main:0"},
        {"attr_id": "attr-00000003", "text_md": "锚点丢失", "start_node_id": "main:99"},
    ])

    causes, _suggestions, dropped = _translate_attribution(nodes, runtime, [_signal("diag-1")])

    # Two located entries come from one signal; the lost anchor is counted apart.
    assert len(causes) - dropped == 2
    assert len(runtime.attributions) == 1
    assert dropped == 1


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


def test_filter_and_apply_carry_reward_task_and_signal_reference() -> None:
    from jiuwenswarm.diagnosis.models import RootCause, SignalRef
    from jiuwenswarm.diagnosis.snapshot import ScopeIndex

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    index = ScopeIndex.build(snapshot, [SubjectRange("main", 0, 10**9)])
    ref = StepRef(trace_id=TRACE_A, span_id=span_id(0), subject_id="main", node_id="main:0")
    cause = RootCause(
        id="attr-1",
        category=RootCauseCategory.OTHER,
        subject_id="main",
        refs=[ref],
        summary="原因",
        signal=SignalRef(id="diag-1", source="reward", tier="weak", score=60.0, category="正确性"),
    )
    result = EngineResult(
        root_causes=[cause],
        reward={"overall": {"total_score": 60}},
        task_description="装好依赖并跑测试",
    )

    filtered = filter_engine_result(result, index=index, profile=ModeProfile.AGENT_WORK)
    record = make_record()
    DiagnosisService._apply_result(record, filtered)

    assert filtered.reward == {"overall": {"total_score": 60}}
    assert filtered.task_description == "装好依赖并跑测试"
    assert filtered.root_causes[0].signal is not None
    assert record.reward == {"overall": {"total_score": 60}}
    assert record.task_description == "装好依赖并跑测试"
    assert record.root_causes[0].signal is not None
    assert record.root_causes[0].signal.category == "正确性"


def test_apply_result_stores_engine_error() -> None:
    record = make_record()
    result = EngineResult(error=DiagnosisError(code="ATTRIBUTION_INCOMPLETE", message="归因未完成：超时"))

    DiagnosisService._apply_result(record, result)

    assert record.error is not None
    assert record.error.code == "ATTRIBUTION_INCOMPLETE"
    assert record.evidence.artifact_eval is not None
    assert record.evidence.artifact_eval.applied is False


def test_production_host_runs_diagnoses_with_the_attribution_core_engine() -> None:
    from jiuwenswarm.server.diagnosis.host import build_diagnosis_engine

    assert isinstance(build_diagnosis_engine(), AttributionCoreEngine)
