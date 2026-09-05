# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for integrity validation rules."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.research_integrity.artifact_tracker import record_artifact
from jiuwenswarm.research_integrity.schemas import (
    ArtifactRecord,
    ExperimentRun,
    ExperimentSpec,
    MetricRecord,
)
from jiuwenswarm.research_integrity.validator import (
    ARTIFACT_HASH_MISMATCH,
    FAILED_RUN,
    MISSING_ARTIFACT,
    MISSING_METRIC,
    MISSING_METRIC_SOURCE,
    MISSING_SEED,
    NAN_METRIC,
    SEED_MISMATCH,
    validate_integrity,
)


def _spec(tmp_path: Path, *, seed: int | None = 42) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_v",
        name="v",
        command="python v.py",
        cwd=str(tmp_path),
        seed=seed,
        expected_artifacts=["results.json"],
        metric_specs=[],
    )


def _run(tmp_path: Path, *, exit_code: int = 0) -> ExperimentRun:
    return ExperimentRun(
        run_id="run_v_1",
        experiment_id="exp_v",
        started_at="2026-08-20T10:00:00+00:00",
        finished_at="2026-08-20T10:00:01+00:00",
        exit_code=exit_code,
        stdout_path=None,
        stderr_path=None,
        environment_fingerprint="a" * 64,
        success=exit_code == 0,
    )


def _artifact(tmp_path: Path, run_id: str = "run_v_1") -> ArtifactRecord:
    path = tmp_path / "results.json"
    path.write_text('{"accuracy": 0.75}', encoding="utf-8")
    return record_artifact(path, run_id=run_id)


def _metric(
    artifact: ArtifactRecord,
    *,
    name: str = "accuracy",
    value: float = 0.75,
    seed: int | None = 42,
) -> MetricRecord:
    return MetricRecord(
        metric_id=f"met_{name}",
        run_id=artifact.run_id,
        artifact_id=artifact.artifact_id,
        name=name,
        value=value,
        source_path=artifact.path,
        source_locator="$.accuracy",
        seed=seed,
    )


def test_valid_run_passes(tmp_path: Path) -> None:
    """The happy path: real artifact, finite metric, matching seeds."""
    spec = _spec(tmp_path)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric]
    )

    assert report.passed is True
    assert report.issues == []
    assert report.verified_metrics == [metric.metric_id]
    assert report.verified_artifacts == [artifact.artifact_id]


def test_failed_run_rejected(tmp_path: Path) -> None:
    """A non-zero exit code fails validation and verifies nothing."""
    spec = _spec(tmp_path)
    run = _run(tmp_path, exit_code=1)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric]
    )

    assert report.passed is False
    codes = {issue.code for issue in report.issues}
    assert FAILED_RUN in codes
    assert report.verified_metrics == []
    assert report.verified_artifacts == []


def test_missing_artifact_rejected(tmp_path: Path) -> None:
    """A spec-declared artifact that was never produced fails validation."""
    spec = _spec(tmp_path)
    run = _run(tmp_path)
    # No artifacts: results.json never appeared.

    report = validate_integrity(spec=spec, run=run, artifacts=[], metrics=[])

    assert report.passed is False
    codes = {issue.code for issue in report.issues}
    assert MISSING_ARTIFACT in codes


def test_nan_metric_rejected(tmp_path: Path) -> None:
    """NaN and inf metric values never verify."""
    spec = _spec(tmp_path)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    nan_metric = _metric(artifact, name="nan_metric", value=float("nan"))
    inf_metric = _metric(artifact, name="inf_metric", value=float("inf"))

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact],
        metrics=[nan_metric, inf_metric],
    )

    assert report.passed is False
    nan_codes = [issue.code for issue in report.issues if issue.code == NAN_METRIC]
    assert len(nan_codes) == 2


def test_missing_seed_rejected(tmp_path: Path) -> None:
    """A spec without a seed fails when seeds are required (default)."""
    spec = _spec(tmp_path, seed=None)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact, seed=None)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric]
    )

    assert report.passed is False
    assert MISSING_SEED in {issue.code for issue in report.issues}


def test_missing_seed_allowed_when_not_required(tmp_path: Path) -> None:
    """Seedless specs pass only when the policy explicitly relaxes the rule."""
    from jiuwenswarm.research_integrity.validator import ValidationPolicy

    spec = _spec(tmp_path, seed=None)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact, seed=None)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric],
        policy=ValidationPolicy(require_seed=False),
    )

    assert report.passed is True


def test_missing_metric_source_rejected(tmp_path: Path) -> None:
    """A metric referencing an unknown artifact fails validation."""
    spec = _spec(tmp_path)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    orphan = _metric(artifact)
    orphan = orphan.model_copy(update={"artifact_id": "art_unknown"})

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[orphan]
    )

    assert report.passed is False
    assert MISSING_METRIC_SOURCE in {issue.code for issue in report.issues}


def test_declared_metric_not_produced_rejected(tmp_path: Path) -> None:
    """A declared metric spec with no parsed record fails validation."""
    from jiuwenswarm.research_integrity.schemas import MetricSpec

    spec = _spec(tmp_path)
    spec = spec.model_copy(
        update={
            "metric_specs": [
                MetricSpec(name="accuracy", source="results.json", locator="$.accuracy")
            ]
        }
    )
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[]
    )

    assert report.passed is False
    assert MISSING_METRIC in {issue.code for issue in report.issues}


def test_seed_mismatch_rejected(tmp_path: Path) -> None:
    """A metric recorded under a different seed than the spec fails."""
    spec = _spec(tmp_path, seed=42)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact, seed=7)

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric]
    )

    assert report.passed is False
    assert SEED_MISMATCH in {issue.code for issue in report.issues}


def test_modified_artifact_invalidates_metric(tmp_path: Path) -> None:
    """Editing an artifact after hashing fails hash verification."""
    spec = _spec(tmp_path)
    run = _run(tmp_path)
    artifact = _artifact(tmp_path)
    metric = _metric(artifact)

    # Tamper after the record was created.
    Path(artifact.path).write_text('{"accuracy": 0.99}', encoding="utf-8")

    report = validate_integrity(
        spec=spec, run=run, artifacts=[artifact], metrics=[metric]
    )

    assert report.passed is False
    assert ARTIFACT_HASH_MISMATCH in {issue.code for issue in report.issues}
