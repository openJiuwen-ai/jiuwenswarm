# -*- coding: utf-8 -*-
"""Public data contracts for Agent and Skill evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, TypeAlias

from openai.types.chat import ChatCompletionMessageParam

from jiuwenswarm.symphony.fingerprint.models import Fingerprint


MetricLevel: TypeAlias = Literal["excellent", "good", "pass", "fail", "not_applicable"]
Confidence: TypeAlias = Literal["none", "low", "normal"]
ComplianceSeverity: TypeAlias = Literal["light", "major", "hard"]


def to_json_safe(value: Any) -> Any:
    """Recursively convert supported values to strict JSON-compatible data."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite.")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings.")
            converted[key] = to_json_safe(item)
        return converted
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable.")


@dataclass(frozen=True)
class Latency:
    """Execution latency summary; TTFT and E2E values use milliseconds."""

    ttft: float | None = None
    e2e: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("ttft", self.ttft),
            ("e2e", self.e2e),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"Latency {name} must be finite and non-negative.")


@dataclass(frozen=True)
class EvaluationCase:
    """A complete execution sample and the evaluated static fingerprint."""

    fingerprint: Fingerprint
    message: list[ChatCompletionMessageParam] = field(default_factory=list)
    success: bool | None = None
    latency: Latency | None = None
    event_time: datetime | None = None

    @property
    def input_contents(self) -> list[Any]:
        return [
            item.get("content")
            for item in self.message
            if item.get("role") == "user" and item.get("content") is not None
        ]

    @property
    def final_output(self) -> Any | None:
        for item in reversed(self.message):
            if item.get("role") == "assistant":
                return item.get("content")
        return None


@dataclass(frozen=True)
class AssertionResult:
    """A deterministic assertion produced by a compliance rule."""

    code: str
    passed: bool
    severity: ComplianceSeverity = "hard"
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.severity not in {"light", "major", "hard"}:
            raise ValueError(
                "AssertionResult severity must be 'light', 'major', or 'hard'."
            )

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(
            {
                "code": self.code,
                "passed": self.passed,
                "severity": self.severity,
                "reason": self.reason,
                "evidence": self.evidence,
            }
        )


@dataclass(frozen=True)
class MetricResult:
    """Result returned by one independently deployable metric evaluator."""

    metric: str
    type: str
    id: str
    version: str
    event_time: datetime | None
    score: float | None
    level: MetricLevel
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError("MetricResult score must be between 0 and 1.")
        if self.score is None and self.level != "not_applicable":
            raise ValueError("A missing score must use level 'not_applicable'.")

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe(
            {
                "metric": self.metric,
                "type": self.type,
                "id": self.id,
                "version": self.version,
                "event_time": self.event_time,
                "score": self.score,
                "level": self.level,
                "reason": self.reason,
                "metrics": self.metrics,
            }
        )


@dataclass(frozen=True)
class Window:
    """Optional aggregation window metadata controlled by the caller."""

    start: datetime | None = None
    end: datetime | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_json_safe({"start": self.start, "end": self.end, "label": self.label})


@dataclass(frozen=True)
class QualityResult:
    """Per-object window aggregation without an object-level quality score."""

    type: str
    id: str
    version: str
    window: Window | None
    result_count: int
    metrics: dict[str, MetricResult]
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        metrics = {}
        for name, result in self.metrics.items():
            payload = result.to_dict()
            payload.pop("reason", None)
            metrics[name] = payload
        return to_json_safe(
            {
                "type": self.type,
                "id": self.id,
                "version": self.version,
                "window": self.window.to_dict() if self.window else None,
                "result_count": self.result_count,
                "metrics": metrics,
                "confidence": self.confidence,
            }
        )
