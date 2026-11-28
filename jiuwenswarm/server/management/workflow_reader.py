# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read existing workflow state without constructing a Team execution runtime."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.team_runtime_registry import get_existing_team_manager

from jiuwenswarm.server.management.workflow_state import WorkflowRunState

logger = logging.getLogger(__name__)


def existing_workflow_handler(session_id: str) -> Any:
    """Borrow a handler only from an already active Team manager.

    Reading UI lists cannot activate the SDK or install an execution owner.
    TeamManager has one process-local singleton; its accessor would create it.
    """
    manager = get_existing_team_manager()
    return manager.get_workflow_handler(session_id) if manager is not None else None


def restored_workflows(session_id: str) -> list[dict[str, Any]]:
    """Return the same validated, recoverable persisted view as Team startup."""
    from jiuwenswarm.server.runtime.session.session_metadata import (
        _enqueue_write,
        _read_metadata,
    )

    metadata = _read_metadata(session_id, cache_bust=True)
    rows = metadata.get("workflow_runs")
    if not rows:
        return []
    runs = {
        run_id: WorkflowRunState.model_validate(row) for run_id, row in rows.items()
    }
    changed = False
    for run in runs.values():
        if not run.is_terminal:
            if run.status != "paused":
                run.status = "paused"
                changed = True
            if not run.recovered:
                run.recovered = True
                changed = True
    if changed:
        # Keep all metadata fields (including the session budget) in the same
        # read-modify-write, matching Team recovery's persistence contract.
        if metadata.get("session_id"):
            metadata["workflow_runs"] = {
                run_id: run.model_dump() for run_id, run in runs.items()
            }
            _enqueue_write(
                session_id, metadata, merge_fields=frozenset({"workflow_runs"})
            )
        else:
            logger.warning(
                "Workflow recovery skipped invalid session metadata: %s", session_id
            )
    return [run.to_workflow_run_dict() for run in runs.values()]
