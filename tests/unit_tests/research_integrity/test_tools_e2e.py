# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end tests: run_research_experiment against the real toy experiment.

The tests prove the reverse-traceable chain

    0.75 -> MetricRecord -> ArtifactRecord -> ExperimentRun -> ExperimentSpec

by executing the toy experiment as a *real subprocess* (no mocking of the
process, the artifact, or the parser).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from jiuwenswarm.research_integrity.artifact_tracker import verify_artifact
from jiuwenswarm.research_integrity.examples import toy_experiment
from jiuwenswarm.research_integrity.examples.toy_experiment import evaluate
from jiuwenswarm.research_integrity.manifest import ManifestStore
from jiuwenswarm.research_integrity.schemas import ExperimentSpec, MetricSpec
from jiuwenswarm.research_integrity.tools import run_research_experiment

# Absolute path to the toy script: the subprocess runs with cwd=<tmp_path>,
# so a repo-relative path would not resolve (and the repo path contains a
# space, hence the quoting).
_TOY = Path(toy_experiment.__file__).resolve()


def _toy_spec(tmp_path: Path, *, fail: bool = False) -> ExperimentSpec:
    command = f'"{sys.executable}" "{_TOY}" --seed 42 --output results.json'
    if fail:
        command += " --fail"
    return ExperimentSpec(
        experiment_id="exp_toy_e2e",
        name="toy_e2e",
        hypothesis_id="hyp-toy",
        command=command,
        cwd=str(tmp_path),
        seed=42,
        expected_artifacts=["results.json"],
        metric_specs=[
            MetricSpec(name="accuracy", source="results.json", locator="$.accuracy"),
            MetricSpec(name="n_samples", source="results.json", locator="$.n_samples"),
        ],
    )


def test_toy_experiment_computes_075() -> None:
    """The toy experiment itself computes accuracy = 0.75 (15/20)."""
    assert evaluate() == pytest.approx(0.75)


def test_e2e_normal_experiment_full_lineage(tmp_path: Path) -> None:
    """E2E-1: real run -> artifact -> metrics -> PASS + full lineage."""
    spec = _toy_spec(tmp_path)

    outcome = run_research_experiment(spec, project_root=tmp_path)

    # Run succeeded and passed integrity.
    assert outcome.run.exit_code == 0
    assert outcome.passed, [i.message for i in outcome.report.issues]
    assert outcome.report.verified_metrics

    # The chain: 0.75 -> MetricRecord -> ArtifactRecord -> Run -> Spec.
    (accuracy,) = [m for m in outcome.metrics if m.name == "accuracy"]
    assert accuracy.value == pytest.approx(0.75)

    artifact = next(
        a for a in outcome.artifacts if a.artifact_id == accuracy.artifact_id
    )
    assert verify_artifact(artifact)
    assert artifact.run_id == accuracy.run_id == outcome.run.run_id
    assert outcome.run.experiment_id == spec.experiment_id

    # Everything persisted: manifests reload and agree with the outcome
    # (lineage lists are deterministic: artifacts by path, metrics by name).
    store = ManifestStore(tmp_path / ".jiuwen" / "research_integrity")
    lineage = store.load_lineage(outcome.run.run_id)
    assert lineage["spec"] == spec
    assert lineage["run"] == outcome.run
    assert lineage["artifacts"] == sorted(
        outcome.artifacts, key=lambda a: a.path
    )
    assert lineage["metrics"] == sorted(outcome.metrics, key=lambda m: m.name)
    assert lineage["report"] == outcome.report

    # The environment fingerprint is stored and referenced by the run.
    fingerprint = store.load_fingerprint(outcome.run.environment_fingerprint)
    assert fingerprint is not None
    assert fingerprint.python_version


def test_e2e_failed_experiment_rejected(tmp_path: Path) -> None:
    """E2E-2: exit_code != 0 -> report fails, metrics not verified."""
    spec = _toy_spec(tmp_path, fail=True)

    outcome = run_research_experiment(spec, project_root=tmp_path)

    assert outcome.run.exit_code != 0
    assert outcome.run.success is False
    assert outcome.report is not None
    assert outcome.report.passed is False
    assert "FAILED_RUN" in {issue.code for issue in outcome.report.issues}
    assert outcome.report.verified_metrics == []


def test_e2e_missing_artifact_rejected(tmp_path: Path) -> None:
    """A spec expecting an artifact the command never writes fails."""
    spec = _toy_spec(tmp_path)
    spec = spec.model_copy(
        update={
            "expected_artifacts": ["results.json", "never_written.json"],
            "experiment_id": "exp_toy_missing",
        }
    )

    outcome = run_research_experiment(spec, project_root=tmp_path)

    assert outcome.report.passed is False
    assert "MISSING_ARTIFACT" in {issue.code for issue in outcome.report.issues}


def test_e2e_tampered_artifact_detected(tmp_path: Path) -> None:
    """E2E-3: editing results.json after the run invalidates the report."""
    spec = _toy_spec(tmp_path)
    outcome = run_research_experiment(spec, project_root=tmp_path)
    assert outcome.passed

    # Tamper with the artifact, then re-validate via a fresh run of the
    # validator through the manifest store records.
    results_path = tmp_path / "results.json"
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    payload["accuracy"] = 1.0
    results_path.write_text(json.dumps(payload), encoding="utf-8")

    store = ManifestStore(tmp_path / ".jiuwen" / "research_integrity")
    lineage = store.load_lineage(outcome.run.run_id)
    from jiuwenswarm.research_integrity.validator import validate_integrity

    report = validate_integrity(
        spec=lineage["spec"],
        run=lineage["run"],
        artifacts=lineage["artifacts"],
        metrics=lineage["metrics"],
    )
    assert report.passed is False
    assert "ARTIFACT_HASH_MISMATCH" in {
        issue.code for issue in report.issues
    }
