"""Import the last published Swarm Skill Graph into the Core runtime."""

from __future__ import annotations

import logging
from os.path import lexists
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.symphony import GraphArtifactStore, SymphonyConfig
from openjiuwen.symphony.inventory import RuntimeStorage

from jiuwenswarm.common import paths

logger = logging.getLogger(__name__)

_LEGACY_SNAPSHOT_SCHEMA = "JiuwenSwarm-symphony-graph-source-v2"
_TARGET_RUNTIME_SCHEMA = "symphony.runtime.v1"


def migrate_legacy_graph(config: SymphonyConfig, *, legacy_graph_dir: Path | None = None) -> None:
    """Publish the prior Swarm graph during a workspace upgrade.

    Args:
        config: Active tenant and capability collection's Symphony configuration.
        legacy_graph_dir: Old graph path taken from the original configuration, when available.
    """
    skill_sources = [source for source in config.capability.sources if source.type == "skill"]
    if len(skill_sources) != 1:
        return
    storage = RuntimeStorage(config, skill_source=skill_sources[0])
    if storage.identity.get("schema_version") != _TARGET_RUNTIME_SCHEMA:
        return

    runtime_root = Path(config.storage.artifact_root)
    graph_dir = runtime_root / "graph"
    if not runtime_root.name or graph_dir.is_symlink():
        return
    binding = runtime_root / "runtime.json"
    pointer = graph_dir / "current.json"
    if lexists(binding) and lexists(pointer):
        return

    backup_dir = runtime_root.with_name(f".{runtime_root.name}-legacy-graph")
    legacy_dirs = [backup_dir]
    if legacy_graph_dir is not None:
        legacy_dirs.append(legacy_graph_dir)
    elif runtime_root.name == "runtime":
        old_root = runtime_root.parent
        default_root = paths.get_symphony_dir().resolve()
        legacy_dirs.append(old_root / "graph" if old_root == default_root else old_root)
    if not any((path / "current.json").is_file() for path in (graph_dir, *legacy_dirs)):
        return

    with FileLock(f"{backup_dir}.lock", timeout=30):
        bound = lexists(binding)
        if bound and lexists(pointer):
            return
        candidates = legacy_dirs if bound else [graph_dir, *legacy_dirs]
        for source_dir in candidates:
            payload = _read_legacy_graph(source_dir)
            if payload is not None:
                break
        else:
            return

        if source_dir == graph_dir:
            # Core must bind the empty runtime before the imported graph is published.
            # Keep the copied directory outside the runtime so an interrupted import can retry.
            if lexists(backup_dir):
                raise FileExistsError(f"Legacy Symphony graph backup already exists: {backup_dir}")
            graph_dir.rename(backup_dir)

        storage.check(write=True)
        store = GraphArtifactStore(graph_dir)
        if store.current_version() is not None:
            return
        artifact = store.stage(payload, version=f"legacy-{uuid4().hex}")
        try:
            store.activate_if_current(artifact, expected_version=None)
        except BaseError as exc:
            if not isinstance(exc.details, dict) or exc.details.get("mutation_code") != "stale_graph_version":
                raise


def _read_legacy_graph(graph_dir: Path) -> dict[str, Any] | None:
    if graph_dir.is_symlink():
        return None
    try:
        payload = GraphArtifactStore(graph_dir).read()
    except FileNotFoundError:
        return None
    except ValueError:
        logger.warning("Cannot read legacy Symphony graph in %s", graph_dir, exc_info=True)
        return None

    snapshot = payload.get("source_snapshot")
    if (
        payload.get("schema_version") != "1.0"
        or not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != _LEGACY_SNAPSHOT_SCHEMA
    ):
        return None
    if not isinstance(payload.get("generated_at"), str) or any(
        not isinstance(payload.get(key), dict) for key in ("config", "lookup")
    ) or any(
        not isinstance(payload.get(key), list) or any(not isinstance(item, dict) for item in payload[key])
        for key in ("capabilities", "nodes", "edges")
    ):
        logger.warning("Incomplete legacy Symphony graph in %s", graph_dir)
        return None
    return payload
