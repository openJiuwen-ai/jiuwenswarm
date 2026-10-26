# -*- coding: utf-8 -*-
"""Metric grading and result aggregation primitives."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from jiuwenswarm.symphony.evaluation.models import (
    MetricLevel,
    MetricResult,
    Window,
)


def score_level(score: float, thresholds: tuple[float, float, float]) -> MetricLevel:
    """Map ascending pass/good/excellent thresholds to a level."""

    pass_threshold, good_threshold, excellent_threshold = thresholds
    if score >= excellent_threshold:
        return "excellent"
    if score >= good_threshold:
        return "good"
    if score >= pass_threshold:
        return "pass"
    return "fail"


class MetricAccumulator:
    """Aggregate one metric, grouped by fingerprint identity."""

    def __init__(
        self,
        metric: str,
        window: Window | None = None,
        aggregation_thresholds: tuple[float, float, float] = (0.6, 0.8, 0.9),
    ) -> None:
        self.metric = metric
        self.window = window
        self.aggregation_thresholds = aggregation_thresholds
        self._results: list[MetricResult] = []

    def add(self, result: MetricResult) -> "MetricAccumulator":
        if result.metric != self.metric:
            raise ValueError(f"Expected metric {self.metric!r}, got {result.metric!r}.")
        self._results.append(result)
        return self

    def extend(self, results: Iterable[MetricResult]) -> "MetricAccumulator":
        for result in results:
            self.add(result)
        return self

    def aggregate(self) -> list[MetricResult]:
        grouped: dict[tuple[str, str, str], list[MetricResult]] = defaultdict(list)
        for result in self._results:
            grouped[(result.type, result.id, result.version)].append(result)
        return [
            self._aggregate_group(results)
            for _, results in sorted(grouped.items(), key=lambda item: item[0])
        ]

    def _aggregate_group(self, results: list[MetricResult]) -> MetricResult:
        applicable = [result for result in results if result.score is not None]
        template = results[0]
        if not applicable:
            return MetricResult(
                metric=self.metric,
                type=template.type,
                id=template.id,
                version=template.version,
                event_time=None,
                score=None,
                level="not_applicable",
                reason="",
                metrics={"result_count": 0},
            )
        score = sum(result.score or 0 for result in applicable) / len(applicable)
        if self.metric == "success_rate":
            level: MetricLevel = "excellent" if score >= 0.999 else "fail"
        else:
            level = score_level(score, self.aggregation_thresholds)
        metrics = self._aggregate_metrics(applicable)
        metrics["result_count"] = len(applicable)
        return MetricResult(
            metric=self.metric,
            type=template.type,
            id=template.id,
            version=template.version,
            event_time=None,
            score=score,
            level=level,
            reason="",
            metrics=metrics,
        )

    @staticmethod
    def _aggregate_metrics(results: list[MetricResult]) -> dict[str, Any]:
        return {}
