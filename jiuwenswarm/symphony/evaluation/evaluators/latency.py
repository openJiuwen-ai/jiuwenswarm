"""Latency scenario classification, evaluation, and aggregation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeAlias

from jiuwenswarm.symphony.evaluation.aggregation import MetricAccumulator
from jiuwenswarm.symphony.evaluation.evaluators.base import BaseEvaluator
from jiuwenswarm.symphony.evaluation.interfaces import EvaluationLLM
from jiuwenswarm.symphony.evaluation.models import (
    EvaluationCase,
    MetricLevel,
    MetricResult,
    Window,
)
from jiuwenswarm.symphony.fingerprint.models import Fingerprint


LatencyScenario: TypeAlias = Literal[
    "realtime_interaction",
    "short_task",
    "long",
]
LATENCY_SCENARIOS = {
    "realtime_interaction",
    "short_task",
    "long",
}


@dataclass(frozen=True)
class LatencyScenarioResult:
    """Latency scenario inferred once from one static fingerprint."""

    scenario: LatencyScenario
    reason: str


class LatencyScenarioClassifier:
    """Use an LLM to classify latency expectations from static data."""

    def __init__(self, llm: EvaluationLLM) -> None:
        self.llm = llm

    def classify(self, fingerprint: Fingerprint) -> LatencyScenarioResult:
        response = self.llm.chat(
            [{"role": "user", "content": self._prompt(fingerprint)}]
        )
        payload = self._parse(response)
        scenario = payload.get("scenario")
        reason = payload.get("reason")
        if scenario not in LATENCY_SCENARIOS:
            raise ValueError("Model returned an unsupported latency scenario.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Model response must contain a latency scenario reason.")
        return LatencyScenarioResult(scenario=scenario, reason=reason.strip())

    def apply(self, fingerprint: Fingerprint) -> Fingerprint:
        """Return a fingerprint enriched with its static scenario result."""

        result = self.classify(fingerprint)
        static_data = dict(fingerprint.static_data)
        evaluation_data = static_data.get("evaluation", {})
        evaluation_data = (
            dict(evaluation_data) if isinstance(evaluation_data, dict) else {}
        )
        evaluation_data.update(
            {
                "latency_scenario": result.scenario,
                "latency_scenario_reason": result.reason,
            }
        )
        static_data["evaluation"] = evaluation_data
        return replace(fingerprint, static_data=static_data)

    @staticmethod
    def _parse(response: list[str]) -> dict[str, Any]:
        if not response or not isinstance(response[0], str):
            raise ValueError("Model returned no latency scenario response.")
        payload = json.loads(response[0])
        if not isinstance(payload, dict):
            raise ValueError("Latency scenario response must be a JSON object.")
        return payload

    @staticmethod
    def _prompt(fingerprint: Fingerprint) -> str:
        static_payload = {
            "type": fingerprint.type,
            "name": fingerprint.name,
            "description": fingerprint.description,
        }
        return (
            "Classify the latency scenario for this Agent or Skill from static data.\n"
            "Choose exactly one scenario:\n"
            "- realtime_interaction: Q&A or interactive calls requiring an immediate "
            "first response.\n"
            "- short_task: creation or productivity work expected to finish within "
            "a short period.\n"
            "- long: asynchronous research, document, audio, or video work with long "
            "execution time.\n"
            "Return only JSON with scenario and reason.\n"
            f"Static data:\n{json.dumps(static_payload, ensure_ascii=False, default=str)}"
        )


class LatencyEvaluator(BaseEvaluator):
    metric = "latency"

    def __init__(
        self,
        scenario: LatencyScenario | None = "short_task",
        thresholds_ms: tuple[float, float, float] | None = None,
    ) -> None:
        if scenario is not None and scenario not in LATENCY_SCENARIOS:
            raise ValueError(f"Unsupported latency scenario: {scenario}")
        if scenario is None and thresholds_ms is not None:
            raise ValueError(
                "Custom latency thresholds require one fixed latency scenario."
            )
        self.scenario = scenario
        self.thresholds_ms = thresholds_ms
        configured_thresholds = thresholds_ms or (
            self._default_thresholds(scenario) if scenario is not None else None
        )
        if configured_thresholds is not None and any(
            value < 0 for value in configured_thresholds
        ):
            raise ValueError("Latency thresholds must be non-negative.")
        if configured_thresholds is not None and (
            tuple(sorted(configured_thresholds)) != configured_thresholds
        ):
            raise ValueError("Latency thresholds must be ascending.")

    @staticmethod
    def _default_thresholds(
        scenario: LatencyScenario,
    ) -> tuple[float, float, float]:
        if scenario == "realtime_interaction":
            return (5_000.0, 10_000.0, 15_000.0)
        return (30_000.0, 60_000.0, 90_000.0)

    def _scenario(self, case: EvaluationCase) -> LatencyScenario | None:
        if self.scenario is not None:
            return self.scenario
        evaluation_data = case.fingerprint.static_data.get("evaluation", {})
        if not isinstance(evaluation_data, dict):
            return None
        scenario = evaluation_data.get("latency_scenario")
        return scenario if scenario in LATENCY_SCENARIOS else None

    def _value(
        self,
        case: EvaluationCase,
        scenario: LatencyScenario,
    ) -> float | None:
        if case.latency is None:
            return None
        if scenario == "realtime_interaction":
            return case.latency.ttft
        return case.latency.e2e

    def level_for(self, value: float, scenario: LatencyScenario) -> MetricLevel:
        if scenario == "long":
            return "excellent"
        thresholds = self.thresholds_ms or self._default_thresholds(scenario)
        excellent, good, passing = thresholds
        if value <= excellent:
            return "excellent"
        if value <= good:
            return "good"
        if value <= passing:
            return "pass"
        return "fail"

    def evaluate(self, case: EvaluationCase) -> MetricResult:
        scenario = self._scenario(case)
        if scenario is None:
            return self.not_applicable(
                case,
                "The fingerprint has no classified latency scenario.",
            )
        value = self._value(case, scenario)
        if value is None:
            return self.not_applicable(case, "Required latency data is missing.")
        level = self.level_for(value, scenario)
        score = {"excellent": 1.0, "good": 0.8, "pass": 0.6, "fail": 0.0}[level]
        latency = case.latency
        if latency is None:
            return self.not_applicable(case, "Required latency data is missing.")
        return self.result(
            case,
            score,
            level,
            f"{scenario} latency is {value:g} ms.",
            metrics={
                "scenario": scenario,
                "ttft": latency.ttft,
                "e2e": latency.e2e,
            },
        )

    def new_accumulator(self, window: Window | None = None) -> MetricAccumulator:
        return LatencyAccumulator(self, window)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class LatencyAccumulator(MetricAccumulator):
    """Aggregate latency distributions while grading the scenario p95."""

    def __init__(self, evaluator: LatencyEvaluator, window: Window | None) -> None:
        super().__init__(
            evaluator.metric,
            window,
            aggregation_thresholds=evaluator.aggregation_thresholds,
        )
        self.evaluator = evaluator

    def _aggregate_group(self, results: list[MetricResult]) -> MetricResult:
        applicable = [result for result in results if result.score is not None]
        template = results[0]
        if not applicable:
            return super()._aggregate_group(results)

        def distribution(name: str) -> dict[str, float] | None:
            values = [
                float(result.metrics[name])
                for result in applicable
                if result.metrics.get(name) is not None
            ]
            if not values:
                return None
            return {
                "avg": sum(values) / len(values),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
            }

        ttft = distribution("ttft")
        e2e = distribution("e2e")
        scenarios = {result.metrics.get("scenario") for result in applicable}
        if len(scenarios) != 1:
            raise ValueError("One fingerprint version has multiple latency scenarios.")
        scenario = scenarios.pop()
        if scenario not in LATENCY_SCENARIOS:
            raise ValueError("Latency result has an unsupported scenario.")
        selected = ttft if scenario == "realtime_interaction" else e2e
        if selected is None:
            raise ValueError("Latency results are missing the required distribution.")
        level = self.evaluator.level_for(selected["p95"], scenario)
        score = {"excellent": 1.0, "good": 0.8, "pass": 0.6, "fail": 0.0}[level]
        return MetricResult(
            metric=self.metric,
            type=template.type,
            id=template.id,
            version=template.version,
            event_time=None,
            score=score,
            level=level,
            reason="",
            metrics={
                "scenario": scenario,
                "result_count": len(applicable),
                "ttft": ttft,
                "e2e": e2e,
            },
        )
