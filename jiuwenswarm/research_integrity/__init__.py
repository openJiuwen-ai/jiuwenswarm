# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Experiment integrity and artifact lineage for scientific agent workflows.

This package answers one question: *did this reported number really come from
a reproducible experiment?*

Provenance chain::

    ExperimentSpec      what will run, what artifacts/metrics are expected
        -> EnvironmentFingerprint   under which environment
        -> ExperimentRun            one real process execution
            -> ArtifactRecord       hashed output files
                -> MetricRecord     deterministically parsed numbers
                    -> IntegrityReport  pass/fail verdict

Public API: :func:`run_research_experiment` executes the whole chain;
:class:`~jiuwenswarm.research_integrity.manifest.ManifestStore` persists it;
:func:`~jiuwenswarm.research_integrity.validator.validate_integrity` judges it.
"""

from jiuwenswarm.research_integrity.artifact_tracker import (
    discover_artifacts,
    record_artifact,
    sha256_file,
    verify_artifact,
)
from jiuwenswarm.research_integrity.fingerprint import (
    EnvironmentFingerprint,
    capture_environment_fingerprint,
)
from jiuwenswarm.research_integrity.manifest import (
    DEFAULT_MANIFEST_DIR,
    ManifestStore,
)
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
    MetricSpec,
)
from jiuwenswarm.research_integrity.tools import (
    ArtifactCheck,
    ExperimentLineage,
    ExperimentOutcome,
    MetricCheck,
    TableStats,
    UnverifiedMetricError,
    VerificationSummary,
    inspect_experiment,
    render_experiment_table,
    run_research_experiment,
    verify_research_artifacts,
)
from jiuwenswarm.research_integrity.validator import (
    DEFAULT_POLICY,
    ValidationPolicy,
    validate_integrity,
)

__all__ = [
    "ArtifactCheck",
    "ArtifactRecord",
    "DEFAULT_MANIFEST_DIR",
    "DEFAULT_POLICY",
    "EnvironmentFingerprint",
    "ExperimentLineage",
    "ExperimentOutcome",
    "ExperimentRun",
    "ExperimentSpec",
    "IntegrityIssue",
    "IntegrityReport",
    "ManifestStore",
    "MetricCheck",
    "MetricParseError",
    "MetricRecord",
    "MetricSpec",
    "TableStats",
    "UnverifiedMetricError",
    "ValidationPolicy",
    "VerificationSummary",
    "capture_environment_fingerprint",
    "discover_artifacts",
    "inspect_experiment",
    "parse_metric",
    "record_artifact",
    "render_experiment_table",
    "run_research_experiment",
    "sha256_file",
    "validate_integrity",
    "verify_artifact",
    "verify_research_artifacts",
]
