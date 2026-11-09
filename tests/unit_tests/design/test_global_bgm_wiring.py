# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The film score is generated once by the music node, never by a clip."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
    film_scored_by_music_node,
)
from jiuwenswarm.server.runtime.designer.smart_graph import bgm_node_wanted

_SCORED_GRAPH = {
    "nodes": [
        {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip"}},
        {"id": "n_music", "type": "audio", "config": {"pipeline": "music"}},
        {"id": "n_compose", "type": "video", "config": {"pipeline": "compose"}},
    ],
    "edges": [
        {"id": "e1", "source": "n_clip_1", "target": "n_compose"},
        {"id": "e2", "source": "n_music", "target": "n_compose"},
    ],
}

_MUSIC_CFG = {
    "cast_names": ["Alex"],
    "on_screen": ["Alex"],
    "clip_action": "Alex pours coffee.",
    "bgm_lock": {"instruments": "solo piano and strings", "mood": "warm morning"},
}


def test_bgm_node_needs_an_explicit_music_request() -> None:
    # Opt-in: a configured backend alone must not put music on the film.
    assert bgm_node_wanted({"include_music": True, "policy": "music"}, "a film", can_music=True) is True
    assert bgm_node_wanted({"include_music": False, "policy": "optional_music"}, "a film", can_music=True) is False
    assert bgm_node_wanted({"include_music": False}, "a film", can_music=True) is False
    # Declining and silence still win over a request.
    assert bgm_node_wanted({"include_music": True, "policy": "silent"}, "", can_music=True) is False
    assert bgm_node_wanted({"include_music": True, "policy": "music"}, "no music please", can_music=True) is False
    # No backend, no node.
    assert bgm_node_wanted({"include_music": True, "policy": "music"}, "", can_music=False) is False


def test_music_node_owning_the_score_detaches_it_from_the_clips() -> None:
    assert film_scored_by_music_node(_SCORED_GRAPH) is True
    assert film_scored_by_music_node({}) is False
    detached = {**_SCORED_GRAPH, "edges": _SCORED_GRAPH["edges"][:1]}
    assert film_scored_by_music_node(detached) is False


def test_no_clip_prompt_ever_asks_for_music() -> None:
    # Even with a bgm_lock full of concrete instrumentation and no music node,
    # the clip prompt stays music-free: the score belongs to compose.
    for graph in ({}, _SCORED_GRAPH):
        prompt = compose_practice_prompt(cfg=_MUSIC_CFG, graph=graph, action=_MUSIC_CFG["clip_action"])
        assert "solo piano and strings" not in prompt
        assert "Audio:" not in prompt
        assert "音乐：" not in prompt
