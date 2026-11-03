"""Host build-run metadata and public Core graph artifact access."""

from __future__ import annotations

import json
from pathlib import Path

from openjiuwen.symphony import GraphArtifactStore

CURRENT_POINTER_FILENAME = "current.json"
BUILD_RUNS_DIRNAME = ".build_runs"


def resolve_graph_artifact_dir(graph_dir: str | Path) -> Path:
    """Resolve the published artifact through Core, retaining flat metadata reads."""
    root = Path(graph_dir).resolve()
    artifact = GraphArtifactStore(root).current_artifact()
    return artifact.graph_path.parent if artifact is not None else root


def graph_exists(graph_dir: str | Path) -> bool:
    """Report only graph versions accepted by the public Core artifact contract."""
    try:
        store = GraphArtifactStore(graph_dir)
        artifact = store.current_artifact()
        if artifact is None:
            return False
        store.read(artifact.version)
    except (OSError, ValueError):
        return False
    return True


def build_runs_dir(graph_dir: str | Path) -> Path:
    return Path(graph_dir).resolve() / BUILD_RUNS_DIRNAME


def build_run_dir(graph_dir: str | Path, run_id: str) -> Path:
    return build_runs_dir(graph_dir) / run_id


def latest_incomplete_build(graph_dir: str | Path) -> Path | None:
    runs_root = build_runs_dir(graph_dir)
    if not runs_root.is_dir():
        return None
    candidates = []
    for child in runs_root.iterdir():
        checkpoint = child / "checkpoint.json"
        if not checkpoint.is_file():
            continue
        try:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        candidates.append(
            (
                str(payload.get("updated_at") or ""),
                str(payload.get("status") or ""),
                child,
            )
        )
    if not candidates:
        return None
    _updated_at, status, path = sorted(candidates, key=lambda item: item[0])[-1]
    if status in {"running", "failed"}:
        return path
    return None


