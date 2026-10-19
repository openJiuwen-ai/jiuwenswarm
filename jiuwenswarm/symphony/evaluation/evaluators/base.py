# -*- coding: utf-8 -*-
"""Common evaluator behavior, including shared LLM scoring."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from jiuwenswarm.symphony.evaluation.aggregation import (
    MetricAccumulator,
    score_level,
)
from jiuwenswarm.symphony.evaluation.interfaces import EvaluationLLM
from jiuwenswarm.symphony.evaluation.models import (
    EvaluationCase,
    MetricLevel,
    MetricResult,
    Window,
)


class BaseEvaluator(ABC):
    """Common synchronous interface implemented by every metric evaluator."""

    metric: str
    aggregation_thresholds: tuple[float, float, float] = (0.6, 0.8, 0.9)

    @abstractmethod
    def evaluate(self, case: EvaluationCase) -> MetricResult:
        """Evaluate one complete execution case."""

    def evaluate_batch(self, cases: Iterable[EvaluationCase]) -> list[MetricResult]:
        return [self.evaluate(case) for case in cases]

    def new_accumulator(self, window: Window | None = None) -> MetricAccumulator:
        return MetricAccumulator(
            self.metric,
            window,
            aggregation_thresholds=self.aggregation_thresholds,
        )

    def result(
        self,
        case: EvaluationCase,
        score: float | None,
        level: MetricLevel,
        reason: str,
        metrics: dict[str, Any] | None = None,
    ) -> MetricResult:
        return MetricResult(
            metric=self.metric,
            type=case.fingerprint.type,
            id=case.fingerprint.id,
            version=case.fingerprint.version,
            event_time=case.event_time,
            score=score,
            level=level,
            reason=reason,
            metrics=dict(metrics or {}),
        )

    def not_applicable(self, case: EvaluationCase, reason: str) -> MetricResult:
        return self.result(case, None, "not_applicable", reason)


class LLMScoreEvaluator(BaseEvaluator):
    """Shared fail-soft scoring flow for LLM-based metrics."""

    thresholds: tuple[float, float, float]
    rubric: str

    def __init__(self, llm: EvaluationLLM | None = None) -> None:
        self.llm = llm

    def evaluate(self, case: EvaluationCase) -> MetricResult:
        if self.llm is None:
            return self.not_applicable(case, "An evaluation LLM is not configured.")
        if not case.input_contents:
            return self.not_applicable(case, "The message has no user input.")
        if case.final_output is None:
            return self.not_applicable(case, "The message has no assistant output.")
        try:
            responses = self.llm.chat([{"role": "user", "content": self._prompt(case)}])
            payload = self._parse(responses)
        except Exception as exc:
            return self.not_applicable(case, f"LLM evaluation failed: {exc}")
        score = payload.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return self.not_applicable(case, "LLM response has no numeric score.")
        score = float(score)
        if score not in {0.0, 1.0}:
            return self.not_applicable(case, "LLM score must be either 0 or 1.")
        reason = payload.get("reason")
        if not isinstance(reason, str):
            return self.not_applicable(case, "LLM response has no textual reason.")
        return self.result(case, score, score_level(score, self.thresholds), reason)

    def _prompt(self, case: EvaluationCase) -> str:
        if case.event_time is None:
            reference_time = datetime.now(timezone.utc)
            reference_time_source = "evaluation_time"
        else:
            reference_time = case.event_time
            reference_time_source = "event_time"
        payload = {
            "fingerprint": case.fingerprint.to_dict(),
            "message": case.message,
            "reference_time": reference_time.isoformat(),
            "reference_time_source": reference_time_source,
        }
        return (
            f"{self.rubric}\n\n"
            "输出要求：score 只能是数字 0 或 1；reason 只写一句简短理由，"
            "指出决定性证据，不复述评分标准，不虚构依据。不要输出思考过程，"
            "只返回 JSON：{\"score\": 0 或 1, \"reason\": \"理由\"}。\n"
            f"Evaluation data:\n{json.dumps(payload, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _parse(responses: Sequence[str]) -> dict[str, Any]:
        if not responses or not isinstance(responses[0], str):
            raise ValueError("LLM returned no response.")
        payload = json.loads(responses[0])
        if not isinstance(payload, dict):
            raise ValueError("LLM response is not a JSON object.")
        return payload
