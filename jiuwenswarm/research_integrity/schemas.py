# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Experiment integrity schemas.

Record types capturing the full provenance chain of a scientific experiment:

    ExperimentSpec          (declared before execution)
        -> ExperimentRun        (one real process execution)
            -> ArtifactRecord   (hashed output files)
                -> MetricRecord (deterministically parsed numbers)
                    -> IntegrityReport (validation verdict)

The chain is intentionally dependency-free (pydantic only) so the package can
be unit-tested and reused without the rest of the harness.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Record(BaseModel):
    """Base record: strict, immutable-by-convention, JSON round-trippable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricSpec(_Record):
    """Declarative description of one metric to extract from an artifact.

    Attributes:
        name: Metric name as it will appear in reports or published tables.
        source: Path of the artifact file the metric is read from
            (absolute, or relative to the experiment cwd).
        locator: Deterministic locator inside the artifact. Supported forms:

            - JSON: ``$.accuracy`` or ``$.results[0].score``
            - CSV: ``row=method_a,column=accuracy`` (row key in the first
              column matched against the column named ``column``)
            - JSONL: ``jsonl[line=14].score`` (0-based line index)
    """

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    locator: str = Field(min_length=1)


class ExperimentSpec(_Record):
    """A declared experiment: what to run and what to expect.

    Must be created (and persisted) *before* execution so that expectations
    cannot be retrofitted to whatever the run happened to produce.
    """

    experiment_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    hypothesis_id: str = ""
    command: str = Field(min_length=1)
    cwd: str = "."
    seed: int | None = None
    config_path: str | None = None
    dataset_paths: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    metric_specs: list[MetricSpec] = Field(default_factory=list)

    @field_validator("command")
    @classmethod
    def _reject_blank_command(cls, value: str) -> str:
        """A whitespace-only command is not a runnable experiment."""
        if not value.strip():
            raise ValueError("command must not be blank")
        return value


class ExperimentRun(_Record):
    """One real execution of an :class:`ExperimentSpec` command."""

    run_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    started_at: str = Field(min_length=1)  # ISO-8601 UTC
    finished_at: str = Field(min_length=1)  # ISO-8601 UTC
    exit_code: int = 0
    stdout_path: str | None = None
    stderr_path: str | None = None
    environment_fingerprint: str = Field(min_length=1)
    success: bool = True


class ArtifactRecord(_Record):
    """A hashed output file produced by an experiment run."""

    artifact_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    kind: str = Field(min_length=1)  # json | csv | jsonl | txt | other


class MetricRecord(_Record):
    """One metric value extracted deterministically from an artifact.

    The ``source_locator`` records *where inside the artifact* the value came
    from, so any paper number can be traced back to an exact file position.
    """

    metric_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: float
    source_path: str = Field(min_length=1)
    source_locator: str = Field(min_length=1)
    seed: int | None = None


class IntegrityIssue(_Record):
    """A single integrity violation found during validation.

    Attributes:
        code: Stable machine-readable issue code (see validator for the list).
        message: Human-readable explanation.
        details: Optional structured context for debugging / logging.
    """

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class IntegrityReport(_Record):
    """Validation verdict for one experiment run.

    ``passed`` is true only when the run produced no issues; published tables
    may only consume metrics whose run report passed.
    """

    run_id: str = Field(min_length=1)
    passed: bool
    issues: list[IntegrityIssue] = Field(default_factory=list)
    verified_metrics: list[str] = Field(default_factory=list)
    verified_artifacts: list[str] = Field(default_factory=list)


__all__ = [
    "MetricSpec",
    "ExperimentSpec",
    "ExperimentRun",
    "ArtifactRecord",
    "MetricRecord",
    "IntegrityIssue",
    "IntegrityReport",
]
