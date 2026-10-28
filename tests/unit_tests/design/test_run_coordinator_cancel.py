# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RunCoordinator terminal-state handling on a run record no live trigger owns."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
from jiuwenswarm.server.runtime.design.run_coordinator import RunCoordinator


class _Store:
    def __init__(self, run: dict[str, Any]) -> None:
        self.runs = {run["run_id"]: run}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        return deepcopy(run) if run is not None else None

    def save_run(self, run: dict[str, Any]) -> dict[str, Any]:
        self.runs[run["run_id"]] = deepcopy(run)
        return run


def _zombie_run() -> dict[str, Any]:
    return {
        "run_id": "run_z",
        "graph_id": "g_z",
        "status": "running",
        "current_node_ids": ["n2"],
        "node_states": {
            "n1": {"status": "completed"},
            "n2": {"status": "running"},
        },
    }


def test_cancel_ends_a_run_left_running_by_a_dead_process():
    store = _Store(_zombie_run())
    coordinator = RunCoordinator(store, "g_z")

    result = coordinator.cancel("run_z")

    assert result["status"] == "cancelled"
    saved = store.runs["run_z"]
    assert saved["status"] == "cancelled"
    assert saved["current_node_ids"] == []
    assert saved["node_states"]["n1"]["status"] == "completed"
    assert saved["node_states"]["n2"]["status"] == "failed"


async def test_cancel_leaves_a_live_trigger_to_the_abort_signal():
    store = _Store(_zombie_run())
    coordinator = RunCoordinator(store, "g_z")

    async with coordinator._lock:
        result = coordinator.cancel("run_z")
        await asyncio.sleep(0)

    assert result["status"] == "running"
    assert store.runs["run_z"]["status"] == "running"


def _refused_graph():
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_refused",
        "project_id": "ut",
        "metadata": {"team_design_run_id": "run_refused"},
        "nodes": [
            {"id": "n_up", "type": "text", "label": "up",
             "config": {"prompt": "u", "pipeline": "brief"}},
            {"id": "n_down", "type": "text", "label": "down",
             "config": {"prompt": "d", "pipeline": "brief"}},
        ],
        "edges": [{"id": "e1", "source": "n_up", "target": "n_down"}],
    })


def _failed_run() -> dict[str, Any]:
    return {
        "run_id": "run_refused",
        "graph_id": "g_refused",
        "status": "failed",
        "current_node_ids": [],
        "node_states": {
            "n_up": {"status": "failed", "error": "boom"},
            "n_down": {"status": "failed", "error": "upstream node failed: n_up"},
        },
    }


async def test_refused_rerun_writes_failed_instead_of_staying_running():
    """A compile refusal must land on the run record, not escape the trigger.

    Regression: the coordinator seeded status='running' BEFORE compiling, so
    a single-node rerun refused by the fail-fast ('upstream not ready')
    crashed the background trigger and left the record 'running' forever —
    the frontend polled a trigger nobody owned and every further click
    re-seeded the same zombie.
    """
    store = _Store(_failed_run())
    coordinator = RunCoordinator(store, "g_refused")
    updates: list[dict[str, Any]] = []

    result = await coordinator.start(
        _refused_graph(), node_ids=["n_down"], on_update=updates.append
    )

    assert result["status"] == "failed"
    assert "upstream not ready: n_up" in str(result.get("error"))
    saved = store.runs["run_refused"]
    assert saved["status"] == "failed"
    assert saved["current_node_ids"] == []
    assert "upstream not ready: n_up" in str(saved.get("error"))
    assert any(u.get("status") == "failed" for u in updates)

