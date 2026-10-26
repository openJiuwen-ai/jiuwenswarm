# -*- coding: utf-8 -*-
"""Persist evaluation quality alongside public fingerprint fields."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.symphony.evaluation.models import (
    QualityResult,
    Window,
    to_json_safe,
)
from jiuwenswarm.symphony.fingerprint.models import Fingerprint


CORE_METRICS = (
    "success_rate",
    "latency",
    "accuracy",
    "completeness",
    "compliance",
)
REPORT_METRIC_BY_CORE = {
    "success_rate": "success_rate",
    "latency": "latency",
    "accuracy": "accuracy",
    "completeness": "fluency",
    "compliance": "compliance",
}
REPORT_METRICS = tuple(REPORT_METRIC_BY_CORE.values())
METRIC_NAMES = {
    "success_rate": "响应完成率",
    "latency": "响应时延",
    "accuracy": "准确性",
    "fluency": "交互流畅性",
    "compliance": "结构规范性",
}


def _identity(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("type", "")),
        str(item.get("id", "")),
        str(item.get("version", "")),
    )


def _public_fingerprint(fingerprint: Fingerprint) -> dict[str, Any]:
    return {
        "type": fingerprint.type,
        "id": fingerprint.id,
        "name": fingerprint.name,
        "description": fingerprint.description,
        "version": fingerprint.version,
        "inputs": [item.to_dict() for item in fingerprint.inputs],
        "outputs": [item.to_dict() for item in fingerprint.outputs],
    }


def _distribution(value: Any) -> tuple[Any, Any, Any]:
    if isinstance(value, dict):
        return value.get("avg"), value.get("p95"), value.get("p99")
    return value, value, value


def _latency_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    flat_names = (
        "avg_ttft",
        "p95_ttft",
        "p99_ttft",
        "avg_e2e",
        "p95_e2e",
        "p99_e2e",
    )
    if any(name in metrics for name in flat_names):
        return {name: metrics.get(name) for name in flat_names}
    ttft_avg, ttft_p95, ttft_p99 = _distribution(metrics.get("ttft"))
    e2e_avg, e2e_p95, e2e_p99 = _distribution(metrics.get("e2e"))
    return {
        "avg_ttft": ttft_avg,
        "p95_ttft": ttft_p95,
        "p99_ttft": ttft_p99,
        "avg_e2e": e2e_avg,
        "p95_e2e": e2e_p95,
        "p99_e2e": e2e_p99,
    }


def _quality_payload(result: QualityResult) -> dict[str, Any]:
    if set(result.metrics) != set(CORE_METRICS):
        raise ValueError("QualityResult must contain the five standard metrics.")
    result_identity = (result.type, result.id, result.version)
    metrics: dict[str, Any] = {}
    for core_name in CORE_METRICS:
        report_name = REPORT_METRIC_BY_CORE[core_name]
        metric = result.metrics[core_name]
        if metric.metric != core_name:
            raise ValueError(
                f"MetricResult metric {metric.metric!r} must match its "
                f"mapping key {core_name!r}."
            )
        metric_identity = (metric.type, metric.id, metric.version)
        if metric_identity != result_identity:
            raise ValueError(
                "MetricResult identity must match QualityResult identity: "
                f"{metric_identity!r} != {result_identity!r}."
            )
        metric_details = dict(metric.metrics)
        metric_details.pop("result_count", None)
        metrics[report_name] = {
            "name": METRIC_NAMES[report_name],
            "score": metric.score,
            "level": metric.level,
            "result_count": int(
                metric.metrics.get("result_count", result.result_count)
            ),
            "metrics": (
                _latency_metrics(metric_details)
                if core_name == "latency"
                else metric_details
            ),
        }
        if core_name == "latency":
            metrics[report_name]["scenario"] = metric.metrics.get("scenario")
    return metrics


def _clean_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        excluded = {
            "quality_score",
            "static_data",
            "assertions",
            "evidence",
        }
        cleaned = {}
        for key, item in value.items():
            if key not in excluded:
                cleaned[key] = _clean_mapping(item)
        return cleaned
    if isinstance(value, list):
        return [_clean_mapping(item) for item in value]
    return value


def _sanitize_existing_metric(name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {"score", "level", "result_count"}
    if not required <= set(value):
        return None
    metric_details = _clean_mapping(value.get("metrics", {}))
    if isinstance(metric_details, dict):
        metric_details.pop("result_count", None)
    cleaned = {
        "name": METRIC_NAMES[name],
        "score": value["score"],
        "level": value["level"],
        "result_count": value["result_count"],
        "metrics": metric_details,
    }
    if name == "latency":
        raw_metrics = value.get("metrics", {})
        fallback_scenario = (
            raw_metrics.get("scenario") if isinstance(raw_metrics, dict) else None
        )
        cleaned["scenario"] = value.get("scenario", fallback_scenario)
        cleaned["metrics"] = _latency_metrics(cleaned["metrics"])
    return cleaned


def _sanitize_existing_quality(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get("metrics", value)
    if not isinstance(candidate, dict) or not set(REPORT_METRICS) <= set(candidate):
        return None
    cleaned = {
        name: _sanitize_existing_metric(name, candidate[name])
        for name in REPORT_METRICS
    }
    if any(item is None for item in cleaned.values()):
        return None
    return cleaned


def _sanitize_existing(item: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}
    public_fields = (
        "type",
        "id",
        "name",
        "description",
        "version",
        "inputs",
        "outputs",
    )
    for key in public_fields:
        if key in item:
            cleaned[key] = item[key]
    quality = _sanitize_existing_quality(item.get("quality"))
    if quality is not None:
        cleaned["quality"] = quality
    return _clean_mapping(cleaned)


def _effective_window(
    results: list[QualityResult], explicit: Window | None
) -> Window | None:
    if explicit is not None:
        if any(result.window != explicit for result in results):
            raise ValueError("QualityResult windows must match the explicit window.")
        return explicit
    if not results:
        return None
    inferred = results[0].window
    if any(result.window != inferred for result in results[1:]):
        raise ValueError("QualityResult windows must be consistent.")
    return inferred


def _window_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("fingerprints.json window must be an object or null.")
    return {
        "start": value.get("start"),
        "end": value.get("end"),
        "label": value.get("label"),
    }


def write_quality_report(
    path: str | Path,
    fingerprints: Iterable[Fingerprint],
    quality_results: Iterable[QualityResult],
    window: Window | None = None,
) -> Path:
    """Atomically merge quality results into a public ``fingerprints.json``.

    Objects are joined by ``(type, id, version)``. Existing unrelated objects
    remain in the report, while evaluation-only private static data is omitted.
    """

    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target / "fingerprints.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    supplied = {
        (item.type, item.id, item.version): _public_fingerprint(item)
        for item in fingerprints
    }
    quality_items = list(quality_results)
    effective_window = _effective_window(quality_items, window)
    effective_window_payload = effective_window.to_dict() if effective_window else None
    quality_by_identity = {
        (item.type, item.id, item.version): item for item in quality_items
    }
    missing = sorted(set(quality_by_identity) - set(supplied))
    if missing:
        raise ValueError(
            "Quality results have no matching fingerprint: "
            + ", ".join("/".join(identity) for identity in missing)
        )

    existing: list[dict[str, Any]] = []
    existing_window_payload: dict[str, Any] | None = None
    if target.exists():
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("fingerprints.json must contain a JSON object.")
        existing_window_payload = _window_payload(payload.get("window"))
        raw_fingerprints = payload.get("fingerprints", [])
        if not isinstance(raw_fingerprints, list):
            raise ValueError("fingerprints.json must contain a fingerprints list.")
        existing = [
            _sanitize_existing(item)
            for item in raw_fingerprints
            if isinstance(item, dict)
        ]
        if existing_window_payload != effective_window_payload:
            for item in existing:
                item.pop("quality", None)

    merged = {_identity(item): item for item in existing}
    for identity, item in supplied.items():
        previous_quality = merged.get(identity, {}).get("quality")
        merged[identity] = item
        if identity in quality_by_identity:
            merged[identity]["quality"] = _quality_payload(
                quality_by_identity[identity]
            )
        elif previous_quality is not None:
            merged[identity]["quality"] = previous_quality

    report = to_json_safe(
        _clean_mapping(
            {
                "window": effective_window_payload,
                "fingerprints": [merged[key] for key in sorted(merged)],
            }
        )
    )
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary_name)
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = None
        with stream:
            json.dump(
                report,
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target
