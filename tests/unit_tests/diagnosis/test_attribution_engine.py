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
    _translate_evolution_judge,
    _translate_reward_failure_causes,
)
from jiuwenswarm.diagnosis.engine import EngineError, EngineEvent, EngineInputs, EngineResult
from jiuwenswarm.diagnosis.model_resolver import ResolvedDiagnosisModel
from jiuwenswarm.diagnosis.models import (
    DiagnosisError,
    ModeProfile,
    ModelInfo,
    RewardFailureCause,
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


def _summary_outcome(summary: str = "选中范围内共执行了 3 个步骤，期间出现一次工具报错。") -> SimpleNamespace:
    return SimpleNamespace(trajectory_summary=summary, status="success", completed=True)


def _signal(signal_id: str, tier: str = "weak", *, source: SignalSource = SignalSource.KEYWORD_TOOL) -> DiagnosticSignal:
    return DiagnosticSignal.create(
        text=f"信号 {signal_id}",
        source=source,
        tier=tier,
        node_ids=["main:0"],
        signal_id=signal_id,
    )


def _reward_signal(signal_id: str = "diag-r", *, text: str = "测试应当通过\n仍有用例失败") -> DiagnosticSignal:
    return DiagnosticSignal.create(
        text=text,
        source=SignalSource.REWARD,
        tier="weak",
        node_ids=["main:0"],
        signal_id=signal_id,
        score=60.0,
        metadata={"category": "正确性"},
    )


def _reward_payload(*requirements: str, comment: str = "fix it") -> dict[str, Any]:
    return {
        "schema_version": "attribution.reward.v1",
        "overall": {"comment": "整体", "total_score": 60},
        "categories": [],
        "dimensions": [
            {"category": "c", "requirement": requirement, "comment": comment, "score": 60}
            for requirement in requirements
        ],
    }


def _node() -> TrajectoryNode:
    return TrajectoryNode(
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


def _single_node() -> dict[str, list[TrajectoryNode]]:
    return {"main": [_node()]}


def _drain(queue: asyncio.Queue[EngineEvent | None]) -> list[EngineEvent]:
    events: list[EngineEvent] = []
    while not queue.empty():
        event = queue.get_nowait()
        assert event is not None
        events.append(event)
    return events


def _lines(queue: asyncio.Queue[EngineEvent | None]) -> list[str | None]:
    """Log lines of the drained events; table updates are not log lines."""
    return [event.text_delta for event in _drain(queue) if event.kind == "output_delta"]


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
async def test_empty_pool_raises_empty_scope() -> None:
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

    async def summary(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        sink = kwargs.get("status_sink")
        if sink is not None:
            sink.report(
                SimpleNamespace(
                    request_id="llm-9",
                    stage="attribution",
                    iteration=1,
                    status="succeeded",
                    model="fake-model",
                    response_preview=None,
                    error=None,
                ),
            )
        return _summary_outcome("任务解析失败，但已阅读选中范围的轨迹。")

    monkeypatch.setattr(module, "run_trajectory_summary", summary)
    events: list[EngineEvent] = []

    async def emit(event: EngineEvent) -> None:
        events.append(event)

    expected_summary = {
        "TASK_INCOMPLETE": "选中范围不包含完整任务，无法提取任务描述",
        "MULTIPLE_UNRELATED_TASKS": "选中范围包含多个互不相关的任务，无法归因",
    }

    for error_type, code in (
        ("incomplete_task", "TASK_INCOMPLETE"),
        ("multiple_unrelated_tasks", "MULTIPLE_UNRELATED_TASKS"),
    ):

        async def fail(*_args: Any, **_kwargs: Any) -> TaskParseResult:
            raise TaskParseError(error_type, "boom")

        events.clear()
        monkeypatch.setattr(module, "run_task_parse", fail)
        result = await engine.run(inputs, emit)
        assert result.error is None
        assert result.trajectory_summary == "任务解析失败，但已阅读选中范围的轨迹。"
        assert result.reward_failure_causes == [] and result.evolution_judge == []
        finished = {
            (event.stage_id, event.status)
            for event in events
            if event.kind == "stage_finished"
        }
        assert ("task_parse", "completed") in finished
        assert ("task_parse", "failed") not in finished
        assert ("diagnose", "skipped") in finished
        assert ("attribute", "skipped") in finished
        assert any(
            event.stage_id == "task_parse"
            and event.kind == "output_delta"
            and event.text_delta == "正在生成轨迹摘要"
            for event in events
        )
        assert any(
            event.stage_id == "task_parse" and event.kind == "output_delta" and "llm-9" in (event.text_delta or "")
            for event in events
        )
        assert not any(event.stage_id == "attribute" and event.kind == "output_delta" for event in events)
        finished_parse = next(
            event for event in events
            if event.stage_id == "task_parse" and event.kind == "stage_finished"
        )
        assert finished_parse.summary == expected_summary[code]
        assert all(
            event.skip_reason == "未解析出任务，已改为生成轨迹摘要"
            for event in events
            if event.kind == "stage_finished" and event.status == "skipped"
        )

    async def invalid(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        raise TaskParseError("invalid_response", "boom")

    events.clear()
    monkeypatch.setattr(module, "run_task_parse", invalid)
    result = await engine.run(inputs, emit)
    assert result.error is not None and result.error.code == "MODEL_OUTPUT_INVALID"
    assert result.error.message == "任务解析输出无法解析：boom"
    assert result.trajectory_summary == ""
    failed = next(
        event for event in events
        if event.stage_id == "task_parse" and event.kind == "stage_finished"
    )
    assert failed.status == "failed" and failed.summary == result.error.message
    assert not any(event.status == "skipped" for event in events)

    async def crash(*_args: Any, **_kwargs: Any) -> TaskParseResult:
        raise RuntimeError("network down")

    events.clear()
    monkeypatch.setattr(module, "run_task_parse", crash)
    result = await engine.run(inputs, emit)
    assert result.error is not None and result.error.code == "MODEL_CALL_FAILED"
    assert result.error.message == "任务解析模型调用失败：network down"
    assert result.trajectory_summary == ""
    failed = next(
        event for event in events
        if event.stage_id == "task_parse" and event.kind == "stage_finished"
    )
    assert failed.status == "failed" and failed.summary == result.error.message
    assert not any(event.status == "skipped" for event in events)


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

    async def summary(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return _summary_outcome("选中范围没有产生诊断信号，轨迹主要是任务收尾。")

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)
    monkeypatch.setattr(module, "run_trajectory_summary", summary)

    result = await engine.run(inputs, emit)

    assert result.error is None
    assert result.trajectory_summary == "选中范围没有产生诊断信号，轨迹主要是任务收尾。"
    assert result.reward_failure_causes == []
    assert result.evolution_judge == []
    assert all(event.kind != "reward" for event in events)
    finished = {event.stage_id: event for event in events if event.kind == "stage_finished"}
    assert finished["diagnose"].status == "completed"
    assert finished["diagnose"].summary == "信号 0 条（规则模式 · Reward 已跳过：无任务产物）"
    assert finished["attribute"].status == "skipped"
    assert finished["attribute"].skip_reason == "诊断分析没有产出可归因的信号，已跳过归因"
    assert finished["attribute"].summary == "已跳过归因 · 轨迹摘要已生成"
    attribute = [event for event in events if event.stage_id == "attribute"]
    assert [event.kind for event in attribute] == ["stage_started", "output_delta", "stage_finished"]
    assert attribute[1].text_delta == "正在生成轨迹摘要"


@pytest.mark.asyncio
async def test_only_perfect_signals_still_run_summary_only(monkeypatch: pytest.MonkeyPatch) -> None:
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
            signals=[_signal("diag-p", tier="perfect")],
            reward_overall=RewardOverall(comment="很好", total_score=95),
            signal_mode="reward",
            run_meta={"reward": {"submitted": True}},
            reward_payload=_reward_payload("测试应当通过"),
        )

    async def attribute(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        raise AssertionError("the attribution agent must not run for perfect-only signals")

    async def summary(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return _summary_outcome("所有评分维度均已达标，轨迹无失败归因。")

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)
    monkeypatch.setattr(module, "run_trajectory_summary", summary)

    result = await engine.run(inputs, emit)

    assert result.error is None
    assert result.trajectory_summary == "所有评分维度均已达标，轨迹无失败归因。"
    assert result.reward_failure_causes == [] and result.evolution_judge == []
    assert result.reward == _reward_payload("测试应当通过")
    kinds = [(event.stage_id, event.kind) for event in events]
    reward_at = kinds.index(("diagnose", "reward"))
    assert reward_at < kinds.index(("diagnose", "stage_finished")) < kinds.index(("attribute", "stage_started"))
    assert events[reward_at].reward == _reward_payload("测试应当通过")
    finished = next(event for event in events if event.stage_id == "attribute" and event.kind == "stage_finished")
    assert finished.status == "completed"


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
            signals=[_reward_signal()],
            reward_overall=RewardOverall(comment="基本完成", total_score=87),
            signal_mode="reward",
            run_meta={"reward": {"submitted": True, "budget_exceeded": False, "tokens": 10}},
            reward_payload=_reward_payload("测试应当通过"),
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_reward_failure(
            "diag-r",
            [
                {
                    "attr_id": "attr-12345678",
                    "text_md": "根因文本",
                    "start_node_id": "main:0",
                    "suggestion": "把结论移到开头",
                }
            ],
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
            trajectory_summary="这段轨迹跑了测试但仍有失败",
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
    reward_at = kinds.index(("diagnose", "reward"))
    assert reward_at < kinds.index(("diagnose", "stage_finished")) < kinds.index(("attribute", "stage_started"))
    assert events[reward_at].reward == _reward_payload("测试应当通过")
    task_lines = [event.text_delta for event in events if event.stage_id == "task_parse" and event.kind == "output_delta"]
    assert any("llm-1" in line for line in task_lines)
    assert any("iter 1" in line for line in task_lines)
    diagnose_lines = [event.text_delta for event in events if event.stage_id == "diagnose" and event.kind == "output_delta"]
    assert any("llm-2" in line and "keyword_diag" in line for line in diagnose_lines)
    starts = [index for index, kind in enumerate(kinds) if kind[1] == "stage_started"]
    finishes = [index for index, kind in enumerate(kinds) if kind[1] == "stage_finished"]
    assert starts == [0, kinds.index(("diagnose", "stage_started")), kinds.index(("attribute", "stage_started"))]
    assert finishes[0] < starts[1] < finishes[1] < starts[2] < finishes[2]
    assert result.trajectory_summary == "这段轨迹跑了测试但仍有失败"
    assert result.reward == _reward_payload("测试应当通过")
    cause = result.reward_failure_causes[0]
    assert cause.dimension == "测试应当通过"
    assert cause.suggestion == ["把结论移到开头"]
    assert cause.signal is not None
    assert cause.signal.id == "diag-r"
    assert cause.signal.source == "reward"
    assert cause.signal.node_ids == ["main:0"]
    assert cause.evidence[0].node_id == "main:0"
    finished_attribute = next(
        event for event in events if event.stage_id == "attribute" and event.kind == "stage_finished"
    )
    assert finished_attribute.summary == "轨迹摘要已生成 · 失败原因 1 条 · 演进项 0 条"
    assert result.error is None


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
            signals=[_reward_signal()],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
            reward_payload=None,
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_reward_failure(
            "diag-r",
            [
                {
                    "attr_id": "attr-aaaaaaaa",
                    "text_md": "半成品原因",
                    "start_node_id": "main:0",
                    "suggestion": "补上缺失的对比",
                }
            ],
        )
        return SimpleNamespace(
            status="failed",
            failure_kind="timeout",
            error_detail=None,
            completed=False,
            pending_signal_ids=["diag-r"],
            skipped_signal_ids=[],
            wake_attempted=True,
            counts={},
            token_usage={},
            trajectory_summary="归因超时前写了一半",
        )

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    result = await engine.run(_inputs(snapshot, [SubjectRange("main", 0, 10**9)]), emit)

    assert result.trajectory_summary == "归因超时前写了一半"
    assert result.reward_failure_causes and result.reward_failure_causes[0].summary == "半成品原因"
    assert result.error is not None
    assert result.error.code == "ATTRIBUTION_INCOMPLETE"
    assert "超时" in result.error.message
    finished = next(event for event in events if event.stage_id == "attribute" and event.kind == "stage_finished")
    assert finished.status == "failed"


def test_translate_reward_failure_causes_anchors_and_suggestions() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    runtime.record_reward_failure(
        "diag-r",
        [
            {"attr_id": "attr-00000001", "text_md": "可定位", "start_node_id": "main:0", "suggestion": "改这里"},
            {"attr_id": "attr-00000002", "text_md": "锚点丢失", "start_node_id": "main:99", "suggestion": "改那里"},
        ],
    )
    signal = _reward_signal()

    causes, dropped = _translate_reward_failure_causes(runtime, [signal], _reward_payload("测试应当通过"), _single_node())

    assert len(causes) == 1 and dropped == 1
    cause = causes[0]
    assert cause.id == "rfc-1"
    assert cause.dimension == "测试应当通过"
    assert cause.evidence == [StepRef(trace_id=TRACE_A, span_id=span_id(0), subject_id="main", node_id="main:0")]
    assert cause.suggestion == ["改这里"]
    assert cause.signal is not None and cause.signal.id == "diag-r"
    assert cause.signal.text == "测试应当通过\n仍有用例失败"


def test_agent_suggestion_wins_over_the_reward_comment() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    runtime.record_reward_failure(
        "diag-r",
        [{"attr_id": "attr-1", "text_md": "原因", "start_node_id": "main:0", "suggestion": "把对比补上"}],
    )

    causes, _dropped = _translate_reward_failure_causes(
        runtime, [_reward_signal()], _reward_payload("测试应当通过"), _single_node()
    )

    assert causes[0].suggestion == ["把对比补上"]


def test_reward_comment_is_only_a_fallback_for_entries_without_a_suggestion() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    runtime.record_reward_failure(
        "diag-r", [{"attr_id": "attr-1", "text_md": "原因", "start_node_id": "main:0"}]
    )
    causes, _dropped = _translate_reward_failure_causes(
        runtime, [_reward_signal()], _reward_payload("测试应当通过"), _single_node()
    )
    assert causes[0].suggestion == ["fix it"]

    other = NodeBackedRuntime(_single_node(), "zh")
    other.record_reward_failure(
        "diag-r", [{"attr_id": "attr-1", "text_md": "原因", "start_node_id": "main:0"}]
    )
    missing, _dropped = _translate_reward_failure_causes(
        other, [_reward_signal()], _reward_payload("别的需求"), _single_node()
    )
    assert missing[0].suggestion == []


def test_translate_skill_evolution_judge() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    signal = _signal("diag-1")
    runtime.mark_signal_topic("diag-1", "skill", "demo-skill")
    runtime.record_skill_cause(
        "diag-1", [{"attr_id": "attr-1", "text_md": "skill 用错", "start_node_id": "main:0"}]
    )
    runtime.record_skill_evolution("demo-skill", "## 改进\n- 加一步", 0.8)

    judges, dropped = _translate_evolution_judge(runtime, [signal], _single_node())

    assert dropped == 0
    assert len(judges) == 1
    judge = judges[0]
    assert judge.category == "skill"
    assert judge.target == "demo-skill"
    assert judge.score == 0.8
    assert judge.suggestion == ["## 改进\n- 加一步"]
    assert judge.summary == "skill 用错"
    assert judge.evidence[0].node_id == "main:0"
    assert judge.related_signals and judge.related_signals[0].signal_id == "diag-1"
    assert judge.related_signals[0].entries[0]["text_md"] == "skill 用错"


def test_related_signal_carries_the_signal_category_and_score() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    signal = _reward_signal("diag-r")
    runtime.mark_signal_topic("diag-r", "skill", "demo-skill")
    runtime.record_skill_cause(
        "diag-r", [{"attr_id": "attr-1", "text_md": "x", "start_node_id": "main:0"}]
    )
    runtime.record_skill_evolution("demo-skill", "advice", 0.5)

    judges, _dropped = _translate_evolution_judge(runtime, [signal], _single_node())

    related = judges[0].related_signals[0]
    assert (related.category, related.score) == ("正确性", 60.0)
    assert related.node_ids == ["main:0"]


def test_translate_experience_evolution_judge() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    signal = _signal("diag-1")
    runtime.mark_signal_topic("diag-1", "experience")
    runtime.record_experience_cause(
        "diag-1", [{"attr_id": "attr-1", "text_md": "经验依据", "start_node_id": "main:0"}]
    )
    runtime.record_experience_evolution("partial", ["先核对环境"])

    judges, _dropped = _translate_evolution_judge(runtime, [signal], _single_node())

    judge = judges[0]
    assert judge.category == "experience"
    assert judge.outcome == "partial"
    assert judge.score == 0.5
    assert judge.target is None
    assert judge.suggestion == ["先核对环境"]


def test_translate_evolution_judge_skips_unanchored_items() -> None:
    runtime = NodeBackedRuntime(_single_node(), "zh")
    runtime.mark_signal_topic("diag-1", "skill", "demo-skill")
    runtime.record_skill_cause(
        "diag-1", [{"attr_id": "attr-2", "text_md": "x", "start_node_id": "main:99"}]
    )
    runtime.skill_evolutions["demo-skill"] = {"md_string": "advice", "confidence": 0.5}

    judges, dropped = _translate_evolution_judge(runtime, [_signal("diag-1")], _single_node())

    assert judges == [] and dropped == 1


def test_streaming_runtime_pushes_whole_tables() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    signal = _reward_signal()
    runtime = _StreamingRuntime(
        _single_node(), "zh", queue, _TEXTS["zh"], [signal], _reward_payload("测试应当通过")
    )

    runtime.record_reward_failure(
        "diag-r",
        [{"attr_id": "attr-1", "text_md": "原因 A", "start_node_id": "main:0", "suggestion": "改 A"}],
    )
    events = _drain(queue)
    tables = [event for event in events if event.kind == "reward_failure_causes"]
    assert len(tables) == 1
    assert tables[0].stage_id == "attribute"
    assert [cause.id for cause in tables[0].reward_failure_causes] == ["rfc-1"]
    assert events[0].kind == "output_delta"  # the entry log line comes first


def test_streaming_runtime_pushes_evolution_tables_and_logs() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    signal = _signal("diag-1")
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"], [signal])

    runtime.mark_signal_topic("diag-1", "skill", "demo-skill")
    assert _lines(queue) == ["标记 skill：diag-1"]
    runtime.record_skill_cause(
        "diag-1", [{"attr_id": "attr-1", "text_md": "原因 A", "start_node_id": "main:0"}]
    )
    runtime.record_skill_evolution("demo-skill", "改进", 0.7)
    events = _drain(queue)
    table_events = [event for event in events if event.kind == "evolution_judge"]
    assert table_events and table_events[-1].evolution_judge[0].target == "demo-skill"
    assert table_events[-1].evolution_judge[0].score == 0.7
    assert [event.text_delta for event in events if event.kind == "output_delta"] == [
        "[attr-1] 原因 A（main:0）",
        "已提交 Skill 改进：demo-skill",
    ]


def test_streaming_runtime_logs_marks_invalid_and_summary() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    runtime = _StreamingRuntime(_single_node(), "zh", queue, _TEXTS["zh"])

    runtime.mark_signal_invalid("diag-2")
    runtime.record_experience_evolution("partial", ["tip"])
    runtime.record_trajectory_summary("这段轨迹的摘要")
    lines = _lines(queue)
    assert lines == ["标记无效：diag-2", "已提交经验判定：partial", "这段轨迹的摘要"]
    assert runtime.invalid_signals == {"diag-2"}
    assert runtime.experience_evolution == {"outcome": "partial", "suggestions": ["tip"]}
    assert runtime.trajectory_summary == "这段轨迹的摘要"


def test_streaming_runtime_marks_resubmissions_as_updated() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _TEXTS, _StreamingRuntime

    queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
    signal = _reward_signal()
    runtime = _StreamingRuntime(
        _single_node(), "zh", queue, _TEXTS["zh"], [signal], _reward_payload("测试应当通过")
    )
    runtime.record_reward_failure(
        "diag-r",
        [{"attr_id": "attr-1", "text_md": "旧", "start_node_id": "main:0", "suggestion": "改旧"}],
    )
    runtime.record_reward_failure(
        "diag-r",
        [
            {
                "attr_id": "attr-2",
                "text_md": "新",
                "start_node_id": "main:0",
                "end_node_id": "main:0",
                "suggestion": "改新",
            }
        ],
    )
    runtime.record_trajectory_summary("摘要一")
    runtime.record_trajectory_summary("摘要二")

    lines = _lines(queue)
    assert lines == [
        "[attr-1] 旧（main:0）",
        "（更新）[attr-2] 新（main:0）",
        "摘要一",
        "（更新）摘要二",
    ]
    assert [entry["attr_id"] for entry in runtime.reward_attributions["diag-r"]] == ["attr-2"]


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
            signals=[_reward_signal()],
            reward_overall=None,
            signal_mode="rule_jev",
            run_meta={"reward": {"skipped": "no artifacts"}},
            reward_payload=None,
        )

    async def attribute(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        runtime = kwargs["runtime"]
        runtime.record_reward_failure(
            "diag-r",
            [
                {
                    "attr_id": "attr-1",
                    "text_md": "原因",
                    "start_node_id": "main:0",
                    "suggestion": "改一下",
                }
            ],
        )
        await asyncio.sleep(0.05)
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
            trajectory_summary="summary",
        )

    monkeypatch.setattr(module, "run_task_parse", parse)
    monkeypatch.setattr(module, "run_diagnosis", diagnose)
    monkeypatch.setattr(module, "run_attribution", attribute)

    await engine.run(_inputs(_snapshot([_chat_record(0, 1000, 2000)]), [SubjectRange("main", 0, 10**9)]), emit)

    assert seen_before_return == [1]
    entry_lines = [event for event in events if event.text_delta == "[attr-1] 原因（main:0）"]
    assert len(entry_lines) == 1


def test_clip_folds_whitespace_and_truncates() -> None:
    from jiuwenswarm.diagnosis.attribution_engine import _clip

    assert _clip("a\n\n b\t c", 20) == "a b c"
    assert _clip("x" * 10, 5) == "xxxxx…"


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


def test_filter_and_apply_carry_reward_summary_and_signal_reference() -> None:
    from jiuwenswarm.diagnosis.models import SignalRef
    from jiuwenswarm.diagnosis.snapshot import ScopeIndex

    snapshot = _snapshot([_chat_record(0, 1000, 2000)])
    index = ScopeIndex.build(snapshot, [SubjectRange("main", 0, 10**9)])
    ref = StepRef(trace_id=TRACE_A, span_id=span_id(0), subject_id="main", node_id="main:0")
    cause = RewardFailureCause(
        id="rfc-1",
        dimension="正确性",
        summary="原因",
        evidence=[ref],
        suggestion=["改进"],
        signal=SignalRef(id="diag-1", source="reward", tier="weak", score=60.0, category="正确性"),
    )
    result = EngineResult(
        trajectory_summary="轨迹摘要",
        reward_failure_causes=[cause],
        reward=_reward_payload("正确性"),
    )

    filtered = filter_engine_result(result, index=index, profile=ModeProfile.AGENT_WORK)
    record = make_record()
    DiagnosisService._apply_result(record, filtered)

    assert filtered.reward == _reward_payload("正确性")
    assert filtered.trajectory_summary == "轨迹摘要"
    assert filtered.reward_failure_causes[0].signal is not None
    assert record.reward == _reward_payload("正确性")
    assert record.trajectory_summary == "轨迹摘要"
    assert record.reward_failure_causes[0].signal is not None
    assert record.reward_failure_causes[0].signal.category == "正确性"


def test_apply_result_stores_engine_error() -> None:
    record = make_record()
    result = EngineResult(
        trajectory_summary="摘要",
        error=DiagnosisError(code="ATTRIBUTION_INCOMPLETE", message="归因未完成：超时"),
    )

    DiagnosisService._apply_result(record, result)

    assert record.error is not None
    assert record.error.code == "ATTRIBUTION_INCOMPLETE"
    assert record.trajectory_summary == "摘要"


def test_production_host_runs_diagnoses_with_the_attribution_core_engine() -> None:
    from jiuwenswarm.server.diagnosis.host import build_diagnosis_engine

    assert isinstance(build_diagnosis_engine(), AttributionCoreEngine)
