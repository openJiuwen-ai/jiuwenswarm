"""Backward-compatible facade over the resumable ExperimentAgent coordinator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_coordinator import ExperimentAgentCoordinator
from contracts import ExperimentModuleInput, ExperimentModuleOutput
from prepare_data import DEFAULT_MAX_DOWNLOAD_BYTES


class ExperimentPipeline:
    """Run all four internal Agent stages in their required order."""

    def __init__(
        self,
        *,
        manifest_path: Path | None = None,
        allow_downloads: bool = True,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        base_dir: Path | None = None,
    ) -> None:
        self._coordinator = ExperimentAgentCoordinator(
            manifest_path=manifest_path,
            allow_downloads=allow_downloads,
            max_download_bytes=max_download_bytes,
            base_dir=base_dir,
        )

    def run(
        self,
        payload: ExperimentModuleInput | dict[str, Any],
    ) -> ExperimentModuleOutput:
        return self._coordinator.run_all(payload)


def run_experiment_module(
    payload: ExperimentModuleInput | dict[str, Any],
    *,
    manifest_path: Path | None = None,
    allow_downloads: bool = True,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    base_dir: Path | None = None,
) -> ExperimentModuleOutput:
    """Public Python entrypoint retained for existing module integrations."""

    return ExperimentPipeline(
        manifest_path=manifest_path,
        allow_downloads=allow_downloads,
        max_download_bytes=max_download_bytes,
        base_dir=base_dir,
    ).run(payload)
