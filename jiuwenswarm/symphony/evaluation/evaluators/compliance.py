"""Deterministic static compliance evaluator and aggregation."""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Protocol

from jiuwenswarm.symphony.evaluation.aggregation import MetricAccumulator
from jiuwenswarm.symphony.evaluation.evaluators.base import BaseEvaluator
from jiuwenswarm.symphony.evaluation.models import (
    AssertionResult,
    ComplianceSeverity,
    EvaluationCase,
    MetricResult,
    Window,
)
from jiuwenswarm.symphony.fingerprint.models import Fingerprint


class ComplianceRule(Protocol):
    """Deterministic static compliance rule supplied by a business adapter."""

    code: str
    severity: ComplianceSeverity

    def evaluate(self, fingerprint: Fingerprint) -> AssertionResult:
        """Evaluate one fingerprint against this rule."""
        ...


class ComplianceEvaluator(BaseEvaluator):
    metric = "compliance"

    def __init__(self, rules: Iterable[ComplianceRule] = ()) -> None:
        self.rules = list(rules)
        for rule in self.rules:
            if rule.severity not in {"light", "major", "hard"}:
                raise ValueError(
                    f"Unsupported compliance severity for rule {rule.code!r}: "
                    f"{rule.severity!r}"
                )

    def evaluate(self, case: EvaluationCase) -> MetricResult:
        if not self.rules:
            return self.not_applicable(case, "No compliance rules are configured.")
        assertions: list[AssertionResult] = []
        for rule in self.rules:
            try:
                severity = rule.severity
                if severity not in {"light", "major", "hard"}:
                    raise ValueError(f"Unsupported compliance severity: {severity!r}")
                evaluated = rule.evaluate(case.fingerprint)
                if not isinstance(evaluated, AssertionResult):
                    raise TypeError("Compliance rule must return AssertionResult.")
                assertion = replace(
                    evaluated,
                    code=rule.code,
                    severity=severity,
                )
            except Exception as exc:
                assertion = AssertionResult(
                    code=rule.code,
                    passed=False,
                    severity="hard",
                    reason=f"Compliance rule raised an exception: {exc}",
                )
            assertions.append(assertion)
        failures = [assertion for assertion in assertions if not assertion.passed]
        if any(item.severity == "hard" for item in failures):
            score, level = 0.0, "fail"
        elif any(item.severity == "major" for item in failures):
            score, level = 0.6, "pass"
        elif failures:
            score, level = 0.8, "good"
        else:
            score, level = 1.0, "excellent"
        reason = (
            "All compliance rules passed."
            if not failures
            else f"{len(failures)} compliance rule(s) failed."
        )
        return self.result(case, score, level, reason)

    def new_accumulator(self, window: Window | None = None) -> MetricAccumulator:
        return ComplianceAccumulator(self.metric, window)


class ComplianceAccumulator(MetricAccumulator):
    """Keep one static compliance result for each fingerprint version."""

    def _aggregate_group(self, results: list[MetricResult]) -> MetricResult:
        applicable = [result for result in results if result.score is not None]
        if not applicable:
            return super()._aggregate_group(results)
        result = applicable[0]
        metrics = dict(result.metrics)
        metrics["result_count"] = 1
        return MetricResult(
            metric=result.metric,
            type=result.type,
            id=result.id,
            version=result.version,
            event_time=None,
            score=result.score,
            level=result.level,
            reason="",
            metrics=metrics,
        )
