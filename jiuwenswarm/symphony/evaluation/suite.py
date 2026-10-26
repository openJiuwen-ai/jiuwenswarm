# -*- coding: utf-8 -*-
"""Multi-metric orchestration and per-object aggregation."""

from __future__ import annotations

from typing import Iterable

from jiuwenswarm.symphony.evaluation.evaluators import (
    AccuracyEvaluator,
    BaseEvaluator,
    CompletenessEvaluator,
    ComplianceEvaluator,
    LatencyEvaluator,
    SuccessRateEvaluator,
)
from jiuwenswarm.symphony.evaluation.models import (
    Confidence,
    EvaluationCase,
    MetricResult,
    QualityResult,
    Window,
)


class EvaluationAccumulator:
    """Aggregate Suite results into one QualityResult per object identity."""

    def __init__(
        self,
        evaluators: Iterable[BaseEvaluator],
        window: Window | None = None,
    ) -> None:
        self.window = window
        self._accumulators = {
            evaluator.metric: evaluator.new_accumulator(window)
            for evaluator in evaluators
        }
        self._identities: set[tuple[str, str, str]] = set()
        self._required_metrics = frozenset(self._accumulators)

    def add(self, results: dict[str, MetricResult]) -> "EvaluationAccumulator":
        if frozenset(results) != self._required_metrics:
            raise ValueError(
                "Evaluation results must contain all five metrics exactly once."
            )
        identities = {
            (result.type, result.id, result.version) for result in results.values()
        }
        if len(identities) != 1:
            raise ValueError("Evaluation results must share one fingerprint identity.")
        if any(result.metric != metric for metric, result in results.items()):
            raise ValueError("Each result must match its metric mapping key.")
        for metric, result in results.items():
            self._accumulators[metric].add(result)
        self._identities.update(identities)
        return self

    def extend(
        self, batches: Iterable[dict[str, MetricResult]]
    ) -> "EvaluationAccumulator":
        for results in batches:
            self.add(results)
        return self

    def aggregate(self) -> list[QualityResult]:
        by_metric: dict[str, dict[tuple[str, str, str], MetricResult]] = {}
        for metric, accumulator in self._accumulators.items():
            by_metric[metric] = {
                (result.type, result.id, result.version): result
                for result in accumulator.aggregate()
            }
        quality_results: list[QualityResult] = []
        for identity in sorted(self._identities):
            metrics = {
                metric: grouped[identity]
                for metric, grouped in by_metric.items()
                if identity in grouped
            }
            counts = [
                int(result.metrics.get("result_count", 0))
                for result in metrics.values()
            ]
            result_count = max(counts, default=0)
            confidence: Confidence
            if result_count == 0:
                confidence = "none"
            elif result_count == 1:
                confidence = "low"
            else:
                confidence = "normal"
            quality_results.append(
                QualityResult(
                    type=identity[0],
                    id=identity[1],
                    version=identity[2],
                    window=self.window,
                    result_count=result_count,
                    metrics=metrics,
                    confidence=confidence,
                )
            )
        return quality_results


class EvaluationSuite:
    """Evaluate the fixed five-dimensional quality model."""

    def __init__(self, evaluators: Iterable[BaseEvaluator] | None = None) -> None:
        selected = (
            list(evaluators)
            if evaluators is not None
            else [
                SuccessRateEvaluator(),
                LatencyEvaluator(),
                AccuracyEvaluator(),
                CompletenessEvaluator(),
                ComplianceEvaluator(),
            ]
        )
        metrics = [evaluator.metric for evaluator in selected]
        required_metrics = {
            "success_rate",
            "latency",
            "accuracy",
            "completeness",
            "compliance",
        }
        if len(metrics) != len(set(metrics)) or set(metrics) != required_metrics:
            raise ValueError(
                "EvaluationSuite requires exactly the five standard metrics."
            )
        self.evaluators = selected

    def evaluate(self, case: EvaluationCase) -> dict[str, MetricResult]:
        return {
            evaluator.metric: evaluator.evaluate(case) for evaluator in self.evaluators
        }

    def evaluate_batch(
        self, cases: Iterable[EvaluationCase]
    ) -> list[dict[str, MetricResult]]:
        return [self.evaluate(case) for case in cases]

    def new_accumulator(self, window: Window | None = None) -> EvaluationAccumulator:
        return EvaluationAccumulator(self.evaluators, window)
