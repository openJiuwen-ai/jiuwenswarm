# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Experiment integrity validation.

Given a run, its spec, its artifacts and its metrics, decide whether the run's
numbers are trustworthy. Every rule here exists because an LLM-generated
"result" must never be able to masquerade as a measured one:

- FAILED_RUN          exit code != 0 / success flag false -> reject
- MISSING_ARTIFACT    a spec-declared expected artifact was not produced
- MISSING_SEED        spec.seed missing while seeds are required
- NAN_METRIC          non-finite (NaN/inf) metric value
- MISSING_METRIC      a spec-declared metric produced no record
- MISSING_METRIC_SOURCE  metric without a resolvable artifact record
- SEED_MISMATCH       metric seed differs from spec seed
- ARTIFACT_HASH_MISMATCH  artifact changed after hashing (tamper)

All failures are reported; none are silently ignored.
"""

from __future__ import annotations

import math

from jiuwenswarm.research_integrity.artifact_tracker import verify_artifact
from jiuwenswarm.research_integrity.schemas import (
    ArtifactRecord,
    ExperimentRun,
    ExperimentSpec,
    IntegrityIssue,
    IntegrityReport,
    MetricRecord,
)

# Issue codes (stable; logs and tests key off these).
FAILED_RUN = "FAILED_RUN"
MISSING_ARTIFACT = "MISSING_ARTIFACT"
MISSING_SEED = "MISSING_SEED"
NAN_METRIC = "NAN_METRIC"
MISSING_METRIC = "MISSING_METRIC"
MISSING_METRIC_SOURCE = "MISSING_METRIC_SOURCE"
SEED_MISMATCH = "SEED_MISMATCH"
ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"


class ValidationPolicy:
    """Toggleable validation rules (mirrors config ``research_integrity``).

    All defaults are the strict/safe choice; each flag maps 1:1 to a
    ``verification.*`` key in the framework config.
    """

    def __init__(
        self,
        *,
        require_seed: bool = True,
        require_artifact: bool = True,
        require_metric_source: bool = True,
        reject_failed_runs: bool = True,
        reject_nan_metrics: bool = True,
        verify_artifact_hashes: bool = True,
    ) -> None:
        """Create a policy with explicit rule toggles.

        Args:
            require_seed: Fail when ``spec.seed`` is ``None``.
            require_artifact: Fail when an expected artifact is missing.
            require_metric_source: Fail when a metric lacks an artifact.
            reject_failed_runs: Fail runs whose process exited non-zero.
            reject_nan_metrics: Fail non-finite metric values.
            verify_artifact_hashes: Re-hash artifacts at validation time.
        """
        self.require_seed = require_seed
        self.require_artifact = require_artifact
        self.require_metric_source = require_metric_source
        self.reject_failed_runs = reject_failed_runs
        self.reject_nan_metrics = reject_nan_metrics
        self.verify_artifact_hashes = verify_artifact_hashes


DEFAULT_POLICY = ValidationPolicy()


def validate_integrity(
    *,
    spec: ExperimentSpec,
    run: ExperimentRun,
    artifacts: list[ArtifactRecord],
    metrics: list[MetricRecord],
    policy: ValidationPolicy | None = None,
    extra_issues: list[IntegrityIssue] | None = None,
) -> IntegrityReport:
    """Validate one experiment run against its spec and records.

    Args:
        spec: The declared experiment (created before execution).
        run: The real execution record.
        artifacts: Hashed artifact records produced by the run.
        metrics: Metric records parsed from those artifacts.
        policy: Rule toggles; defaults to :data:`DEFAULT_POLICY`.
        extra_issues: Issues raised by the execution layer (e.g. metric
            parse errors) folded into the same report.

    Returns:
        An :class:`IntegrityReport`. ``passed`` is true only when *no* issue
        was found; ``verified_metrics`` / ``verified_artifacts`` are empty
        whenever the report fails (a failed run verifies nothing).
    """
    pol = policy or DEFAULT_POLICY
    issues: list[IntegrityIssue] = list(extra_issues or [])

    if pol.reject_failed_runs and (not run.success or run.exit_code != 0):
        issues.append(
            IntegrityIssue(
                code=FAILED_RUN,
                message=(
                    f"run {run.run_id} exited with code {run.exit_code}; "
                    "metrics from failed runs are never verifiable"
                ),
                details={"exit_code": run.exit_code, "success": run.success},
            )
        )

    if pol.require_seed and spec.seed is None:
        issues.append(
            IntegrityIssue(
                code=MISSING_SEED,
                message=(
                    f"experiment {spec.experiment_id} has no seed; "
                    "reproducibility requires an explicit seed"
                ),
            )
        )

    artifact_ids = {record.artifact_id for record in artifacts}

    if pol.require_artifact:
        for expected in spec.expected_artifacts:
            expected_norm = expected.replace("\\", "/")
            found = any(
                record.path.replace("\\", "/").endswith(expected_norm)
                for record in artifacts
            )
            if not found:
                issues.append(
                    IntegrityIssue(
                        code=MISSING_ARTIFACT,
                        message=(
                            f"expected artifact {expected!r} missing after run "
                            f"{run.run_id}"
                        ),
                        details={"expected": expected},
                    )
                )

    if pol.verify_artifact_hashes:
        for record in artifacts:
            if not verify_artifact(record):
                issues.append(
                    IntegrityIssue(
                        code=ARTIFACT_HASH_MISMATCH,
                        message=(
                            f"artifact {record.path} changed after hashing "
                            "(sha256 mismatch); its metrics are invalidated"
                        ),
                        details={
                            "artifact_id": record.artifact_id,
                            "recorded_sha256": record.sha256,
                        },
                    )
                )

    for metric in metrics:
        if pol.reject_nan_metrics and not math.isfinite(metric.value):
            issues.append(
                IntegrityIssue(
                    code=NAN_METRIC,
                    message=(
                        f"metric {metric.name!r} is non-finite ({metric.value}); "
                        "NaN/inf values cannot enter published tables"
                    ),
                    details={"metric_id": metric.metric_id, "value": str(metric.value)},
                )
            )
        if pol.require_metric_source and metric.artifact_id not in artifact_ids:
            issues.append(
                IntegrityIssue(
                    code=MISSING_METRIC_SOURCE,
                    message=(
                        f"metric {metric.name!r} references unknown artifact "
                        f"{metric.artifact_id!r}"
                    ),
                    details={"metric_id": metric.metric_id},
                )
            )
        if metric.seed is not None and spec.seed is not None and metric.seed != spec.seed:
            issues.append(
                IntegrityIssue(
                    code=SEED_MISMATCH,
                    message=(
                        f"metric {metric.name!r} seed {metric.seed} differs from "
                        f"spec seed {spec.seed}"
                    ),
                    details={"metric_id": metric.metric_id},
                )
            )

    produced_names = {metric.name for metric in metrics}
    for metric_spec in spec.metric_specs:
        if metric_spec.name not in produced_names:
            issues.append(
                IntegrityIssue(
                    code=MISSING_METRIC,
                    message=(
                        f"declared metric {metric_spec.name!r} produced no record "
                        f"for run {run.run_id}"
                    ),
                    details={"locator": metric_spec.locator},
                )
            )

    passed = not issues
    return IntegrityReport(
        run_id=run.run_id,
        passed=passed,
        issues=issues,
        verified_metrics=[m.metric_id for m in metrics] if passed else [],
        verified_artifacts=[a.artifact_id for a in artifacts] if passed else [],
    )


__all__ = [
    "ValidationPolicy",
    "DEFAULT_POLICY",
    "validate_integrity",
    "FAILED_RUN",
    "MISSING_ARTIFACT",
    "MISSING_SEED",
    "NAN_METRIC",
    "MISSING_METRIC",
    "MISSING_METRIC_SOURCE",
    "SEED_MISMATCH",
    "ARTIFACT_HASH_MISMATCH",
    "IntegrityIssue",
    "IntegrityReport",
]
