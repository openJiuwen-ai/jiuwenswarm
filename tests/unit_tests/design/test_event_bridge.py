from __future__ import annotations

from copy import deepcopy
from typing import Any

from openjiuwen.agent_teams.workflow.engine.progress import ProgressKind, WorkflowProgressEvent

from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge


class _RecordingStore:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def save_run(self, run: dict[str, Any]) -> dict[str, Any]:
        self.saved.append(deepcopy(run))
        return run


def _bridge() -> tuple[DesignEventBridge, _RecordingStore, list[str | None]]:
    store = _RecordingStore()
    pushed: list[str | None] = []
    run = {"run_id": "run_1", "graph_id": "graph_1", "status": "running", "node_states": {}}
    bridge = DesignEventBridge(
        run,
        lambda _run, node_id=None: pushed.append(node_id),
        store=store,
        graph={"nodes": [{"id": "n_brief", "label": "Brief", "config": {"pipeline": "brief"}}]},
    )
    return bridge, store, pushed


def test_node_status_changes_are_persisted_for_run_polling():
    bridge, store, pushed = _bridge()

    bridge.emit(WorkflowProgressEvent(kind=ProgressKind.AGENT_STARTED, label="n_brief"))
    assert store.saved[-1]["node_states"]["n_brief"]["status"] == "running"
    assert "n_brief" in pushed

    bridge.emit(WorkflowProgressEvent(kind=ProgressKind.AGENT_COMPLETED, label="n_brief"))
    assert store.saved[-1]["node_states"]["n_brief"]["status"] == "completed"


def test_node_failure_is_persisted_with_its_error():
    bridge, store, _pushed = _bridge()

    bridge.emit(WorkflowProgressEvent(kind=ProgressKind.AGENT_STARTED, label="n_brief"))
    bridge.emit(
        WorkflowProgressEvent(kind=ProgressKind.AGENT_FAILED, label="n_brief", message="model timeout")
    )

    state = store.saved[-1]["node_states"]["n_brief"]
    assert state["status"] == "failed"
    assert state["error"] == "model timeout"
