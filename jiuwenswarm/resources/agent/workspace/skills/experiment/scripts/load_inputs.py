"""Load and validate public requests and internal implementation manifests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contracts import ExperimentModuleInput
from io_utils import read_json, resolve_run_dir
from runtime_models import ImplementationManifest


DEFAULT_MANIFEST_NAME = "implementation-manifest.json"


def load_request(path: Path) -> ExperimentModuleInput:
    return ExperimentModuleInput.model_validate(read_json(path))


def validate_request(payload: ExperimentModuleInput | dict[str, Any]) -> ExperimentModuleInput:
    if isinstance(payload, ExperimentModuleInput):
        return payload
    return ExperimentModuleInput.model_validate(payload)


def default_manifest_path(
    request: ExperimentModuleInput,
    *,
    base_dir: Path | None = None,
) -> Path:
    run_dir = resolve_run_dir(request.execution_config.run_dir, base_dir=base_dir)
    return run_dir / DEFAULT_MANIFEST_NAME


def load_manifest(path: Path) -> ImplementationManifest:
    return ImplementationManifest.model_validate(read_json(path))
