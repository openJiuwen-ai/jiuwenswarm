# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-added canvas nodes should not grow extra wires after save."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.designer.orchestration import Director


def _pairs(graph: dict) -> set[tuple[str, str]]:
    return {
        (str(edge.get("source") or ""), str(edge.get("target") or ""))
        for edge in graph.get("edges") or []
        if isinstance(edge, dict)
    }


def test_onboard_does_not_rewire_successor_connected_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: False,
    )
    graph = {
        "nodes": [
            {
                "id": "n_character",
                "type": "image",
                "config": {"pipeline": "character"},
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"pipeline": "clip"},
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {"pipeline": "clip"},
            },
            {
                "id": "n_compose",
                "type": "video",
                "config": {"pipeline": "compose"},
            },
            {
                "id": "n_image_user",
                "type": "image",
                "config": {
                    "role": "image",
                    "user_added": True,
                    "inputs": ["n_character"],
                },
            },
        ],
        "edges": [
            {
                "id": "e_char_user",
                "source": "n_character",
                "target": "n_image_user",
                "kind": "data",
            },
            {
                "id": "e_clip_compose",
                "source": "n_clip_1",
                "target": "n_compose",
                "kind": "data",
            },
        ],
    }
    Director().onboard_user_added_nodes(graph)
    pairs = _pairs(graph)
    assert ("n_character", "n_image_user") in pairs
    assert ("n_image_user", "n_clip_1") not in pairs
    assert ("n_image_user", "n_clip_2") not in pairs
    assert ("n_image_user", "n_compose") not in pairs


def test_onboard_does_not_rewire_successor_connected_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: False,
    )
    graph = {
        "nodes": [
            {
                "id": "n_clip_ua1",
                "type": "image",
                "config": {"pipeline": "frame"},
            },
            {
                "id": "n_compose",
                "type": "video",
                "config": {"pipeline": "compose"},
            },
            {
                "id": "n_video_user",
                "type": "video",
                "config": {"role": "video", "user_added": True, "inputs": ["n_clip_ua1"]},
            },
        ],
        "edges": [
            {
                "id": "e_frame_user",
                "source": "n_clip_ua1",
                "target": "n_video_user",
                "kind": "data",
            },
        ],
    }
    Director().onboard_user_added_nodes(graph)
    pairs = _pairs(graph)
    assert ("n_clip_ua1", "n_video_user") in pairs
    assert ("n_video_user", "n_compose") not in pairs


def test_onboard_does_not_wire_orphan_dock_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: False,
    )
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"pipeline": "clip"},
            },
            {
                "id": "n_compose",
                "type": "video",
                "config": {"pipeline": "compose"},
            },
            {
                "id": "n_image_user",
                "type": "image",
                "config": {"role": "image", "user_added": True},
            },
            {
                "id": "n_video_user",
                "type": "video",
                "config": {"role": "video", "user_added": True},
            },
        ],
        "edges": [
            {
                "id": "e_clip_compose",
                "source": "n_clip_1",
                "target": "n_compose",
                "kind": "data",
            },
        ],
    }
    Director().onboard_user_added_nodes(graph)
    pairs = _pairs(graph)
    assert ("n_image_user", "n_clip_1") not in pairs
    assert ("n_image_user", "n_compose") not in pairs
    assert ("n_video_user", "n_compose") not in pairs
    assert ("n_clip_1", "n_compose") in pairs
    assert graph["metadata"]["director_user_node_onboard"]["orphans"]


def test_director_records_canvas_add_and_remove_without_new_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: False,
    )
    from jiuwenswarm.server.runtime.designer.orchestration import Director

    graph = {
        "metadata": {
            "user_canvas_edits": [
                {"op": "add", "node_id": "n_image_user", "label": "Image 2", "role": "image"},
                {"op": "remove", "node_id": "n_clip_9", "label": "Clip 9", "role": "clip"},
                {
                    "op": "connect",
                    "node_id": "n_image_user",
                    "peer_id": "n_clip_user",
                },
                {"op": "replace", "node_id": "n_image_user", "label": "officer.png"},
            ]
        },
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"pipeline": "clip"},
            },
            {
                "id": "n_compose",
                "type": "video",
                "config": {"pipeline": "compose"},
            },
            {
                "id": "n_image_user",
                "type": "image",
                "config": {"role": "image", "user_added": True},
            },
            {
                "id": "n_clip_user",
                "type": "video",
                "config": {"role": "clip", "user_added": True},
            },
        ],
        "edges": [
            {
                "id": "e_clip_compose",
                "source": "n_clip_1",
                "target": "n_compose",
                "kind": "data",
            },
        ],
    }
    Director().onboard_user_added_nodes(graph)
    Director().ensure_agents_and_prune(graph)
    pairs = _pairs(graph)
    assert ("n_image_user", "n_clip_1") not in pairs
    assert ("n_clip_user", "n_compose") not in pairs
    assert ("n_clip_1", "n_compose") in pairs
    awareness = graph["metadata"]["director_canvas_awareness"]
    assert awareness["added"][0]["node_id"] == "n_image_user"
    assert awareness["removed"][0]["node_id"] == "n_clip_9"
    assert awareness["connected"][0]["peer_id"] == "n_clip_user"
    assert awareness["replaced"][0]["label"] == "officer.png"
