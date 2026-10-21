from __future__ import annotations

from copy import deepcopy
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["designer.graph.updated", "designer.run.updated", "designer.node.updated", "designer.leader.activity"])
@pytest.mark.parametrize("channel", ["video_tool", "web"])
async def test_voice_design_canvas_events_reach_web_session(monkeypatch, event, channel):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.design.team_bridge import _push_team_event

    host = SimpleNamespace(send_push=AsyncMock())
    monkeypatch.setattr(AgentWebSocketServer, "get_instance", lambda: host)
    payload = {"event_type": event, "session_id": "design-chat", "graph": {"graph_id": "g"}}
    await _push_team_event(channel, "design-chat", payload, "original-turn")
    frame = host.send_push.call_args.args[0]
    assert frame["channel_id"] == "web"
    assert frame["session_id"] == "design-chat"
    assert frame["request_id"] == "original-turn"
    assert frame["payload"] is payload
