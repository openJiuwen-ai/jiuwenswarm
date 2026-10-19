from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

import jiuwenswarm.symphony.evaluation as evaluation


def fingerprint(
    kind: str = "skill", identifier: str = "weather"
) -> evaluation.Fingerprint:
    return evaluation.Fingerprint(
        type=kind,
        id=identifier,
        name=identifier,
        description="Answer weather questions accurately.",
        version="1.0.0",
        inputs=[evaluation.ParameterSpec(name="query", type="text")],
        outputs=[evaluation.ArtifactSpec(name="answer", type="text")],
        static_data={"documentation": "Return concise current conditions."},
    )


def case(kind: str = "skill", **kwargs: Any) -> evaluation.EvaluationCase:
    values: dict[str, Any] = {
        "fingerprint": fingerprint(kind),
        "message": [
            {"role": "user", "content": "Weather in Shenzhen?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Shenzhen"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_weather", "content": "30"},
            {"role": "user", "content": "Use Celsius."},
            {"role": "assistant", "content": "It is 30 °C."},
        ],
        "success": True,
        "latency": evaluation.Latency(ttft=1_000, e2e=10_000),
        "event_time": datetime(2026, 7, 18, tzinfo=timezone.utc),
    }
    values.update(kwargs)
    return evaluation.EvaluationCase(**values)


def test_agent_and_skill_case_extract_openai_message_values() -> None:
    skill_case = case()
    agent_case = case("agent")

    assert skill_case.input_contents == ["Weather in Shenzhen?", "Use Celsius."]
    assert skill_case.final_output == "It is 30 °C."
    assert agent_case.fingerprint.type == "agent"
    assert evaluation.EvaluationCase(fingerprint=fingerprint()).final_output is None


def test_latency_validates_invalid_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        evaluation.Latency(e2e=-1)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_latency_rejects_non_finite_values(invalid: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        evaluation.Latency(ttft=invalid)
    with pytest.raises(ValueError, match="finite"):
        evaluation.Latency(e2e=invalid)


def test_success_rate_single_and_grouped_results_exclude_na() -> None:
    evaluator = evaluation.SuccessRateEvaluator()
    results = evaluator.evaluate_batch(
        [case(success=True), case(success=False), case(success=None)]
    )

    assert [(result.score, result.level) for result in results] == [
        (1.0, "excellent"),
        (0.0, "fail"),
        (None, "not_applicable"),
    ]
    aggregate = evaluator.new_accumulator().extend(results).aggregate()[0]
    assert aggregate.score == 0.5
    assert aggregate.level == "fail"
    assert aggregate.reason == ""
    assert aggregate.metrics["result_count"] == 2


@pytest.mark.parametrize(
    ("value", "level", "score"),
    [
        (5_000, "excellent", 1.0),
        (5_001, "good", 0.8),
        (10_001, "pass", 0.6),
        (15_001, "fail", 0.0),
    ],
)
def test_realtime_latency_thresholds(value: float, level: str, score: float) -> None:
    evaluator = evaluation.LatencyEvaluator("realtime_interaction")
    result = evaluator.evaluate(case(latency=evaluation.Latency(ttft=value)))

    assert (result.level, result.score) == (level, score)


def test_short_and_long_latency_and_missing_data() -> None:
    short = evaluation.LatencyEvaluator("short_task")
    long = evaluation.LatencyEvaluator("long")

    assert (
        short.evaluate(case(latency=evaluation.Latency(e2e=30_000))).level
        == "excellent"
    )
    assert short.evaluate(case(latency=evaluation.Latency(e2e=90_001))).level == "fail"
    assert (
        long.evaluate(case(latency=evaluation.Latency(e2e=999_999))).level
        == "excellent"
    )
    assert short.evaluate(case(latency=None)).level == "not_applicable"


def test_latency_evaluator_reads_classified_static_scenario() -> None:
    original = fingerprint()
    classified = evaluation.LatencyScenarioClassifier(
        FakeLLM('{"scenario": "realtime_interaction", "reason": "interactive Q&A"}')
    ).apply(original)
    evaluator = evaluation.LatencyEvaluator(scenario=None)
    result = evaluator.evaluate(
        case(
            fingerprint=classified,
            latency=evaluation.Latency(ttft=5_001, e2e=1),
        )
    )

    assert "evaluation" not in original.static_data
    assert result.metrics["scenario"] == "realtime_interaction"
    assert result.level == "good"


def test_latency_scenario_classifier_rejects_invalid_response() -> None:
    classifier = evaluation.LatencyScenarioClassifier(
        FakeLLM('{"scenario": "instant", "reason": "unsupported"}')
    )

    with pytest.raises(ValueError, match="unsupported latency scenario"):
        classifier.classify(fingerprint())


def test_latency_scenario_classifier_rejects_malformed_json() -> None:
    classifier = evaluation.LatencyScenarioClassifier(
        FakeLLM("{'scenario': 'short_task', 'reason': 'not valid JSON'}")
    )

    with pytest.raises(json.JSONDecodeError):
        classifier.classify(fingerprint())


def test_latency_aggregation_reports_both_distributions_and_grades_p95() -> None:
    evaluator = evaluation.LatencyEvaluator("short_task")
    results = [
        evaluator.evaluate(
            case(latency=evaluation.Latency(ttft=first_response, e2e=total))
        )
        for first_response, total in [(100, 10_000), (300, 20_000)]
    ]

    aggregate = evaluator.new_accumulator().extend(results).aggregate()[0]

    assert aggregate.metrics["ttft"] == {
        "avg": 200.0,
        "p95": 290.0,
        "p99": 298.0,
    }
    assert aggregate.metrics["e2e"] == {
        "avg": 15_000.0,
        "p95": 19_500.0,
        "p99": 19_900.0,
    }
    assert aggregate.level == "excellent"
    assert aggregate.reason == ""


def test_latency_aggregation_rejects_missing_scenario_distribution() -> None:
    evaluator = evaluation.LatencyEvaluator("short_task")
    malformed = evaluation.MetricResult(
        metric="latency",
        type="agent",
        id="agent-a",
        version="1.0.0",
        event_time=None,
        score=1.0,
        level="excellent",
        reason="",
        metrics={"scenario": "short_task", "ttft": None, "e2e": None},
    )

    with pytest.raises(ValueError, match="required distribution"):
        evaluator.new_accumulator().add(malformed).aggregate()


class FakeLLM:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.messages: Any = None

    def chat(self, messages: Any, **kwargs: Any) -> list[str]:
        self.messages = messages
        if isinstance(self.response, Exception):
            raise self.response
        return [self.response]

    def generate(self, prompts: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("Scoring evaluators must use chat.")


@pytest.mark.parametrize(
    ("evaluator_type", "score", "level"),
    [
        (evaluation.AccuracyEvaluator, 1, "excellent"),
        (evaluation.AccuracyEvaluator, 0, "fail"),
        (evaluation.CompletenessEvaluator, 1, "excellent"),
        (evaluation.CompletenessEvaluator, 0, "fail"),
    ],
)
def test_llm_metrics_accept_only_binary_scores(
    evaluator_type: type, score: int, level: str
) -> None:
    llm = FakeLLM(f'{{"score": {score}, "reason": "结论正确。"}}')

    result = evaluator_type(llm).evaluate(case())

    assert (result.score, result.level) == (float(score), level)
    assert "static_data" in llm.messages[0]["content"]
    assert "Use Celsius" in llm.messages[0]["content"]
    assert "call_weather" in llm.messages[0]["content"]
    assert '"reference_time": "2026-07-18T00:00:00+00:00"' in llm.messages[0][
        "content"
    ]
    assert '"reference_time_source": "event_time"' in llm.messages[0]["content"]


def test_accuracy_prompt_treats_event_time_as_authoritative() -> None:
    llm = FakeLLM('{"score": 1, "reason": "工具结果支持该回答。"}')

    evaluation.AccuracyEvaluator(llm).evaluate(case())

    prompt = llm.messages[0]["content"]
    assert "reference_time 是本次评估的权威当前时间" in prompt
    assert "即使它晚于模型训练截止时间" in prompt
    assert "不得仅因电影、产品、功能或事件晚于模型训练时间" in prompt
    assert "优先依据工具结果、对话上下文和回答的内部一致性判断" in prompt
    assert "不得仅以模型不知道该信息为由判定 score=0" in prompt
    assert "如果工具结果或上下文与回答矛盾" in prompt


def test_llm_prompt_uses_current_evaluation_time_when_event_time_is_missing() -> None:
    llm = FakeLLM('{"score": 1, "reason": "回答完整。"}')
    before = datetime.now(timezone.utc)

    evaluation.CompletenessEvaluator(llm).evaluate(case(event_time=None))

    after = datetime.now(timezone.utc)
    prompt = llm.messages[0]["content"]
    payload = json.loads(prompt.split("Evaluation data:\n", maxsplit=1)[1])
    reference_time = datetime.fromisoformat(payload["reference_time"])
    assert payload["reference_time_source"] == "evaluation_time"
    assert reference_time.tzinfo is not None
    assert before <= reference_time.astimezone(timezone.utc) <= after


@pytest.mark.parametrize(
    ("evaluator_type", "rubric_terms"),
    [
        (
            evaluation.AccuracyEvaluator,
            (
                "无事实性错误，核心信息准确",
                "完全符合客观事实，无任何事实性错误",
                "逻辑严密，推理严谨，无逻辑谬误",
                "符合行业规范/专业规范，表述专业",
                "新知识/新产品场景",
                "事实核查",
                "逻辑验证",
                "专业规范",
                "新知识处理",
                "只要存在事实性错误即判定为不及格",
            ),
        ),
        (
            evaluation.CompletenessEvaluator,
            (
                "完整执行了用户请求的技能",
                "从识别意图到执行技能的完整过程",
                "声称具备的技能与实际执行能力一致",
                "查询航班、预订服务",
                "仅提供路径规划但未启动导航",
                "订票、导航、预订、支付",
                "功能执行",
                "流程完整性",
                "多步骤任务",
                "技能一致性",
                "重点关注技能描述中明确提到的步骤和要求",
            ),
        ),
    ],
)
def test_llm_metric_prompt_uses_concise_binary_rubric(
    evaluator_type: type, rubric_terms: tuple[str, ...]
) -> None:
    evaluator = evaluator_type(FakeLLM('{"score": 1, "reason": "已完成。"}'))

    result = evaluator.evaluate(case())

    prompt = evaluator.llm.messages[0]["content"]
    assert result.metric == evaluator.metric
    assert hasattr(evaluator, "rubric")
    assert not hasattr(evaluator, "instruction")
    assert all(term in evaluator.rubric for term in rubric_terms)
    assert evaluator.rubric in prompt
    assert "数字 0 或 1" in prompt
    assert "一句简短理由" in prompt
    assert "不要输出思考过程" in prompt
    assert "只返回 JSON" in prompt


@pytest.mark.parametrize(
    "llm",
    [
        None,
        FakeLLM(RuntimeError("offline")),
        FakeLLM("not json"),
        FakeLLM("{score: 1, reason: ok}"),
        FakeLLM('result: {"score": 1, "reason": "ok"}'),
        FakeLLM('{"score": 2, "reason": "bad"}'),
        FakeLLM('{"score": 0.5, "reason": "uncertain"}'),
        FakeLLM('{"score": "1", "reason": "bad type"}'),
        FakeLLM('{"score": true, "reason": "bad type"}'),
        FakeLLM('{"score": 0.9}'),
    ],
)
def test_llm_metric_failures_are_not_applicable(llm: FakeLLM | None) -> None:
    assert evaluation.AccuracyEvaluator(llm).evaluate(case()).level == "not_applicable"


def test_llm_metric_missing_input_or_output_is_not_applicable() -> None:
    evaluator = evaluation.AccuracyEvaluator(FakeLLM('{"score": 1, "reason": "ok"}'))
    no_input = case(message=[{"role": "assistant", "content": "x"}])
    no_output = case(message=[{"role": "user", "content": "x"}])

    assert evaluator.evaluate(no_input).level == "not_applicable"
    assert evaluator.evaluate(no_output).level == "not_applicable"


@pytest.mark.parametrize(
    ("evaluator_type", "passed", "total", "expected_score", "expected_level"),
    [
        (evaluation.AccuracyEvaluator, 8, 10, 0.8, "fail"),
        (evaluation.AccuracyEvaluator, 9, 10, 0.9, "pass"),
        (evaluation.AccuracyEvaluator, 19, 20, 0.95, "good"),
        (evaluation.AccuracyEvaluator, 99, 100, 0.99, "excellent"),
        (evaluation.CompletenessEvaluator, 5, 10, 0.5, "fail"),
        (evaluation.CompletenessEvaluator, 6, 10, 0.6, "pass"),
        (evaluation.CompletenessEvaluator, 8, 10, 0.8, "good"),
        (evaluation.CompletenessEvaluator, 9, 10, 0.9, "excellent"),
    ],
)
def test_binary_metric_aggregation_uses_metric_thresholds(
    evaluator_type: type,
    passed: int,
    total: int,
    expected_score: float,
    expected_level: str,
) -> None:
    evaluator = evaluator_type(FakeLLM('{"score": 1, "reason": "通过。"}'))
    successful = evaluator.evaluate(case())
    evaluator.llm = FakeLLM('{"score": 0, "reason": "未通过。"}')
    failed = evaluator.evaluate(case())
    results = [successful] * passed + [failed] * (total - passed)

    aggregate = evaluator.new_accumulator().extend(results).aggregate()[0]

    assert aggregate.score == pytest.approx(expected_score)
    assert aggregate.level == expected_level
    assert aggregate.reason == ""


@dataclass
class Rule:
    code: str
    severity: evaluation.ComplianceSeverity
    passed: bool = False
    raises: bool = False

    def evaluate(
        self, fingerprint: evaluation.Fingerprint
    ) -> evaluation.AssertionResult:
        if self.raises:
            raise RuntimeError("broken rule")
        return evaluation.AssertionResult(
            code="ignored",
            passed=self.passed,
            severity="hard",
            reason=fingerprint.name,
            evidence=[{"rule": self.code}],
        )


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ([Rule("ok", "hard", True)], (1.0, "excellent")),
        ([Rule("light", "light")], (0.8, "good")),
        ([Rule("major", "major")], (0.6, "pass")),
        ([Rule("hard", "hard")], (0.0, "fail")),
        ([Rule("raises", "light", raises=True)], (0.0, "fail")),
    ],
)
def test_compliance_severity(rules: list[Rule], expected: tuple[float, str]) -> None:
    result = evaluation.ComplianceEvaluator(rules).evaluate(case())
    assert (result.score, result.level) == expected


def test_compliance_without_rules_is_not_applicable() -> None:
    result = evaluation.ComplianceEvaluator().evaluate(case())
    assert result.level == "not_applicable"


def test_compliance_rejects_unknown_severity() -> None:
    rule = Rule("unknown", "warning")
    with pytest.raises(ValueError, match="Unsupported compliance severity"):
        evaluation.ComplianceEvaluator([rule])


def test_compliance_isolates_invalid_rule_result_as_hard_failure() -> None:
    class InvalidRule:
        code = "invalid-result"
        severity = "light"

        def evaluate(self, fingerprint: evaluation.Fingerprint) -> object:
            return object()

    result = evaluation.ComplianceEvaluator([InvalidRule()]).evaluate(case())

    assert (result.score, result.level) == (0.0, "fail")


def test_assertion_result_rejects_unknown_severity() -> None:
    with pytest.raises(ValueError, match="severity"):
        evaluation.AssertionResult(
            code="bad",
            passed=False,
            severity="warning",
        )


def test_result_models_recursively_convert_dates_to_json_safe_values() -> None:
    timestamp = datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc)
    metric = evaluation.MetricResult(
        metric="accuracy",
        type="skill",
        id="weather",
        version="1.0.0",
        event_time=timestamp,
        score=1.0,
        level="excellent",
        reason="ok",
        metrics={"nested": [{"measured_at": timestamp}]},
    )
    window = evaluation.Window(start=timestamp, end=timestamp, label="sample")
    quality = evaluation.QualityResult(
        type="skill",
        id="weather",
        version="1.0.0",
        window=window,
        result_count=1,
        metrics={"accuracy": metric},
        confidence="low",
    )

    payload = quality.to_dict()
    assert payload["metrics"]["accuracy"]["event_time"] == timestamp.isoformat()
    assert (
        payload["metrics"]["accuracy"]["metrics"]["nested"][0]["measured_at"]
        == timestamp.isoformat()
    )
    json.dumps(payload, allow_nan=False)


def test_public_result_models_do_not_expose_assertions_or_evidence() -> None:
    metric = evaluation.MetricResult(
        metric="accuracy",
        type="agent",
        id="weather",
        version="1",
        event_time=None,
        score=1.0,
        level="excellent",
        reason="ok",
    )
    quality = evaluation.QualityResult(
        type="agent",
        id="weather",
        version="1",
        window=None,
        result_count=1,
        metrics={"accuracy": metric},
        confidence="low",
    )

    assert not hasattr(metric, "assertions")
    assert not hasattr(metric, "evidence")
    assert not hasattr(quality, "evidence")
    assert "assertions" not in metric.to_dict()
    assert "evidence" not in metric.to_dict()
    assert metric.to_dict()["reason"] == "ok"
    assert "evidence" not in quality.to_dict()
    assert "reasons" not in quality.to_dict()
    assert "reason" not in quality.to_dict()["metrics"]["accuracy"]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_result_models_reject_non_finite_nested_numbers(invalid: float) -> None:
    result = evaluation.MetricResult(
        metric="accuracy",
        type="skill",
        id="weather",
        version="1.0.0",
        event_time=None,
        score=1.0,
        level="excellent",
        reason="ok",
        metrics={"distribution": [invalid]},
    )
    with pytest.raises(ValueError, match="finite"):
        result.to_dict()


def test_result_models_reject_unknown_nested_objects() -> None:
    assertion = evaluation.AssertionResult(
        code="unknown",
        passed=True,
        evidence=[{"value": object()}],
    )
    with pytest.raises(TypeError, match="not JSON serializable"):
        assertion.to_dict()


def test_compliance_aggregation_keeps_one_static_result() -> None:
    evaluator = evaluation.ComplianceEvaluator([Rule("static", "light")])
    result = evaluator.evaluate(case())

    aggregate = evaluator.new_accumulator().extend([result, result]).aggregate()[0]

    assert aggregate.metrics["result_count"] == 1
    assert aggregate.score == result.score
    assert aggregate.reason == ""
    assert not hasattr(aggregate, "assertions")
    assert not hasattr(aggregate, "evidence")


def standard_suite() -> evaluation.EvaluationSuite:
    score_llm = FakeLLM('{"score": 1, "reason": "ok"}')
    return evaluation.EvaluationSuite(
        [
            evaluation.SuccessRateEvaluator(),
            evaluation.LatencyEvaluator(),
            evaluation.AccuracyEvaluator(score_llm),
            evaluation.CompletenessEvaluator(score_llm),
            evaluation.ComplianceEvaluator([Rule("ok", "hard", True)]),
        ]
    )


def test_suite_batch_and_quality_result_have_no_quality_score() -> None:
    suite = standard_suite()
    batches = suite.evaluate_batch([case(), case(kind="agent")])
    accumulator = suite.new_accumulator(evaluation.Window(label="daily"))
    quality = accumulator.extend(batches).aggregate()

    assert len(batches) == 2
    assert [result.type for result in quality] == ["agent", "skill"]
    assert quality[0].confidence == "low"
    assert set(quality[0].metrics) == {
        "success_rate",
        "latency",
        "accuracy",
        "completeness",
        "compliance",
    }
    assert "quality_score" not in quality[0].to_dict()
    assert quality[0].to_dict()["window"]["label"] == "daily"


def test_all_na_quality_result_has_zero_count_and_no_confidence() -> None:
    suite = evaluation.EvaluationSuite()
    result = suite.evaluate(case(success=None, latency=None, message=[]))
    quality = suite.new_accumulator().add(result).aggregate()[0]

    assert quality.result_count == 0
    assert quality.confidence == "none"
    assert quality.metrics["success_rate"].score is None


def test_suite_requires_atomic_five_metric_results_with_one_identity() -> None:
    with pytest.raises(ValueError, match="exactly the five"):
        evaluation.EvaluationSuite([evaluation.SuccessRateEvaluator()])

    suite = standard_suite()
    skill_results = suite.evaluate(case())
    accumulator = suite.new_accumulator()
    with pytest.raises(ValueError, match="all five metrics"):
        accumulator.add({"success_rate": skill_results["success_rate"]})

    agent_results = suite.evaluate(case(kind="agent"))
    mixed = dict(skill_results)
    mixed["accuracy"] = agent_results["accuracy"]
    with pytest.raises(ValueError, match="one fingerprint identity"):
        accumulator.add(mixed)

    mismatched_metric = dict(skill_results)
    mismatched_metric["accuracy"] = skill_results["completeness"]
    with pytest.raises(ValueError, match="mapping key"):
        accumulator.add(mismatched_metric)

    assert accumulator.aggregate() == []
