# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One regression case per defect this branch fixes for the design canvas.

Each test pins the behaviour that used to fail, so a later change cannot quietly
reintroduce it.
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.handlers.text_nodes import _audio_plan_instruction
from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import infer_crowd_state
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
    exited_names,
)
from jiuwenswarm.server.runtime.designer.script_analysis import detect_cast_form
from jiuwenswarm.server.runtime.designer.smart_graph import _specs_referenced_by_clips


def test_chinese_headcount_needs_a_person_noun() -> None:
    # "三位路人" counts people; an age and a duration are not headcounts.
    assert detect_cast_form("\u8def\u4eba", "\u4e09\u4f4d\u8def\u4eba\u4ece\u8857\u89d2\u7ecf\u8fc7") == (
        "group",
        "\u4e09\u4f4d",
    )
    assert detect_cast_form("\u5f20\u4e09", "\u4ed6\u5df2\u7ecf\u4e94\u5341\u5c81\u4e86") == ("single", "")
    assert detect_cast_form("\u738b\u4e94", "\u4ed6\u8981\u7b49\u4e09\u5c0f\u65f6") == ("single", "")


def test_shared_look_crowd_is_a_group_not_one_stand_in() -> None:
    # A collective name or plural noun draws the group; one named person stays single.
    assert detect_cast_form("Porters", "four-man uniform group") == ("group", "")
    assert detect_cast_form("Commuters", "muted raincoats and jackets") == ("group", "")
    assert detect_cast_form("Elder", "a man aged fifty to seventy-five") == ("single", "")
    # "one adult passer-by" describes a member, the collective name still wins.
    assert detect_cast_form("\u8def\u4eba", "one adult passer-by")[0] == "group"


def test_crowd_exit_and_return_come_from_events_not_from_prose() -> None:
    exited = infer_crowd_state({}, events=[{"type": "crowd_exit"}])
    assert exited["disposition"] == "exited"
    assert exited["present"] is False
    assert exited["hold"]

    back = infer_crowd_state(
        {"crowd_lock": {"disposition": "returned"}},
        prior_cfg={"crowd_state": {"disposition": "exited"}},
    )
    assert back["present"] is True
    assert back["hold"]

    # Prose is no longer scanned here: that language dependence was the root cause.
    prose_only = infer_crowd_state({}, prior_text="the whole crowd left the platform")
    assert prose_only["disposition"] == "unspecified"
    assert prose_only["present"] is None


def test_clip_prompt_never_names_a_character_that_exited() -> None:
    graph = {
        "nodes": [
            {"id": "n_character_1", "type": "image", "config": {"pipeline": "character", "character_ids": ["char_ruth"]}},
            {"id": "n_character_2", "type": "image", "config": {"pipeline": "character", "character_ids": ["char_eli"]}},
        ]
    }
    cfg = {
        "on_screen": ["char_ruth"],
        "character_ids": ["char_ruth", "char_eli"],
        "exited_ids": ["char_eli"],
        "clip_action": "Ruth waits under the lamp; Eli has already gone.",
    }
    assert "Eli" in exited_names(cfg, graph)
    prompt = compose_practice_prompt(cfg=cfg, graph=graph, action=cfg["clip_action"])
    assert "Eli" not in prompt
    assert "Ruth" in prompt


def test_a_shot_without_a_place_is_not_given_one() -> None:
    # The old fallback wrote "the empty room" onto outdoor and aerial shots.
    prompt = compose_practice_prompt(
        cfg={"cast_names": ["Ruth"], "on_screen": ["Ruth"], "clip_action": "Ruth walks."},
        graph={},
        action="Ruth walks.",
    )
    assert "empty room" not in prompt.lower()


def test_only_the_specs_a_clip_stages_survive_the_prune() -> None:
    nodes = [
        {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip", "character_ids": ["char_1"]}},
        {"id": "n_character_1", "type": "image", "config": {"pipeline": "character", "character_ids": ["char_1"]}},
        {"id": "n_character_2", "type": "image", "config": {"pipeline": "character", "character_ids": ["char_2"]}},
    ]
    assert _specs_referenced_by_clips(nodes) == {"n_character_1"}


def test_audio_plan_instruction_follows_the_plan() -> None:
    silent = _audio_plan_instruction(
        {"speech": {"include": False, "kind": "none"}, "sfx": {"include": False}, "music": {"requested": False}}
    )
    assert "no dialogue" in silent
    assert "no room tone" in silent
    assert "Music: none" in silent

    scored = _audio_plan_instruction(
        {"speech": {"include": True}, "sfx": {"include": True}, "music": {"requested": True, "supported": True}}
    )
    assert "one film-level score" in scored
    assert "room tone and action sound only" in scored

    unsupported = _audio_plan_instruction(
        {"speech": {"include": True}, "sfx": {"include": True}, "music": {"requested": True, "supported": False}}
    )
    assert "NOT SUPPORTED" in unsupported
