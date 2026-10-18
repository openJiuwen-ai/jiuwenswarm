# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RunCoordinator.cancel on a run record no live trigger owns."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

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
    coordinator = RunCoordinator(store, "g_z", spawner=None)

    result = coordinator.cancel("run_z")

    assert result["status"] == "cancelled"
    saved = store.runs["run_z"]
    assert saved["status"] == "cancelled"
    assert saved["current_node_ids"] == []
    assert saved["node_states"]["n1"]["status"] == "completed"
    assert saved["node_states"]["n2"]["status"] == "failed"


async def test_cancel_leaves_a_live_trigger_to_the_abort_signal():
    store = _Store(_zombie_run())
    coordinator = RunCoordinator(store, "g_z", spawner=None)

    async with coordinator._lock:
        result = coordinator.cancel("run_z")
        await asyncio.sleep(0)

    assert result["status"] == "running"
    assert store.runs["run_z"]["status"] == "running"
