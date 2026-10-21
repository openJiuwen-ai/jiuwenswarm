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


def test_first_result_writes_current_slots_and_double_writes_refs():
    bridge, store, _pushed = _bridge()
    ref_a = {"uri": "file:///a.png"}

    bridge.on_node_result("n_brief", {"text": "v1", "output_ref": dict(ref_a)})

    state = store.saved[-1]["node_states"]["n_brief"]
    assert state["text"] == "v1"
    assert state["output_ref"] == ref_a
    # A single-artifact submit also lands in output_refs: list-first
    # downstream readers must never see a stale set from an earlier version.
    assert state["output_refs"] == [ref_a]
    # First version: nothing to archive yet.
    assert state.get("history", []) == []


def test_rerun_archives_previous_version_and_replaces_refs():
    bridge, store, _pushed = _bridge()
    ref_a = {"uri": "file:///a.png"}
    ref_a2 = {"uri": "file:///a2.png"}
    ref_b = {"uri": "file:///b.png"}

    bridge.on_node_result(
        "n_brief",
        {"text": "v1", "output_ref": dict(ref_a), "output_refs": [dict(ref_a), dict(ref_a2)]},
    )
    bridge.on_node_result("n_brief", {"text": "v2", "output_ref": dict(ref_b)})

    state = store.saved[-1]["node_states"]["n_brief"]
    assert state["text"] == "v2"
    assert state["output_ref"] == ref_b
    # Full replacement: the v2 single artifact must be the only ref — the
    # stale a2 from v1 is exactly the old scarf-video bug.
    assert state["output_refs"] == [ref_b]
    # Old-version log: history[-1] is the TRUE previous version.
    assert len(state["history"]) == 1
    assert state["history"][-1]["text"] == "v1"
    assert state["history"][-1]["output_ref"] == ref_a


def test_text_only_rerun_over_media_node_archives_old_ref():
    bridge, store, _pushed = _bridge()
    ref_a = {"uri": "file:///a.png"}

    bridge.on_node_result("n_brief", {"text": "v1", "output_ref": dict(ref_a)})
    bridge.on_node_result("n_brief", {"text": "v2"})

    state = store.saved[-1]["node_states"]["n_brief"]
    assert state["text"] == "v2"
    # Per-field write: no new ref in the product, the current ref is kept.
    assert state["output_ref"] == ref_a
    assert state["history"][-1]["text"] == "v1"
    assert state["history"][-1]["output_ref"] == ref_a
