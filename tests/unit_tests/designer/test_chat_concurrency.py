# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A chat turn must not silently overwrite another writer.

``_chat_graph`` plans against the graph it read and then saves a graph derived
from that snapshot. The design branch guarded this until e29021fac ("add asset
into chat box") dropped the guards together with the chat-document conflict
machinery, which left two concurrent turns on one graph free to overwrite each
other — the loser's edit vanishing with no error.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.designer import leader_chat as leader_chat_module
from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter

GRAPH_ID = "graph_concurrent"


class _RecordingStore:
    """A store the test owns, so nothing touches the real ~/.jiuwenswarm."""

    def __init__(self, graph: dict) -> None:
        self.graph = graph
        self.saved = 0

    def get_graph(self, graph_id: str) -> dict:
        return self.graph

    def get_latest_run_for_graph(self, graph_id: str) -> None:
        return None

    def save_graph(self, graph: dict) -> dict:
        self.saved += 1
        self.graph = graph
        return graph


def _graph() -> dict:
    return {
        "graph_id": GRAPH_ID,
        "project_id": "proj_concurrent",
        "metadata": {},
        "nodes": [{"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip"}}],
        "edges": [],
    }


def _prepare(monkeypatch: pytest.MonkeyPatch, graph: dict) -> _RecordingStore:
    store = _RecordingStore(graph)
    monkeypatch.setattr(designer_adapter, "_store", store)
    monkeypatch.setattr(
        designer_adapter._executor, "reconcile_loaded_graph", lambda loaded: loaded
    )
    # These cases are about the lost-update half; the active-run guard has its
    # own test, and it reads shared executor state these cases should not depend on.
    monkeypatch.setattr(
        designer_adapter._executor, "has_active_tasks", lambda graph_id: False
    )
    return store


def _request() -> AgentRequest:
    return AgentRequest(request_id="request", channel_id="web")


@pytest.mark.asyncio
async def test_a_concurrent_graph_write_is_refused_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two _chat_graph calls on one graph: the loser must not clobber the winner."""
    original = _graph()
    store = _prepare(monkeypatch, original)

    async def fake_leader_chat(graph, *args, **kwargs):
        # A second writer lands while this turn's leader call is in flight —
        # another chat turn, or the canvas autosaving a user edit.
        store.graph = {
            **original,
            "nodes": [
                *original["nodes"],
                {"id": "n_scene_1", "type": "image", "config": {"pipeline": "scene"}},
            ],
        }
        return {
            "graph": {**original, "metadata": {"touched": True}},
            "changed": True,
            "summary": "加上场景设定图。",
            "run_node_ids": [],
        }

    monkeypatch.setattr(leader_chat_module, "run_leader_chat", fake_leader_chat)

    payload, error, code = await designer_adapter._chat_graph(
        _request(), {"graph_id": GRAPH_ID, "message": "加上场景"}
    )

    assert payload is None
    assert code == "CONFLICT"
    assert "发生变化" in (error or "")
    assert store.saved == 0
    # The other writer's node is still there: nothing was reverted.
    assert [node["id"] for node in store.graph["nodes"]] == ["n_clip_1", "n_scene_1"]


@pytest.mark.asyncio
async def test_a_turn_with_no_other_writer_still_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not refuse ordinary turns."""
    original = _graph()
    store = _prepare(monkeypatch, original)

    async def fake_leader_chat(graph, *args, **kwargs):
        return {
            "graph": {**original, "metadata": {"touched": True}},
            "changed": True,
            "summary": "改好了。",
            "run_node_ids": [],
        }

    monkeypatch.setattr(leader_chat_module, "run_leader_chat", fake_leader_chat)

    payload, error, code = await designer_adapter._chat_graph(
        _request(), {"graph_id": GRAPH_ID, "message": "改一下"}
    )

    assert error is None and code is None
    assert store.saved == 1
    assert payload is not None


@pytest.mark.asyncio
async def test_a_turn_is_refused_while_a_run_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping runs would share the graph and its node_states."""
    original = _graph()
    store = _prepare(monkeypatch, original)
    monkeypatch.setattr(
        designer_adapter._executor, "has_active_tasks", lambda graph_id: True
    )
    monkeypatch.setattr(designer_adapter, "_start_run", lambda params: (None, "unused", "UNUSED"))

    async def fake_leader_chat(graph, *args, **kwargs):
        return {
            "graph": original,
            "changed": False,
            "summary": "开始生成三段镜头视频。",
            "run_node_ids": ["n_clip_1"],
        }

    monkeypatch.setattr(leader_chat_module, "run_leader_chat", fake_leader_chat)

    payload, error, code = await designer_adapter._chat_graph(
        _request(), {"graph_id": GRAPH_ID, "message": "生成镜头视频"}
    )

    assert payload is None
    assert code == "CONFLICT"
    assert "尚未结束" in (error or "")
    assert store.saved == 0
