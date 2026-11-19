# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The film score is generated once by the music node, never by a clip."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    _sound_line,
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


def test_clip_prompt_forbids_a_score_instead_of_leaving_sound_unspecified() -> None:
    # A native-audio video model fills an unspecified track with a music bed, so a
    # clip prompt without any audio direction scored itself — which is what showed
    # up as "BGM in the clips" once the music node failed.
    for graph in ({}, _SCORED_GRAPH):
        prompt = compose_practice_prompt(cfg=_MUSIC_CFG, graph=graph, action=_MUSIC_CFG["clip_action"])
        assert "no music" in prompt
        assert "no score" in prompt
        assert "no ambient music bed" in prompt


def test_clip_prompt_names_diegetic_cues_and_stays_silent_on_request() -> None:
    cfg = {**_MUSIC_CFG, "sfx": ["wind in the barley", "a distant dog"]}
    prompt = compose_practice_prompt(cfg=cfg, graph=_SCORED_GRAPH, action=cfg["clip_action"])
    assert "wind in the barley" in prompt
    assert "no score" in prompt

    silent = {**_MUSIC_CFG, "audio_policy": "silent"}
    silent_prompt = compose_practice_prompt(cfg=silent, graph=_SCORED_GRAPH, action=silent["clip_action"])
    assert "silent" in silent_prompt.lower()
    assert "no music" in silent_prompt


def test_a_shot_without_speech_forbids_invented_dialogue() -> None:
    prompt = compose_practice_prompt(cfg=_MUSIC_CFG, graph={}, action=_MUSIC_CFG["clip_action"])
    assert "No one speaks" in prompt
    spoken = {**_MUSIC_CFG, "speech_by_character": {"char_1": "hello"}}
    spoken_prompt = compose_practice_prompt(cfg=spoken, graph={}, action=spoken["clip_action"])
    assert "No one speaks" not in spoken_prompt
    assert "人物没有台词、对白或独白" in _sound_line(_MUSIC_CFG, "zh")
    assert "人物没有台词" not in _sound_line(spoken, "zh")
