"""Workspace references that a Symphony planning call may pass to Core."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openjiuwen.core.sys_operation.cwd import get_cwd, get_project_root, get_workspace

PLANNING_INPUTS_ATTR = "_symphony_available_planning_inputs"
DISPATCH_GRAPH_ATTR = "_symphony_dispatch_graph"
PLANNING_INPUTS_SCHEMA = {
    "type": "array",
    "description": (
        "Pass named workspace file paths or confirmed facts needed by individual tasks. "
        "Omit when the request is self-contained."
    ),
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique name for this input."},
            "kind": {"type": "string", "enum": ["text", "file"]},
            "value": {"type": "string", "description": "Known text or a readable file path in the current workspace."},
            "description": {"type": "string", "description": "Optional purpose or version of this input."},
        },
        "required": ["name", "kind", "value"],
        "additionalProperties": False,
    },
}


def supports_planning_inputs() -> bool:
    import openjiuwen.symphony as core_symphony

    return hasattr(core_symphony, "PlanningInput")


def prepare_planning_inputs(value: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Validate input data and canonicalize readable workspace file references."""
    from openjiuwen.symphony import PlanningInput

    available = []
    unavailable = []
    for item in PlanningInput.validate_many(value):
        if item.kind == "file":
            path = _workspace_file(item.value)
            if path is None:
                unavailable.append({"name": item.name, "kind": "file", "description": (
                    "The file reference is not readable in the current workspace."
                )})
                continue
            item = item.model_copy(update={"value": str(path)})
        available.append(item.model_dump(exclude_none=True))
    return available, unavailable


def _workspace_file(value: str) -> Path | None:
    workspace = get_workspace()
    if not workspace:
        return None
    try:
        path = (Path(get_cwd()) / Path(value)).resolve()
        roots = (get_project_root(), workspace)
        if any(path.is_relative_to(Path(root).resolve()) for root in roots):
            if path.is_file() and os.access(path, os.R_OK):
                return path
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def annotate_unavailable_inputs(result: Any, unavailable: list[dict[str, str]]) -> Any:
    if not unavailable or not isinstance(result, dict) or result.get("success") is not True:
        return result
    planned_graph = result.get("planned_graph")
    graph = planned_graph.get("graph") if isinstance(planned_graph, dict) else None
    metadata = graph.get("metadata") if isinstance(graph, dict) else None
    if not isinstance(metadata, dict):
        return result
    reported = metadata.get("missing_inputs") or []
    if metadata.get("plan_kind") == "expert_tasks":
        by_name = {
            hint["name"]: hint
            for hint in reported
            if isinstance(hint, dict) and isinstance(hint.get("name"), str)
        }
        by_name.update({hint["name"]: hint for hint in unavailable})
        missing_inputs = list(by_name.values())
    else:
        missing_inputs = list(dict.fromkeys([
            *(name for name in reported if isinstance(name, str)),
            *(hint["name"] for hint in unavailable),
        ]))
    return {
        **result,
        "planned_graph": {
            **planned_graph,
            "graph": {**graph, "metadata": {**metadata, "missing_inputs": missing_inputs}},
        },
    }
