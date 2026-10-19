"""Execution success-rate evaluator."""

from jiuwenswarm.symphony.evaluation.evaluators.base import BaseEvaluator
from jiuwenswarm.symphony.evaluation.models import (
    EvaluationCase,
    MetricLevel,
    MetricResult,
)


class SuccessRateEvaluator(BaseEvaluator):
    metric = "success_rate"

    def evaluate(self, case: EvaluationCase) -> MetricResult:
        if case.success is None:
            return self.not_applicable(case, "The execution success flag is missing.")
        score = 1.0 if case.success else 0.0
        level: MetricLevel = "excellent" if case.success else "fail"
        reason = "Execution succeeded." if case.success else "Execution failed."
        return self.result(case, score, level, reason)
