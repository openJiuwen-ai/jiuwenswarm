# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistence for Designer execution graphs and run state.

Storage layout under ``get_agent_root_dir()/designer/``:

- ``graphs/{graph_id}.json`` — domain graph (schema_version designer-execution-graph.v1)
- ``runs/{run_id}.json`` — run state (schema_version designer-execution-run.v1)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphValidationError,
    normalize_execution_graph,
    normalize_execution_run,
    preserve_expanded_shot_nodes,
    preserve_nodes_added_since,
    preserve_node_output_refs,
    storage_component,
    utc_now_ms,
)
from jiuwenswarm.common.utils import get_agent_root_dir

logger = logging.getLogger(__name__)

_STORE_LOCK = threading.Lock()


def _designer_root() -> Path:
    root = get_agent_root_dir() / "designer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _graphs_dir() -> Path:
    path = _designer_root() / "graphs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _runs_dir() -> Path:
    path = _designer_root() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(directory: Path, storage_id: str) -> Path | None:
    """``directory/{storage_id}.json`` when the id cannot leave ``directory``."""
    component = storage_component(storage_id)
    if component is None:
        return None
    root = directory.resolve()
    path = root / f"{component}.json"
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != root:
        return None
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON in {path}")
    return data


def reload_graph_inplace(graph: dict[str, Any] | None) -> bool:
    """Replace ``graph`` with the copy last saved from the canvas.

    Node agents keep the dict they were started with. Canvas edits land in the
    store file, so tools re-read that file into the same dict before they run.
    """
    if not isinstance(graph, dict):
        return False
    graph_id = str(graph.get("graph_id") or "").strip()
    if not graph_id:
        return False
    fresh = DesignerGraphStore().get_graph(graph_id)
    if not isinstance(fresh, dict):
        return False
    graph.clear()
    graph.update(fresh)
    return True


class DesignerGraphStore:
    """File-backed store for Designer graphs and execution runs."""

    def save_graph(self, graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
        normalized = normalize_execution_graph(graph)
        path = _record_path(_graphs_dir(), normalized["graph_id"])
        if path is None:
            raise DesignerGraphValidationError("graph_id must not contain path separators")
        with _STORE_LOCK:
            if path.is_file():
                try:
                    existing = normalize_execution_graph(_read_json(path))
                    normalized = preserve_expanded_shot_nodes(normalized, existing)
                    normalized = preserve_nodes_added_since(normalized, existing)
                    normalized = preserve_node_output_refs(normalized, existing)
                    from jiuwenswarm.server.runtime.designer.user_references import (
                        reapply_user_reference_routes,
                    )

                    normalized = reapply_user_reference_routes(normalized, existing) or normalized
                except (DesignerGraphValidationError, ValueError, json.JSONDecodeError, OSError):
                    pass
            normalized["updated_at"] = utc_now_ms()
            _atomic_write_json(path, dict(normalized))
        return normalized

    def get_graph(self, graph_id: str) -> DesignerExecutionGraph | None:
        path = _record_path(_graphs_dir(), str(graph_id or ""))
        if path is None or not path.is_file():
            return None
        with _STORE_LOCK:
            try:
                raw = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read designer graph %s: %s", graph_id, exc)
                return None
        try:
            return normalize_execution_graph(raw)
        except DesignerGraphValidationError as exc:
            logger.warning("Invalid designer graph %s: %s", graph_id, exc)
            return None

    def list_graphs(self, project_id: str | None = None) -> list[DesignerExecutionGraph]:
        wanted = str(project_id or "").strip()
        graphs: list[DesignerExecutionGraph] = []
        with _STORE_LOCK:
            for path in sorted(_graphs_dir().glob("*.json")):
                try:
                    raw = _read_json(path)
                    graph = normalize_execution_graph(raw)
                except (DesignerGraphValidationError, ValueError, json.JSONDecodeError):
                    continue
                if wanted and graph.get("project_id") != wanted:
                    continue
                graphs.append(graph)
        graphs.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return graphs

    def list_graphs_for_project(self, project_id: str) -> list[DesignerExecutionGraph]:
        project_id = str(project_id or "").strip()
        if not project_id:
            return []
        return self.list_graphs(project_id)

    def delete_graph(self, graph_id: str) -> bool:
        """Delete a graph created by an uncommitted workspace transaction."""
        path = _record_path(_graphs_dir(), str(graph_id or ""))
        if path is None:
            return False
        with _STORE_LOCK:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
        return True

    def save_run(self, run: DesignerExecutionRun) -> DesignerExecutionRun:
        normalized = normalize_execution_run(run)
        normalized["updated_at"] = utc_now_ms()
        path = _record_path(_runs_dir(), normalized["run_id"])
        if path is None:
            raise DesignerGraphValidationError("run_id must not contain path separators")
        with _STORE_LOCK:
            _atomic_write_json(path, dict(normalized))
        return normalized

    def get_run(self, run_id: str) -> DesignerExecutionRun | None:
        path = _record_path(_runs_dir(), str(run_id or ""))
        if path is None or not path.is_file():
            return None
        with _STORE_LOCK:
            try:
                raw = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read designer run %s: %s", run_id, exc)
                return None
        try:
            return normalize_execution_run(raw)
        except DesignerGraphValidationError as exc:
            logger.warning("Invalid designer run %s: %s", run_id, exc)
            return None

    def get_latest_run_for_graph(self, graph_id: str) -> DesignerExecutionRun | None:
        graph_id = str(graph_id or "").strip()
        if not graph_id:
            return None
        latest: DesignerExecutionRun | None = None
        latest_ts = -1
        with _STORE_LOCK:
            for path in _runs_dir().glob("*.json"):
                try:
                    raw = _read_json(path)
                    run = normalize_execution_run(raw)
                except (DesignerGraphValidationError, ValueError, json.JSONDecodeError):
                    continue
                if run.get("graph_id") != graph_id:
                    continue
                ts = int(run.get("updated_at") or 0)
                if ts >= latest_ts:
                    latest = run
                    latest_ts = ts
        return latest
