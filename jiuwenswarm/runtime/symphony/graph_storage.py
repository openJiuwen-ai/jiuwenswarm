"""Public Core graph artifact access for host-only presentation metadata."""

from __future__ import annotations

from pathlib import Path

from openjiuwen.symphony import GraphArtifactStore


def resolve_graph_artifact_dir(graph_dir: str | Path) -> Path:
    """Resolve a published version through the Core artifact contract."""
    root = Path(graph_dir).resolve()
    artifact = GraphArtifactStore(root).current_artifact()
    if artifact is None:
        raise FileNotFoundError("No published Symphony graph is available")
    return artifact.graph_path.parent


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

