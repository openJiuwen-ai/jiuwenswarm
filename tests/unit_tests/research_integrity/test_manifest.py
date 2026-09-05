# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for research_integrity schemas and manifest persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jiuwenswarm.research_integrity.manifest import ManifestStore
from jiuwenswarm.research_integrity.schemas import (
    ArtifactRecord,
    ExperimentRun,
    ExperimentSpec,
    IntegrityIssue,
    IntegrityReport,
    MetricRecord,
    MetricSpec,
)


def _make_spec(tmp_path: Path) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_method_a_seed42",
        name="method_a_seed42",
        hypothesis_id="hyp-1",
        command="python evaluate.py --method method_a --seed 42",
        cwd=str(tmp_path),
        seed=42,
        config_path="configs/exp.yaml",
        dataset_paths=["data/qa.jsonl"],
        expected_artifacts=["results/method_a_seed42.json"],
        metric_specs=[
            MetricSpec(
                name="accuracy",
                source="results/method_a_seed42.json",
                locator="$.accuracy",
            )
        ],
    )


def test_manifest_creation(tmp_path: Path) -> None:
    """A spec round-trips through the manifest store unchanged."""
    store = ManifestStore(tmp_path / "manifests")
    spec = _make_spec(tmp_path)

    store.save_spec(spec)
    loaded = store.load_spec(spec.experiment_id)

    assert loaded == spec
    assert store.list_spec_ids() == [spec.experiment_id]
    assert (tmp_path / "manifests" / "specs" / f"{spec.experiment_id}.json").is_file()


def test_manifest_creates_layout(tmp_path: Path) -> None:
    """The store creates the full record-type layout on construction."""
    root = tmp_path / "manifests"
    ManifestStore(root)
    for sub in ("specs", "runs", "artifacts", "metrics", "reports", "fingerprints"):
        assert (root / sub).is_dir(), sub


def test_run_artifact_metric_report_round_trip(tmp_path: Path) -> None:
    """Run/artifact/metric/report records persist and reload identically."""
    store = ManifestStore(tmp_path / "manifests")

    spec = ExperimentSpec(
        experiment_id="exp_x",
        name="x",
        command="python x.py",
        cwd=str(tmp_path),
        seed=7,
        expected_artifacts=["results.json"],
    )
    run = ExperimentRun(
        run_id="run_x_1",
        experiment_id="exp_x",
        started_at="2026-08-20T10:00:00+00:00",
        finished_at="2026-08-20T10:00:05+00:00",
        exit_code=0,
        stdout_path="stdout.txt",
        stderr_path="stderr.txt",
        environment_fingerprint="a" * 64,
        success=True,
    )
    artifact = ArtifactRecord(
        artifact_id="art_run_x_1_abc",
        run_id=run.run_id,
        path=str(tmp_path / "results.json"),
        sha256="b" * 64,
        size_bytes=42,
        kind="json",
    )
    metric = MetricRecord(
        metric_id="met_abc",
        run_id=run.run_id,
        artifact_id=artifact.artifact_id,
        name="accuracy",
        value=0.75,
        source_path=artifact.path,
        source_locator="$.accuracy",
        seed=7,
    )
    report = IntegrityReport(
        run_id=run.run_id,
        passed=True,
        issues=[],
        verified_metrics=[metric.metric_id],
        verified_artifacts=[artifact.artifact_id],
    )

    store.save_spec(spec)
    store.save_run(run)
    store.save_artifact(artifact)
    store.save_metric(metric)
    store.save_report(report)

    lineage = store.load_lineage(run.run_id)
    assert lineage["spec"] == spec
    assert lineage["run"] == run
    assert lineage["artifacts"] == [artifact]
    assert lineage["metrics"] == [metric]
    assert lineage["report"] == report


def test_load_missing_record_raises_key_error(tmp_path: Path) -> None:
    """Loading an unknown record id raises KeyError with a useful message."""
    store = ManifestStore(tmp_path / "manifests")
    with pytest.raises(KeyError, match="not found"):
        store.load_run("run_does_not_exist")


def test_spec_rejects_empty_command() -> None:
    """Schemas validate: an empty command is not a runnable experiment."""
    with pytest.raises(ValidationError):
        ExperimentSpec(
            experiment_id="exp_bad",
            name="bad",
            command="   ",
        )


def test_integrity_report_defaults(tmp_path: Path) -> None:
    """A report built from issues carries the issue list verbatim."""
    issue = IntegrityIssue(code="FAILED_RUN", message="exit 1", details={})
    report = IntegrityReport(run_id="run_bad", passed=False, issues=[issue])
    assert report.passed is False
    assert report.issues == [issue]
    assert report.verified_metrics == []
    assert report.verified_artifacts == []
