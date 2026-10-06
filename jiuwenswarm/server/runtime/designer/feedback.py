# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted Designer pipeline feedback (self/director ratings)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerGraphValidationError,
    storage_component,
)
from jiuwenswarm.common.utils import get_agent_root_dir

logger = logging.getLogger(__name__)

FEEDBACK_SCHEMA = "designer-feedback.v1"


def _feedback_dir(graph_id: str) -> Path | None:
    component = storage_component(graph_id)
    if component is None:
        return None
    root = (get_agent_root_dir() / "designer" / "feedback").resolve()
    path = root / component
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != root:
        return None
    return path


def feedback_path(graph_id: str, run_id: str) -> Path:
    directory = _feedback_dir(graph_id)
    component = storage_component(run_id)
    if directory is None or component is None:
        raise DesignerGraphValidationError(
            "graph_id and run_id must not contain path separators"
        )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{component}.json"
    if path.resolve().parent != directory.resolve():
        raise DesignerGraphValidationError("run_id must not contain path separators")
    return path


def latest_feedback_path(graph_id: str) -> Path | None:
    directory = _feedback_dir(graph_id)
    if directory is None or not directory.is_dir():
        return None
    # ``latest.json`` is a pointer file left behind by older versions.
    files = sorted(
        (p for p in directory.glob("*.json") if p.name != "latest.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def load_feedback(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load feedback %s: %s", path, exc)
        return None


def load_latest_feedback(graph_id: str) -> dict[str, Any] | None:
    return load_feedback(latest_feedback_path(graph_id))


def save_feedback(graph_id: str, run_id: str, payload: dict[str, Any]) -> Path:
    path = feedback_path(graph_id, run_id)
    tmp = path.with_suffix(".json.tmp")
    body = dict(payload)
    body.setdefault("schema_version", FEEDBACK_SCHEMA)
    body["graph_id"] = graph_id
    body["run_id"] = run_id
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def director_node_suggestions(feedback: dict[str, Any] | None) -> dict[str, Any]:
    """Per-node rerun notes. Trajectory stores them on director.suggestions.

    Reports written before that key existed nest the same map under
    director.final.suggestions.
    """
    if not isinstance(feedback, dict):
        return {}
    director = feedback.get("director") if isinstance(feedback.get("director"), dict) else {}
    top = director.get("suggestions")
    if isinstance(top, dict) and top:
        return top
    final = director.get("final") if isinstance(director.get("final"), dict) else {}
    nested = final.get("suggestions")
    if isinstance(nested, dict) and nested:
        return nested
    return top if isinstance(top, dict) else {}


def suggestion_for_node(feedback: dict[str, Any] | None, node_id: str) -> str:
    if not feedback:
        return ""
    suggestions = director_node_suggestions(feedback)
    if suggestions.get(node_id):
        return str(suggestions[node_id])
    agents = feedback.get("agents") or {}
    agent = agents.get(node_id) or {}
    if agent.get("suggestion_for_next"):
        return str(agent["suggestion_for_next"])
    final = feedback.get("final") or {}
    return str(final.get("improvement_plan") or "")
