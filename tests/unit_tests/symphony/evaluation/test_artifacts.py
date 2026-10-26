from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import jiuwenswarm.symphony.evaluation as evaluation
import jiuwenswarm.symphony.evaluation.artifacts as artifacts


def fingerprint(
    kind: str = "skill", identifier: str = "weather"
) -> evaluation.Fingerprint:
    return evaluation.Fingerprint(
        type=kind,
        id=identifier,
        name=identifier,
        description="A useful capability description.",
        version="1.0.0",
        inputs=[evaluation.ParameterSpec(name="query", type="text")],
        outputs=[evaluation.ArtifactSpec(name="answer", type="text")],
        static_data={"secret_source": "private"},
    )


def quality(
    item: evaluation.Fingerprint,
    score: float = 1.0,
    window: evaluation.Window | None = None,
) -> evaluation.QualityResult:
    metrics = {
        name: evaluation.MetricResult(
            metric=name,
            type=item.type,
            id=item.id,
            version=item.version,
            event_time=datetime(2026, 7, 18, tzinfo=timezone.utc),
            score=score,
            level="excellent" if score == 1 else "pass",
            reason=f"{name} result",
            metrics={
                "result_count": 2,
                "measured_at": datetime(2026, 7, 18),
                "quality_score": 0.5,
            },
        )
        for name in (
            "success_rate",
            "latency",
            "accuracy",
            "completeness",
            "compliance",
        )
    }
    return evaluation.QualityResult(
        type=item.type,
        id=item.id,
        version=item.version,
        window=window,
        result_count=2,
        metrics=metrics,
        confidence="normal",
    )


def test_write_quality_report_creates_public_atomic_artifact(tmp_path) -> None:
    item = fingerprint()
    window = evaluation.Window(label="daily")
    target = evaluation.write_quality_report(
        tmp_path,
        [item],
        [quality(item, window=window)],
        window,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert target == tmp_path / "fingerprints.json"
    assert payload["window"]["label"] == "daily"
    assert len(payload["fingerprints"]) == 1
    written = payload["fingerprints"][0]
    assert "static_data" not in written
    assert "quality_score" not in written
    assert set(written["quality"]) == {
        "success_rate",
        "latency",
        "accuracy",
        "fluency",
        "compliance",
    }
    assert written["quality"]["accuracy"]["name"] == "准确性"
    assert written["quality"]["accuracy"]["result_count"] == 2
    assert "result_count" not in written["quality"]["accuracy"]["metrics"]
    assert "reason" not in written["quality"]["accuracy"]
    assert "assertions" not in written["quality"]["accuracy"]
    assert "evidence" not in written["quality"]["accuracy"]
    assert {name: value["name"] for name, value in written["quality"].items()} == {
        "success_rate": "响应完成率",
        "latency": "响应时延",
        "accuracy": "准确性",
        "fluency": "交互流畅性",
        "compliance": "结构规范性",
    }
    latency = written["quality"]["latency"]
    assert latency["name"] == "响应时延"
    assert "scenario" in latency
    assert set(latency["metrics"]) == {
        "avg_ttft",
        "p95_ttft",
        "p99_ttft",
        "avg_e2e",
        "p95_e2e",
        "p99_e2e",
    }
    assert list(tmp_path.glob(".fingerprints.json.*.tmp")) == []
    assert "quality_score" not in target.read_text(encoding="utf-8")


def test_write_quality_report_closes_descriptor_when_fdopen_fails(
    tmp_path,
    monkeypatch,
) -> None:
    item = fingerprint()
    closed_descriptors = []
    real_close = artifacts.os.close

    def fail_fdopen(*args, **kwargs):
        raise OSError("simulated fdopen failure")

    def close_descriptor(descriptor):
        closed_descriptors.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(artifacts.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(artifacts.os, "close", close_descriptor)

    with pytest.raises(OSError, match="simulated fdopen failure"):
        evaluation.write_quality_report(tmp_path, [item], [quality(item)])

    assert len(closed_descriptors) == 1
    assert list(tmp_path.glob(".fingerprints.json.*.tmp")) == []


def test_write_quality_report_updates_by_composite_key_and_keeps_others(
    tmp_path,
) -> None:
    target = tmp_path / "fingerprints.json"
    target.write_text(
        json.dumps(
            {
                "window": None,
                "fingerprints": [
                    {
                        "type": "agent",
                        "id": "same",
                        "name": "old-agent",
                        "description": "old",
                        "version": "1.0.0",
                        "inputs": [],
                        "outputs": [],
                        "static_data": {"private": True},
                        "quality_score": 0.5,
                        "quality": {
                            "quality_score": 0.5,
                            "unknown_metric": {"score": 1},
                        },
                    },
                    {
                        "type": "skill",
                        "id": "same",
                        "name": "unrelated-skill",
                        "description": "kept",
                        "version": "1.0.0",
                        "inputs": [],
                        "outputs": [],
                        "quality_score": 0.8,
                        "quality": {
                            name: {
                                "name": name,
                                "score": 1,
                                "level": "excellent",
                                "result_count": 1,
                                "reason": "old",
                                "metrics": {},
                            }
                            for name in (
                                "success_rate",
                                "latency",
                                "accuracy",
                                "fluency",
                                "compliance",
                                "unknown_metric",
                            )
                        },
                    },
                    {
                        "type": "agent",
                        "id": "malformed",
                        "name": "malformed",
                        "description": "kept without malformed quality",
                        "version": "1.0.0",
                        "inputs": [],
                        "outputs": [],
                        "quality_score": 0.1,
                        "quality": {"unknown_metric": {"score": 1}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    agent = fingerprint("agent", "same")

    evaluation.write_quality_report(target, [agent], [quality(agent, 0.9)])

    objects = json.loads(target.read_text(encoding="utf-8"))["fingerprints"]
    by_key = {(item["type"], item["id"], item["version"]): item for item in objects}
    assert len(objects) == 3
    assert by_key[("agent", "same", "1.0.0")]["name"] == "same"
    assert by_key[("skill", "same", "1.0.0")]["name"] == "unrelated-skill"
    assert all("static_data" not in item for item in objects)
    serialized = json.dumps(objects)
    assert "quality_score" not in serialized
    assert "quality" not in by_key[("agent", "malformed", "1.0.0")]
    assert set(by_key[("skill", "same", "1.0.0")]["quality"]) == {
        "success_rate",
        "latency",
        "accuracy",
        "fluency",
        "compliance",
    }


def test_quality_without_supplied_fingerprint_is_rejected(tmp_path) -> None:
    item = fingerprint()
    with pytest.raises(ValueError, match="no matching fingerprint"):
        evaluation.write_quality_report(tmp_path, [], [quality(item)])


def test_quality_metric_name_must_match_its_mapping_key(tmp_path) -> None:
    item = fingerprint()
    result = quality(item)
    metrics = dict(result.metrics)
    metrics["completeness"] = metrics["accuracy"]
    invalid = evaluation.QualityResult(
        type=result.type,
        id=result.id,
        version=result.version,
        window=result.window,
        result_count=result.result_count,
        metrics=metrics,
        confidence=result.confidence,
    )

    with pytest.raises(ValueError, match="must match its mapping key"):
        evaluation.write_quality_report(tmp_path, [item], [invalid])


def test_quality_metric_identity_must_match_quality_result(tmp_path) -> None:
    item = fingerprint()
    result = quality(item)
    metrics = dict(result.metrics)
    accuracy = metrics["accuracy"]
    metrics["accuracy"] = evaluation.MetricResult(
        metric=accuracy.metric,
        type=accuracy.type,
        id="another-object",
        version=accuracy.version,
        event_time=accuracy.event_time,
        score=accuracy.score,
        level=accuracy.level,
        reason=accuracy.reason,
        metrics=accuracy.metrics,
    )
    invalid = evaluation.QualityResult(
        type=result.type,
        id=result.id,
        version=result.version,
        window=result.window,
        result_count=result.result_count,
        metrics=metrics,
        confidence=result.confidence,
    )

    with pytest.raises(ValueError, match="must match QualityResult identity"):
        evaluation.write_quality_report(tmp_path, [item], [invalid])


def test_cross_window_update_clears_quality_from_unupdated_objects(tmp_path) -> None:
    first_window = None
    second_window = evaluation.Window(label="second")
    first = fingerprint("agent", "first")
    untouched = fingerprint("skill", "untouched")
    evaluation.write_quality_report(
        tmp_path,
        [first, untouched],
        [
            quality(first, window=first_window),
            quality(untouched, window=first_window),
        ],
    )

    evaluation.write_quality_report(
        tmp_path,
        [first],
        [quality(first, score=0.9, window=second_window)],
    )

    payload = json.loads((tmp_path / "fingerprints.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["fingerprints"]}
    assert payload["window"]["label"] == "second"
    assert "quality" in by_id["first"]
    assert "quality" not in by_id["untouched"]
    assert by_id["untouched"]["description"] == untouched.description


def test_quality_result_windows_must_be_consistent(tmp_path) -> None:
    first = fingerprint("agent", "first")
    second = fingerprint("skill", "second")
    first_window = evaluation.Window(label="first")
    second_window = evaluation.Window(label="second")

    with pytest.raises(ValueError, match="must be consistent"):
        evaluation.write_quality_report(
            tmp_path,
            [first, second],
            [
                quality(first, window=first_window),
                quality(second, window=second_window),
            ],
        )
    with pytest.raises(ValueError, match="explicit window"):
        evaluation.write_quality_report(
            tmp_path,
            [first],
            [quality(first, window=first_window)],
            second_window,
        )
