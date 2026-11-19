# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import (
    apply_shot_scope,
    looks_like_full_story_restatement,
)
from jiuwenswarm.server.runtime.designer.pipeline.director_contract import infer_shot_budget
from jiuwenswarm.server.runtime.designer.handlers.clip import build_clip_prompt
from jiuwenswarm.server.runtime.designer.handlers.common import graph_prompt


LONG_STORY = (
    "A father sits by the window, a mother pours tea, and a child walks away from "
    "the table while the whole family talks through the evening meal and then "
    "clears the dishes together."
)


def test_infer_budget_does_not_slice_short_films() -> None:
    assert infer_shot_budget("Make a 3-shot 15 second vertical video", {}) == 3
    n = infer_shot_budget("Create a 5 minute film of a family conversation", {})
    assert n == 1


def test_apply_shot_scope_keeps_a_long_runtime_in_one_clip() -> None:
    out = apply_shot_scope(
        {"clips": [{"clip_index": 1, "action": LONG_STORY, "shot_relation": "angle_variant"}]},
        "5 minute video. " + LONG_STORY,
    )
    assert len(out["clips"]) == 1
    assert out["clips"][0]["shot_relation"] != "angle_variant"


def test_apply_shot_scope_preserves_richer_authored_plan_for_30_seconds() -> None:
    shots = [
        {"clip_index": 1, "action": "A wrapped box lands beneath the tree."},
        {"clip_index": 2, "action": "Family members follow a trail of glowing ornaments."},
        {"clip_index": 3, "action": "They discover the product inside and try it together."},
        {"clip_index": 4, "action": "The room opens into a joyful celebration and brand payoff."},
    ]
    out = apply_shot_scope(
        {"clips": shots},
        "Create a 30 second video for a Christmas product celebration.",
    )

    assert len(out["clips"]) == 4
    assert [shot["action"] for shot in out["clips"]] == [
        shot["action"] for shot in shots
    ]


def test_apply_shot_scope_skips_c_when_under_wan_max() -> None:
    shots = [
        {"clip_index": 1, "action": "Dad sits by the window", "shot_relation": "hard_cut"},
        {"clip_index": 2, "action": "Child walks to the door", "shot_relation": "angle_variant"},
    ]
    out = apply_shot_scope({"clips": shots}, "10-second scene. Dad sits. Child walks to the door.")
    assert len(out["clips"]) == 2
    assert out["clips"][1]["shot_relation"] == "continuation"


def test_restatement_detector() -> None:
    assert looks_like_full_story_restatement(LONG_STORY, LONG_STORY) is True
    assert looks_like_full_story_restatement("Child walks to the door.", LONG_STORY) is False


def test_build_clip_prompt_omits_full_user_story() -> None:
    graph = {
        "description": LONG_STORY,
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": LONG_STORY}},
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "clip_index": 2,
                    "clip_action": "Child walks to the door",
                    "camera": "medium",
                    "cast_names": ["Child"],
                },
            },
        ],
        "metadata": {},
    }
    prompt = build_clip_prompt(graph, graph["nodes"][1])
    assert "USER PROMPT (authoritative story" not in prompt
    assert "walks to the door" in prompt
    assert looks_like_full_story_restatement(prompt, LONG_STORY) is False


def test_graph_prompt_clip_does_not_fall_back_to_brief() -> None:
    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {"role": "clip", "clip_index": 1, "clip_action": "Dad sits by the window"},
    }
    graph = {
        "description": LONG_STORY,
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": LONG_STORY}},
            node,
        ],
    }
    text = graph_prompt(graph, node)
    assert "Dad sits by the window" in text
    assert looks_like_full_story_restatement(text, LONG_STORY) is False


def test_detailed_beat_is_not_full_story_restatement() -> None:
    beat = (
        "Dad sits by the window in a navy sweater, mother pours tea at the table, "
        "steam rising, both facing screen-left."
    )
    assert looks_like_full_story_restatement(beat, LONG_STORY) is False
    out = apply_shot_scope(
        {"clips": [{"clip_index": 1, "action": beat}, {"clip_index": 2, "action": "Child walks to the door"}]},
        LONG_STORY,
    )
    assert "navy sweater" in out["clips"][0]["action"]
    assert "Child walks to the door" in out["clips"][1]["action"]


def test_same_scene_gate_in_handoff_and_graph_prompt_keeps_locks() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
        agent_replays_finished_events,
        handoff_clause_for_prompt,
    )

    clause = handoff_clause_for_prompt(
        [{"clip_index": 1, "clip_action": "child walked away", "speech_line": "bye"}],
        this_clip_index=2,
        this_action="family sits together",
    )
    assert "SAME-SCENE CONSISTENCY GATE" in clause
    assert "AGREES" in clause.upper() or "agree" in clause.lower()
    assert "repeat" in clause.lower()
    assert agent_replays_finished_events(
        "the child walked away from the table again",
        already_done=["shot 1: Child already left — do not show walking away"],
        this_action="family sits together",
    )
    assert not agent_replays_finished_events(
        "Dad stays seated by the window",
        already_done=["shot 1: Child already left — do not show walking away"],
        this_action="Dad stays seated by the window",
    )
    assert not agent_replays_finished_events(
        "the child walks to the door again as asked",
        already_done=["shot 1: Child already left — do not show walking away"],
        this_action="Child walks to the door again",
    )
    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {
            "role": "clip",
            "clip_index": 1,
            "clip_action": "Dad sits by the window",
            "costume_lock": "navy sweater",
            "language_lock": "en",
        },
    }
    graph = {
        "description": LONG_STORY,
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": LONG_STORY}},
            node,
        ],
    }
    text = graph_prompt(graph, node)
    assert "Dad sits by the window" in text
    assert LONG_STORY not in text
    # Locks reach the agent through the structured snapshot, not a second pasted copy.
    assert "navy sweater" not in text
    assert "LANGUAGE LOCK" not in text


def test_empty_actions_are_time_windows_not_angle_restages() -> None:
    prompt = (
        "10-second scene. Father reads a letter at the table. "
        "The child walks to the door."
    )
    out = apply_shot_scope(
        {
            "clips": [
                {
                    "clip_index": 1,
                    "action": "",
                    "view_key": "front",
                    "shot_relation": "hard_cut",
                },
                {
                    "clip_index": 2,
                    "action": "",
                    "view_key": "left",
                    "shot_relation": "angle_variant",
                },
            ]
        },
        prompt,
    )
    actions = [str(s.get("action") or "").strip() for s in out["clips"]]
    assert all(actions)
    assert actions[0][:48] != actions[1][:48]
    assert all(str(s.get("view_key") or "") == "" for s in out["clips"])
    assert out["clips"][1]["shot_relation"] == "continuation"
