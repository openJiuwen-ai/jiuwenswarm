# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the lineage tools: inspect / verify / render.

A rendered LaTeX number must be reverse-traceable through

    LaTeX cell -> MetricRecord -> ArtifactRecord -> ExperimentRun
               -> ExperimentSpec -> environment/config/code hash
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from jiuwenswarm.research_integrity.examples import toy_experiment
from jiuwenswarm.research_integrity.schemas import ExperimentSpec, MetricSpec
from jiuwenswarm.research_integrity.tools import (
    inspect_experiment,
    render_experiment_table,
    run_research_experiment,
    verify_research_artifacts,
    UnverifiedMetricError,
)

_TOY = Path(toy_experiment.__file__).resolve()
_MANIFEST_SUBDIR = Path(".jiuwen") / "research_integrity"

# Tiny configurable generator used for multi-value aggregation tests: writes
# one results file per seed (a real multi-seed matrix never shares a path —
# each run's artifact must stay byte-identical after its run).
_GENERATOR_SRC = (
    "import argparse, json\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--value', type=float, required=True)\n"
    "p.add_argument('--seed', type=int, required=True)\n"
    "a = p.parse_args()\n"
    "json.dump({'accuracy': a.value, 'seed': a.seed}, "
    "open(f'results_seed{a.seed}.json', 'w'))\n"
)


def _manifest_root(tmp_path: Path) -> Path:
    return tmp_path / _MANIFEST_SUBDIR


def _toy_spec(tmp_path: Path, *, fail: bool = False) -> ExperimentSpec:
    command = f'"{sys.executable}" "{_TOY}" --seed 42 --output results.json'
    if fail:
        command += " --fail"
    return ExperimentSpec(
        experiment_id="exp_toy_lineage",
        name="toy_lineage",
        command=command,
        cwd=str(tmp_path),
        seed=42,
        expected_artifacts=["results.json"],
        metric_specs=[
            MetricSpec(name="accuracy", source="results.json", locator="$.accuracy"),
        ],
    )


def _generator_spec(tmp_path: Path, value: float, seed: int) -> ExperimentSpec:
    command = f'"{sys.executable}" gen.py --value {value} --seed {seed}'
    artifact = f"results_seed{seed}.json"
    return ExperimentSpec(
        experiment_id=f"exp_gen_seed{seed}",
        name=f"gen_seed{seed}",
        command=command,
        cwd=str(tmp_path),
        seed=seed,
        expected_artifacts=[artifact],
        metric_specs=[
            MetricSpec(name="accuracy", source=artifact, locator="$.accuracy"),
        ],
    )


# ---------------------------------------------------------------------------
# inspect_experiment
# ---------------------------------------------------------------------------


def test_inspect_experiment_returns_full_lineage(tmp_path: Path) -> None:
    """inspect_experiment reassembles the whole chain incl. fingerprint."""
    spec = _toy_spec(tmp_path)
    outcome = run_research_experiment(spec, project_root=tmp_path)

    lineage = inspect_experiment(
        outcome.run.run_id, manifest_root=_manifest_root(tmp_path)
    )

    assert lineage.spec == spec
    assert lineage.run == outcome.run
    assert lineage.report == outcome.report
    assert lineage.report is not None and lineage.report.passed
    assert lineage.artifacts and lineage.metrics
    assert lineage.metrics[0].value == pytest.approx(0.75)
    # The fingerprint is loaded and referenced by the run.
    assert lineage.fingerprint is not None
    assert (
        lineage.fingerprint.fingerprint_id
        == lineage.run.environment_fingerprint
    )
    assert lineage.fingerprint.python_version


def test_inspect_experiment_unknown_run_raises(tmp_path: Path) -> None:
    """An unknown run id is a hard error, not an empty lineage."""
    with pytest.raises(KeyError):
        inspect_experiment("run_does_not_exist", manifest_root=_manifest_root(tmp_path))


# ---------------------------------------------------------------------------
# verify_research_artifacts
# ---------------------------------------------------------------------------


def test_verify_research_artifacts_all_valid(tmp_path: Path) -> None:
    """Untouched artifacts: everything verifies."""
    outcome = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)

    summary = verify_research_artifacts(manifest_root=_manifest_root(tmp_path))

    assert summary.run_ids == [outcome.run.run_id]
    assert summary.all_valid
    assert summary.valid_metric_ids == [
        m.metric_id for m in outcome.metrics
    ]
    (check,) = summary.metrics
    assert check.name == "accuracy"
    assert check.reason == ""


def test_verify_research_artifacts_detects_tampering(tmp_path: Path) -> None:
    """Editing results.json after the run invalidates artifact + metric."""
    outcome = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)
    assert outcome.passed

    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    payload["accuracy"] = 1.0
    (tmp_path / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    summary = verify_research_artifacts(manifest_root=_manifest_root(tmp_path))

    assert not summary.all_valid
    assert not summary.artifacts[0].valid
    (check,) = summary.metrics
    assert not check.valid
    assert "hash mismatch" in check.reason


def test_verify_research_artifacts_failed_run_metric_invalid(tmp_path: Path) -> None:
    """A failed run's metrics never verify, with the reason spelled out."""
    outcome = run_research_experiment(
        _toy_spec(tmp_path, fail=True), project_root=tmp_path
    )
    assert not outcome.passed

    summary = verify_research_artifacts(manifest_root=_manifest_root(tmp_path))

    assert not summary.all_valid
    (check,) = summary.metrics
    assert check.metric_id not in summary.valid_metric_ids
    assert "failed integrity validation" in check.reason


def test_verify_research_artifacts_explicit_run_subset(tmp_path: Path) -> None:
    """run_ids=... verifies only a report's dependencies, not the store."""
    good = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)
    # Second, failing run in the same store (distinct experiment id).
    fail_spec = _toy_spec(tmp_path, fail=True).model_copy(
        update={"experiment_id": "exp_toy_lineage_fail"}
    )
    run_research_experiment(fail_spec, project_root=tmp_path)

    summary = verify_research_artifacts(
        manifest_root=_manifest_root(tmp_path), run_ids=[good.run.run_id]
    )

    assert summary.run_ids == [good.run.run_id]
    assert summary.all_valid


# ---------------------------------------------------------------------------
# render_experiment_table
# ---------------------------------------------------------------------------


def test_render_latex_single_run(tmp_path: Path) -> None:
    """One run: mean = value, std = 0, n = 1, booktabs row shape."""
    outcome = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)

    row = render_experiment_table(
        method="MethodA",
        metric="accuracy",
        run_ids=[outcome.run.run_id],
        manifest_root=_manifest_root(tmp_path),
        fmt="latex",
    )

    assert row == "MethodA & 0.7500 $\\pm$ 0.0000 & 0.7500 & 0.7500 & 1 \\\\"
    assert row.startswith("MethodA &")
    assert row.endswith(" \\\\")


def test_render_multi_run_aggregation(tmp_path: Path) -> None:
    """mean/std/min/max/n across seeded runs (sample std, ddof=1)."""
    (tmp_path / "gen.py").write_text(_GENERATOR_SRC, encoding="utf-8")
    values = {42: 0.70, 43: 0.80, 44: 0.90}
    run_ids = []
    for seed, value in values.items():
        outcome = run_research_experiment(
            _generator_spec(tmp_path, value, seed), project_root=tmp_path
        )
        assert outcome.passed, [i.message for i in outcome.report.issues]
        run_ids.append(outcome.run.run_id)

    row = render_experiment_table(
        method="MethodA",
        metric="accuracy",
        run_ids=run_ids,
        manifest_root=_manifest_root(tmp_path),
        fmt="latex",
    )

    # mean = 0.8, sample std = 0.1, min = 0.7, max = 0.9, n = 3.
    assert row == "MethodA & 0.8000 $\\pm$ 0.1000 & 0.7000 & 0.9000 & 3 \\\\"


def test_render_csv_and_json(tmp_path: Path) -> None:
    """CSV row and JSON payload with per-value provenance."""
    (tmp_path / "gen.py").write_text(_GENERATOR_SRC, encoding="utf-8")
    outcome = run_research_experiment(
        _generator_spec(tmp_path, 0.75, 42), project_root=tmp_path
    )

    csv_out = render_experiment_table(
        method="MethodA",
        metric="accuracy",
        run_ids=[outcome.run.run_id],
        manifest_root=_manifest_root(tmp_path),
        fmt="csv",
    )
    assert csv_out.splitlines() == [
        "method,metric,mean,std,min,max,n",
        "MethodA,accuracy,0.7500,0.0000,0.7500,0.7500,1",
    ]

    json_out = render_experiment_table(
        method="MethodA",
        metric="accuracy",
        run_ids=[outcome.run.run_id],
        manifest_root=_manifest_root(tmp_path),
        fmt="json",
    )
    payload = json.loads(json_out)
    assert payload["n"] == 1
    assert payload["mean"] == pytest.approx(0.75)
    assert payload["seeds"] == [42]
    assert payload["run_ids"] == [outcome.run.run_id]
    (metric_id,) = payload["metric_ids"]
    prov = payload["provenance"][metric_id]
    assert prov["run_id"] == outcome.run.run_id
    assert prov["source_locator"] == "$.accuracy"
    assert len(prov["artifact_sha256"]) == 64


def test_render_rejects_tampered_artifact(tmp_path: Path) -> None:
    """A row never renders from a tampered artifact."""
    outcome = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)
    payload = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    payload["accuracy"] = 1.0
    (tmp_path / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UnverifiedMetricError, match="sha256"):
        render_experiment_table(
            method="MethodA",
            metric="accuracy",
            run_ids=[outcome.run.run_id],
            manifest_root=_manifest_root(tmp_path),
        )


def test_render_rejects_failed_run(tmp_path: Path) -> None:
    """A row never renders from a run that failed validation."""
    outcome = run_research_experiment(
        _toy_spec(tmp_path, fail=True), project_root=tmp_path
    )

    with pytest.raises(UnverifiedMetricError, match="not verified"):
        render_experiment_table(
            method="MethodA",
            metric="accuracy",
            run_ids=[outcome.run.run_id],
            manifest_root=_manifest_root(tmp_path),
        )


def test_render_rejects_unknown_metric(tmp_path: Path) -> None:
    """Asking for a metric the run never produced fails loudly."""
    outcome = run_research_experiment(_toy_spec(tmp_path), project_root=tmp_path)

    with pytest.raises(UnverifiedMetricError, match="no metric named"):
        render_experiment_table(
            method="MethodA",
            metric="f1",
            run_ids=[outcome.run.run_id],
            manifest_root=_manifest_root(tmp_path),
        )


def test_render_validates_inputs(tmp_path: Path) -> None:
    """Empty run_ids / unknown fmt are caller errors."""
    with pytest.raises(ValueError, match="run_ids"):
        render_experiment_table(
            method="m",
            metric="accuracy",
            run_ids=[],
            manifest_root=_manifest_root(tmp_path),
        )
    with pytest.raises(ValueError, match="fmt"):
        render_experiment_table(
            method="m",
            metric="accuracy",
            run_ids=["run_x"],
            manifest_root=_manifest_root(tmp_path),
            fmt="markdown",
        )


# ---------------------------------------------------------------------------
# LaTeX cell reverse traceability
# ---------------------------------------------------------------------------


def test_latex_number_reverse_traceability(tmp_path: Path) -> None:
    """From the final LaTeX number back to spec + environment hashes.

    Every digit that can appear in a report is traceable to a
    real artifact, a real process execution, and the exact environment
    (code hash) it ran under. The experiment runs a real ``evaluate.py``
    inside the project root, so the fingerprint's code hash covers the
    very code that computed the number.
    """
    # A real project: evaluate.py lives in the project root and is hashed
    # into the environment fingerprint's code_hash.
    (tmp_path / "evaluate.py").write_text(_GENERATOR_SRC, encoding="utf-8")
    spec = ExperimentSpec(
        experiment_id="exp_latex_trace",
        name="latex_trace_toy",
        command=f'"{sys.executable}" evaluate.py --value 0.75 --seed 42',
        cwd=str(tmp_path),
        seed=42,
        expected_artifacts=["results_seed42.json"],
        metric_specs=[
            MetricSpec(
                name="accuracy", source="results_seed42.json", locator="$.accuracy"
            ),
        ],
    )
    outcome = run_research_experiment(spec, project_root=tmp_path)
    assert outcome.passed, [i.message for i in outcome.report.issues]

    row = render_experiment_table(
        method="MethodA",
        metric="accuracy",
        run_ids=[outcome.run.run_id],
        manifest_root=_manifest_root(tmp_path),
        fmt="latex",
    )

    # -- Forward: extract the mean cell from the rendered LaTeX row.
    match = re.match(r"^(\S+) & (\d+\.\d+) \$\\pm\$ (\d+\.\d+)", row)
    assert match is not None, row
    method_label, mean_cell, std_cell = match.groups()
    assert method_label == "MethodA"
    assert float(mean_cell) == pytest.approx(0.75)
    assert float(std_cell) == pytest.approx(0.0)

    # -- Reverse: LaTeX cell -> MetricRecord -> ArtifactRecord
    #    -> ExperimentRun -> ExperimentSpec -> fingerprint hashes.
    lineage = inspect_experiment(
        outcome.run.run_id, manifest_root=_manifest_root(tmp_path)
    )

    # 1) The rendered number equals the recorded metric value...
    (metric,) = lineage.metrics
    assert float(mean_cell) == pytest.approx(metric.value)
    assert metric.run_id == outcome.run.run_id
    assert metric.source_locator == "$.accuracy"

    # 2) ...whose artifact still hashes to the recorded digest...
    (artifact,) = lineage.artifacts
    assert artifact.artifact_id == metric.artifact_id
    assert artifact.run_id == outcome.run.run_id
    assert artifact.sha256 == outcome.artifacts[0].sha256

    # 3) ...produced by the run that executed the declared spec...
    assert lineage.run.run_id == metric.run_id
    assert lineage.run.experiment_id == spec.experiment_id
    assert lineage.run.exit_code == 0
    assert lineage.spec == spec
    assert lineage.spec.command == spec.command

    # 4) ...under a fingerprinted environment: the code hash covers
    #    evaluate.py itself (the code that computed the number).
    fingerprint = lineage.fingerprint
    assert fingerprint is not None
    assert fingerprint.fingerprint_id == lineage.run.environment_fingerprint
    assert fingerprint.code_hash and len(fingerprint.code_hash) == 64
    assert fingerprint.python_version

    # 5) And the verdict that authorized the number is on record.
    assert lineage.report is not None
    assert lineage.report.passed
    assert metric.metric_id in lineage.report.verified_metrics
