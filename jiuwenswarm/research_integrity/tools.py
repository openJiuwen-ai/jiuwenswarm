# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Research experiment execution tools (framework-free core).

``run_research_experiment`` is the single sanctioned path from a declared
:class:`ExperimentSpec` to verified metrics:

    validate spec
    -> fingerprint environment
    -> execute process
    -> capture stdout/stderr
    -> discover + hash artifacts
    -> parse metrics deterministically
    -> validate integrity
    -> persist run / artifacts / metrics / report
    -> emit report

Metrics never pass through an LLM: they are parsed from the artifacts the
real process wrote, or they do not exist.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.research_integrity.artifact_tracker import record_artifact, verify_artifact
from jiuwenswarm.research_integrity.fingerprint import (
    EnvironmentFingerprint,
    capture_environment_fingerprint,
)
from jiuwenswarm.research_integrity.manifest import ManifestStore
from jiuwenswarm.research_integrity.metric_parser import (
    MetricParseError,
    parse_metric,
)
from jiuwenswarm.research_integrity.schemas import (
    ArtifactRecord,
    ExperimentRun,
    ExperimentSpec,
    IntegrityIssue,
    IntegrityReport,
    MetricRecord,
)
from jiuwenswarm.research_integrity.validator import (
    ValidationPolicy,
    validate_integrity,
)

logger = logging.getLogger(__name__)

_METRIC_PARSE_ERROR = "METRIC_PARSE_ERROR"
_DEFAULT_TIMEOUT_SECONDS = 3600.0


def _utc_now_iso() -> str:
    """Current UTC time in ISO-8601 (second precision, Z suffix)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id(experiment_id: str) -> str:
    """Mint a unique, sortable run id."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"run_{experiment_id}_{stamp}_{uuid.uuid4().hex[:8]}"


def _new_metric_id(run_id: str, name: str) -> str:
    """Mint a stable-enough metric id from run + metric name."""
    token = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}::{name}").hex[:12]
    return f"met_{token}"


def _resolve(path: str, base: Path) -> Path:
    """Resolve *path* against *base* unless absolute."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else base / candidate


@dataclass
class ExperimentOutcome:
    """Everything one experiment execution produced, plus its verdict.

    Attributes:
        spec: The declared experiment spec.
        run: The real execution record.
        artifacts: Hashed artifact records (only files that exist).
        metrics: Parsed metric records (parse failures are in *issues*).
        report: The integrity report (the authoritative verdict).
        parse_errors: Metric specs that could not be parsed, with reasons.
    """

    spec: ExperimentSpec
    run: ExperimentRun
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    metrics: list[MetricRecord] = field(default_factory=list)
    report: IntegrityReport | None = None
    parse_errors: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether the integrity report passed."""
        return bool(self.report and self.report.passed)


def run_research_experiment(
    spec: ExperimentSpec | dict[str, Any],
    *,
    project_root: str | Path,
    manifest_root: str | Path | None = None,
    env_whitelist: list[str] | None = None,
    capture_git: bool = True,
    policy: ValidationPolicy | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ExperimentOutcome:
    """Execute one declared experiment and return its full integrity outcome.

    Args:
        spec: The experiment declaration (model or plain dict).
        project_root: Project root for environment fingerprinting.
        manifest_root: Manifest store location (default
            ``<project_root>/.jiuwen/research_integrity``).
        env_whitelist: Environment variables allowed into the fingerprint.
        capture_git: Whether to capture git commit/dirty state.
        policy: Validation policy toggles.
        timeout: Process timeout in seconds.

    Returns:
        An :class:`ExperimentOutcome`. A failed process or violated rule
        never raises — it is recorded in the report so the failure is
        traceable, not silent.
    """
    if isinstance(spec, dict):
        spec = ExperimentSpec.model_validate(spec)
    if not spec.command.strip():
        raise ValueError("experiment spec has an empty command")

    project = Path(project_root).resolve()
    manifest_dir = (
        Path(manifest_root).resolve()
        if manifest_root is not None
        else project / ".jiuwen" / "research_integrity"
    )
    store = ManifestStore(manifest_dir)
    store.save_spec(spec)

    fingerprint = capture_environment_fingerprint(
        project,
        config_path=_resolve(spec.config_path, project) if spec.config_path else None,
        dataset_paths=[
            str(_resolve(item, project)) for item in spec.dataset_paths
        ],
        capture_git=capture_git,
        env_whitelist=env_whitelist,
    )
    store.save_fingerprint(fingerprint)

    run_id = _new_run_id(spec.experiment_id)
    cwd = _resolve(spec.cwd, project)
    started_at = _utc_now_iso()
    stdout_path = store.root / "runs" / f"{run_id}.stdout.txt"
    stderr_path = store.root / "runs" / f"{run_id}.stderr.txt"

    logger.info(
        "[ResearchIntegrity] run %s starting: %s (cwd=%s)", run_id, spec.command, cwd
    )
    try:
        completed = subprocess.run(  # noqa: S603 - argv from spec, no shell
            shlex.split(spec.command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = -1
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"TIMEOUT after {timeout}s\n" + (
            (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        )
    except OSError as exc:
        exit_code = -1
        stdout = ""
        stderr = f"EXECUTION_ERROR: {exc}"

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    finished_at = _utc_now_iso()

    run = ExperimentRun(
        run_id=run_id,
        experiment_id=spec.experiment_id,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        environment_fingerprint=fingerprint.fingerprint_id,
        success=exit_code == 0,
    )
    store.save_run(run)

    artifacts: list[ArtifactRecord] = []
    for expected in spec.expected_artifacts:
        artifact_path = _resolve(expected, cwd)
        try:
            artifacts.append(record_artifact(artifact_path, run_id=run_id))
        except FileNotFoundError:
            logger.warning(
                "[ResearchIntegrity] run %s missing expected artifact %s",
                run_id,
                expected,
            )

    metrics: list[MetricRecord] = []
    parse_errors: dict[str, str] = {}
    artifact_by_path = {record.path: record for record in artifacts}
    for metric_spec in spec.metric_specs:
        source_path = _resolve(metric_spec.source, cwd)
        artifact = artifact_by_path.get(str(source_path))
        if artifact is None:
            try:
                artifact = record_artifact(source_path, run_id=run_id)
                artifacts.append(artifact)
                artifact_by_path[str(source_path)] = artifact
            except FileNotFoundError as exc:
                parse_errors[metric_spec.name] = str(exc)
                continue
        try:
            value = parse_metric(source_path, metric_spec.locator)
        except MetricParseError as exc:
            parse_errors[metric_spec.name] = str(exc)
            continue
        metrics.append(
            MetricRecord(
                metric_id=_new_metric_id(run_id, metric_spec.name),
                run_id=run_id,
                artifact_id=artifact.artifact_id,
                name=metric_spec.name,
                value=value,
                source_path=str(source_path),
                source_locator=metric_spec.locator,
                seed=spec.seed,
            )
        )

    extra_issues = [
        IntegrityIssue(
            code=_METRIC_PARSE_ERROR,
            message=f"metric {name!r} could not be parsed: {reason}",
            details={"metric": name, "reason": reason},
        )
        for name, reason in parse_errors.items()
    ]
    report = validate_integrity(
        spec=spec,
        run=run,
        artifacts=artifacts,
        metrics=metrics,
        policy=policy,
        extra_issues=extra_issues,
    )

    for artifact in artifacts:
        store.save_artifact(artifact)
    for metric in metrics:
        store.save_metric(metric)
    store.save_report(report)

    logger.info(
        "[ResearchIntegrity] run %s finished: exit=%s passed=%s issues=%d",
        run_id,
        exit_code,
        report.passed,
        len(report.issues),
    )
    if not report.passed:
        for issue in report.issues:
            logger.warning(
                "[ResearchIntegrity] run %s issue %s: %s",
                run_id,
                issue.code,
                issue.message,
            )

    return ExperimentOutcome(
        spec=spec,
        run=run,
        artifacts=artifacts,
        metrics=metrics,
        report=report,
        parse_errors=parse_errors,
    )


# ---------------------------------------------------------------------------
# inspect_experiment — full lineage of one run
# ---------------------------------------------------------------------------


@dataclass
class ExperimentLineage:
    """The complete traceable chain of one experiment run.

    Every number that can reach a published report is traceable through this
    chain: ``fingerprint -> spec -> run -> artifacts -> metrics -> report``.
    """

    spec: ExperimentSpec
    run: ExperimentRun
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    metrics: list[MetricRecord] = field(default_factory=list)
    report: IntegrityReport | None = None
    fingerprint: EnvironmentFingerprint | None = None


def inspect_experiment(
    run_id: str,
    *,
    manifest_root: str | Path,
) -> ExperimentLineage:
    """Return the full lineage of *run_id*.

    Args:
        run_id: The run whose lineage is requested.
        manifest_root: Manifest store location.

    Returns:
        An :class:`ExperimentLineage` with spec, run, artifacts, metrics,
        integrity report and the environment fingerprint the run executed
        under. Artifacts are ordered by path and metrics by name
        (deterministic across reloads).

    Raises:
        KeyError: No run with that id exists in the store.
    """
    store = ManifestStore(manifest_root)
    lineage = store.load_lineage(run_id)
    run = lineage["run"]
    fingerprint = store.load_fingerprint(run.environment_fingerprint)
    return ExperimentLineage(
        spec=lineage["spec"],
        run=run,
        artifacts=list(lineage["artifacts"]),
        metrics=list(lineage["metrics"]),
        report=lineage["report"],
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# verify_research_artifacts — are the reported numbers still valid?
# ---------------------------------------------------------------------------


@dataclass
class ArtifactCheck:
    """Re-verification verdict for one recorded artifact."""

    artifact_id: str
    run_id: str
    path: str
    valid: bool


@dataclass
class MetricCheck:
    """Re-verification verdict for one recorded metric.

    A metric stays valid only while (a) its run's report passed, (b) the
    metric is listed in that report's ``verified_metrics``, and (c) its
    artifact still hashes to the recorded sha256. *reason* is empty for
    valid metrics and human-readable otherwise.
    """

    metric_id: str
    name: str
    run_id: str
    artifact_id: str
    valid: bool
    reason: str = ""


@dataclass
class VerificationSummary:
    """Aggregate re-verification result for the runs a paper depends on."""

    run_ids: list[str] = field(default_factory=list)
    artifacts: list[ArtifactCheck] = field(default_factory=list)
    metrics: list[MetricCheck] = field(default_factory=list)

    @property
    def all_valid(self) -> bool:
        """Whether every checked artifact and metric is still valid."""
        return all(item.valid for item in self.artifacts) and all(
            item.valid for item in self.metrics
        )

    @property
    def valid_metric_ids(self) -> list[str]:
        """Ids of the metrics that survived re-verification, in check order."""
        return [item.metric_id for item in self.metrics if item.valid]


def verify_research_artifacts(
    *,
    manifest_root: str | Path,
    run_ids: list[str] | None = None,
) -> VerificationSummary:
    """Re-verify the artifacts behind reported numbers.

    Recomputes every artifact hash and cross-checks every metric against
    its run's integrity report, so a post-hoc edit of any result file is
    caught before the numbers are quoted again.

    Args:
        manifest_root: Manifest store location.
        run_ids: Runs the report depends on (default: every run in the
            store).

    Returns:
        A :class:`VerificationSummary`. Nothing raises: invalid items are
        reported, not hidden.
    """
    store = ManifestStore(manifest_root)
    selected = list(run_ids) if run_ids is not None else store.list_run_ids()

    artifact_checks: list[ArtifactCheck] = []
    metric_checks: list[MetricCheck] = []

    for run_id in selected:
        lineage = store.load_lineage(run_id)
        report = lineage["report"]
        verified_metric_ids = (
            set(report.verified_metrics) if report is not None else set()
        )

        for artifact in lineage["artifacts"]:
            artifact_checks.append(
                ArtifactCheck(
                    artifact_id=artifact.artifact_id,
                    run_id=run_id,
                    path=artifact.path,
                    valid=verify_artifact(artifact),
                )
            )

        for metric in lineage["metrics"]:
            reason = ""
            if report is None:
                reason = f"no integrity report for run {run_id}"
            elif not report.passed:
                reason = f"run {run_id} failed integrity validation"
            elif metric.metric_id not in verified_metric_ids:
                reason = f"metric {metric.metric_id} not in report verified_metrics"
            else:
                artifact = store.load_artifact(metric.artifact_id)
                if not verify_artifact(artifact):
                    reason = (
                        f"artifact {metric.artifact_id} hash mismatch "
                        "(tampered, truncated, or deleted)"
                    )
            metric_checks.append(
                MetricCheck(
                    metric_id=metric.metric_id,
                    name=metric.name,
                    run_id=run_id,
                    artifact_id=metric.artifact_id,
                    valid=reason == "",
                    reason=reason,
                )
            )

    return VerificationSummary(
        run_ids=selected,
        artifacts=artifact_checks,
        metrics=metric_checks,
    )


# ---------------------------------------------------------------------------
# render_experiment_table — verified metrics only
# ---------------------------------------------------------------------------


class UnverifiedMetricError(ValueError):
    """Raised when a table render requests a metric that is not verified.

    Rendering a report table from unverified numbers is the exact failure
    this package exists to prevent, so it fails loudly instead of
    silently dropping the offending run.
    """


@dataclass
class TableStats:
    """Aggregated verified values for one (method, metric) table row."""

    method: str
    metric: str
    n: int
    mean: float
    std: float
    min: float
    max: float
    values: list[float] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    seeds: list[int | None] = field(default_factory=list)


def _verified_metric_value(
    store: ManifestStore,
    run_id: str,
    metric_name: str,
) -> tuple[MetricRecord, ArtifactRecord]:
    """Load one metric and re-verify it end to end.

    Returns:
        The metric record and its artifact record.

    Raises:
        UnverifiedMetricError: The metric is missing, its report failed or
            is absent, it is not in the report's verified list, or its
            artifact no longer hashes to the recorded value.
    """
    lineage = store.load_lineage(run_id)
    report = lineage["report"]
    matches = [m for m in lineage["metrics"] if m.name == metric_name]
    if not matches:
        raise UnverifiedMetricError(
            f"run {run_id} produced no metric named {metric_name!r}"
        )
    metric = matches[0]
    if report is None:
        raise UnverifiedMetricError(
            f"run {run_id} has no integrity report; metrics are unverified"
        )
    if not report.passed or metric.metric_id not in report.verified_metrics:
        raise UnverifiedMetricError(
            f"metric {metric_name!r} of run {run_id} is not verified "
            "(report failed or metric not in verified_metrics)"
        )
    artifact = store.load_artifact(metric.artifact_id)
    if not verify_artifact(artifact):
        raise UnverifiedMetricError(
            f"artifact {artifact.path} of run {run_id} no longer matches its "
            "recorded sha256; metric values are invalidated"
        )
    return metric, artifact


def render_experiment_table(
    *,
    method: str,
    metric: str,
    run_ids: list[str],
    manifest_root: str | Path,
    fmt: str = "latex",
    precision: int = 4,
) -> str:
    """Render one report table row from verified metrics only.

    Aggregates ``mean / std / min / max / n`` over *metric* across the
    given runs, re-verifying every value against its integrity report and
    artifact hash at render time.

    Args:
        method: Row label (the method being compared).
        metric: Metric name to aggregate.
        run_ids: The exact runs feeding the row.
        manifest_root: Manifest store location.
        fmt: ``"latex"`` (booktabs row ``method & mean±std & min & max & n``),
            ``"csv"`` or ``"json"``.
        precision: Decimal places for mean/std/min/max.

    Returns:
        The rendered row/string.

    Raises:
        UnverifiedMetricError: Any requested metric is not verifiable —
            report tables never render partial or unverified data.
        ValueError: Unknown *fmt*, or *run_ids* is empty.
    """
    if not run_ids:
        raise ValueError("run_ids must not be empty")
    if fmt not in ("latex", "csv", "json"):
        raise ValueError(f"unknown fmt {fmt!r} (expected latex | csv | json)")

    store = ManifestStore(manifest_root)
    values: list[float] = []
    metric_ids: list[str] = []
    seeds: list[int | None] = []
    provenance: dict[str, dict[str, str]] = {}

    for run_id in run_ids:
        record, artifact = _verified_metric_value(store, run_id, metric)
        values.append(record.value)
        metric_ids.append(record.metric_id)
        seeds.append(record.seed)
        provenance[record.metric_id] = {
            "run_id": run_id,
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.sha256,
            "source_path": record.source_path,
            "source_locator": record.source_locator,
            "value": str(record.value),
        }

    n = len(values)
    mean = sum(values) / n
    std = (
        (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5 if n > 1 else 0.0
    )
    stats = TableStats(
        method=method,
        metric=metric,
        n=n,
        mean=mean,
        std=std,
        min=min(values),
        max=max(values),
        values=values,
        metric_ids=metric_ids,
        run_ids=list(run_ids),
        seeds=seeds,
    )

    if fmt == "latex":
        return (
            f"{method} & {mean:.{precision}f} $\\pm$ {std:.{precision}f} & "
            f"{stats.min:.{precision}f} & {stats.max:.{precision}f} & {n} \\\\"
        )
    if fmt == "csv":
        return (
            f"method,metric,mean,std,min,max,n\n"
            f"{method},{metric},{mean:.{precision}f},{std:.{precision}f},"
            f"{stats.min:.{precision}f},{stats.max:.{precision}f},{n}"
        )
    # json — full stats plus the provenance chain behind every value, so a
    # rendered number can be traced back to its artifact and locator.
    payload: dict[str, Any] = {
        "method": method,
        "metric": metric,
        "n": n,
        "mean": round(mean, precision),
        "std": round(std, precision),
        "min": round(stats.min, precision),
        "max": round(stats.max, precision),
        "values": values,
        "seeds": seeds,
        "run_ids": list(run_ids),
        "metric_ids": metric_ids,
        "provenance": provenance,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


__all__ = [
    "ArtifactCheck",
    "ExperimentLineage",
    "ExperimentOutcome",
    "MetricCheck",
    "TableStats",
    "UnverifiedMetricError",
    "VerificationSummary",
    "inspect_experiment",
    "render_experiment_table",
    "run_research_experiment",
    "verify_research_artifacts",
]
