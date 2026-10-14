# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest persistence: every integrity record stored as addressable JSON.

Layout under the manifest root (default ``.jiuwen/research_integrity``)::

    <root>/
    ├── fingerprints/<fingerprint_id>.json
    ├── specs/<experiment_id>.json
    ├── runs/<run_id>.json
    ├── artifacts/<artifact_id>.json
    ├── metrics/<metric_id>.json
    └── reports/<run_id>.json

Writes are atomic (tmp file + os.replace) so a crash mid-write can never
corrupt an existing manifest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict, TypeVar

from pydantic import BaseModel

from jiuwenswarm.research_integrity.fingerprint import EnvironmentFingerprint
from jiuwenswarm.research_integrity.schemas import (
    ArtifactRecord,
    ExperimentRun,
    ExperimentSpec,
    IntegrityReport,
    MetricRecord,
)

DEFAULT_MANIFEST_DIR = ".jiuwen/research_integrity"

_M = TypeVar("_M", bound=BaseModel)


class ExperimentLineage(TypedDict):
    """Full lineage of one run, as loaded by :meth:`ManifestStore.load_lineage`."""

    spec: ExperimentSpec
    run: ExperimentRun
    artifacts: list[ArtifactRecord]
    metrics: list[MetricRecord]
    report: IntegrityReport | None


class ManifestStore:
    """JSON file store for integrity records, keyed by record id."""

    def __init__(self, root: str | Path) -> None:
        """Create/open a manifest store rooted at *root*.

        Args:
            root: Directory that will contain the record subdirectories.
        """
        self.root = Path(root)
        for sub in (
            "fingerprints",
            "specs",
            "runs",
            "artifacts",
            "metrics",
            "reports",
        ):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: Path, payload: str) -> None:
        """Write *payload* to *path* atomically (tmp + replace)."""
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)

    def _save(self, subdir: str, record_id: str, record: _M) -> _M:
        path = self.root / subdir / f"{record_id}.json"
        self._atomic_write_json(path, record.model_dump_json(indent=2))
        return record

    def _load(self, subdir: str, record_id: str, model_cls: type[_M]) -> _M:
        path = self.root / subdir / f"{record_id}.json"
        if not path.is_file():
            raise KeyError(f"{model_cls.__name__} {record_id!r} not found in {path}")
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))

    def _load_or_none(
        self, subdir: str, record_id: str, model_cls: type[_M]
    ) -> _M | None:
        try:
            return self._load(subdir, record_id, model_cls)
        except KeyError:
            return None

    def _ids(self, subdir: str) -> list[str]:
        directory = self.root / subdir
        return sorted(
            path.stem for path in directory.glob("*.json") if not path.name.endswith(".tmp")
        )

    # -- save API ----------------------------------------------------------

    def save_fingerprint(self, fingerprint: EnvironmentFingerprint) -> None:
        """Persist an environment fingerprint (id-addressed, deduped by id)."""
        self._save("fingerprints", fingerprint.fingerprint_id, fingerprint)

    def save_spec(self, spec: ExperimentSpec) -> None:
        """Persist an experiment spec (one per experiment_id, overwrite)."""
        self._save("specs", spec.experiment_id, spec)

    def save_run(self, run: ExperimentRun) -> None:
        """Persist an experiment run."""
        self._save("runs", run.run_id, run)

    def save_artifact(self, artifact: ArtifactRecord) -> None:
        """Persist an artifact record."""
        self._save("artifacts", artifact.artifact_id, artifact)

    def save_metric(self, metric: MetricRecord) -> None:
        """Persist a metric record."""
        self._save("metrics", metric.metric_id, metric)

    def save_report(self, report: IntegrityReport) -> None:
        """Persist an integrity report (latest report per run wins)."""
        self._save("reports", report.run_id, report)

    # -- load API ----------------------------------------------------------

    def load_spec(self, experiment_id: str) -> ExperimentSpec:
        """Load the spec for *experiment_id*."""
        return self._load("specs", experiment_id, ExperimentSpec)

    def load_run(self, run_id: str) -> ExperimentRun:
        """Load the run record *run_id*."""
        return self._load("runs", run_id, ExperimentRun)

    def load_artifact(self, artifact_id: str) -> ArtifactRecord:
        """Load the artifact record *artifact_id*."""
        return self._load("artifacts", artifact_id, ArtifactRecord)

    def load_metric(self, metric_id: str) -> MetricRecord:
        """Load the metric record *metric_id*."""
        return self._load("metrics", metric_id, MetricRecord)

    def load_report(self, run_id: str) -> IntegrityReport | None:
        """Load the latest integrity report for *run_id* (``None`` if absent)."""
        return self._load_or_none("reports", run_id, IntegrityReport)

    def load_fingerprint(self, fingerprint_id: str) -> EnvironmentFingerprint | None:
        """Load an environment fingerprint by id (``None`` if absent)."""
        return self._load_or_none("fingerprints", fingerprint_id, EnvironmentFingerprint)

    # -- listing API -------------------------------------------------------

    def list_spec_ids(self) -> list[str]:
        """All persisted experiment ids."""
        return self._ids("specs")

    def list_run_ids(self) -> list[str]:
        """All persisted run ids."""
        return self._ids("runs")

    def list_artifact_ids(self) -> list[str]:
        """All persisted artifact ids."""
        return self._ids("artifacts")

    def list_metric_ids(self) -> list[str]:
        """All persisted metric ids."""
        return self._ids("metrics")

    def load_lineage(self, run_id: str) -> ExperimentLineage:
        """Load the full lineage of *run_id*: spec, run, artifacts, metrics.

        Returns:
            A dict with keys ``spec``, ``run``, ``artifacts``, ``metrics``
            and ``report`` (report is ``None`` when not yet validated).
            Artifacts are ordered by path and metrics by name, so the
            result is deterministic across reloads.
        """
        run = self.load_run(run_id)
        artifacts = sorted(
            (
                record
                for record in map(self.load_artifact, self.list_artifact_ids())
                if record.run_id == run_id
            ),
            key=lambda record: record.path,
        )
        metrics = sorted(
            (
                record
                for record in map(self.load_metric, self.list_metric_ids())
                if record.run_id == run_id
            ),
            key=lambda record: record.name,
        )
        return {
            "spec": self.load_spec(run.experiment_id),
            "run": run,
            "artifacts": artifacts,
            "metrics": metrics,
            "report": self.load_report(run_id),
        }


__all__ = ["DEFAULT_MANIFEST_DIR", "ExperimentLineage", "ManifestStore"]
