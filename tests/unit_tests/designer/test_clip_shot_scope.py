# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
    apply_shot_scope,
    looks_like_full_story_restatement,
    needs_duration_slicing,
    requested_film_duration_sec,
    sequential_shot_count,
    sequential_windows,
)
from jiuwenswarm.server.runtime.designer.pipeline.director_contract import infer_shot_budget
from jiuwenswarm.server.runtime.designer.handlers.clip import build_clip_prompt
from jiuwenswarm.server.runtime.designer.handlers.common import graph_prompt


LONG_STORY = (
    "A father sits by the window, a mother pours tea, and a child walks away from "
    "the table while the whole family talks through the evening meal and then "
    "clears the dishes together."
)


def test_requested_duration_minutes_and_seconds() -> None:
    assert requested_film_duration_sec("make a 10-second clip") == 10
    assert requested_film_duration_sec("5 minute video") == 300
    assert requested_film_duration_sec("5 mins long") == 300
    assert requested_film_duration_sec("三分镜15秒竖屏") == 15
    assert requested_film_duration_sec("no length stated") is None
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import brief_duration_seconds

    assert brief_duration_seconds("Generate a 10-second video in a medieval style") == 10
    assert brief_duration_seconds("no length stated") == 5
    assert brief_duration_seconds("a 5 minute film") == 300


def test_long_runtime_does_not_force_a_clip_split() -> None:
    assert needs_duration_slicing("10-second video") is False
    assert needs_duration_slicing("15 second film") is False
    assert needs_duration_slicing("5 minute video") is False
    assert needs_duration_slicing(LONG_STORY) is False
    assert sequential_shot_count(300) == 20
    assert sequential_shot_count(45) == 3


def test_infer_budget_does_not_slice_short_films() -> None:
    assert infer_shot_budget("Make a 3-shot 15 second vertical video", {}) == 3
    n = infer_shot_budget("Create a 5 minute film of a family conversation", {})
    assert n == 1
    assert n < sequential_shot_count(300)


def test_apply_shot_scope_keeps_a_long_runtime_in_one_clip() -> None:
    out = apply_shot_scope(
        {"shots": [{"shot_index": 1, "action": LONG_STORY, "shot_relation": "angle_variant"}]},
        "5 minute video. " + LONG_STORY,
    )
    assert out["duration_slicing"] is False
    assert len(out["shots"]) == 1
    assert out["target_duration_sec"] == 300
    assert out["shots"][0]["shot_relation"] != "angle_variant"


def test_apply_shot_scope_preserves_richer_authored_plan_for_30_seconds() -> None:
    shots = [
        {"shot_index": 1, "action": "A wrapped box lands beneath the tree."},
        {"shot_index": 2, "action": "Family members follow a trail of glowing ornaments."},
        {"shot_index": 3, "action": "They discover the product inside and try it together."},
        {"shot_index": 4, "action": "The room opens into a joyful celebration and brand payoff."},
    ]
    out = apply_shot_scope(
        {"shots": shots},
        "Create a 30 second video for a Christmas product celebration.",
    )

    assert out["duration_slicing"] is False
    assert len(out["shots"]) == 4
    assert [shot["action"] for shot in out["shots"]] == [
        shot["action"] for shot in shots
    ]
    assert out["target_duration_sec"] == 30


def test_apply_shot_scope_skips_c_when_under_wan_max() -> None:
    shots = [
        {"shot_index": 1, "action": "Dad sits by the window", "shot_relation": "hard_cut"},
        {"shot_index": 2, "action": "Child walks to the door", "shot_relation": "angle_variant"},
    ]
    out = apply_shot_scope({"shots": shots}, "10-second scene. Dad sits. Child walks to the door.")
    assert out.get("duration_slicing") is False
    assert len(out["shots"]) == 2
    assert out["shots"][1]["shot_relation"] == "continuation"


def test_restatement_detector() -> None:
    assert looks_like_full_story_restatement(LONG_STORY, LONG_STORY) is True
    assert looks_like_full_story_restatement("Child walks to the door.", LONG_STORY) is False


def test_sequential_windows_sum(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "wan3.0-video",
    )
    wins = sequential_windows(45, 3)
    assert wins[0] == (0, 15)
    assert wins[-1][1] == 45


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
                    "shot_index": 2,
                    "shot_action": "Child walks to the door",
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
        "config": {"role": "clip", "shot_index": 1, "shot_action": "Dad sits by the window"},
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
        {"shots": [{"shot_index": 1, "action": beat}, {"shot_index": 2, "action": "Child walks to the door"}]},
        LONG_STORY,
    )
    assert "navy sweater" in out["shots"][0]["action"]
    assert "Child walks to the door" in out["shots"][1]["action"]


def test_same_scene_gate_in_handoff_and_graph_prompt_keeps_locks() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
        agent_replays_finished_events,
        handoff_clause_for_prompt,
    )

    clause = handoff_clause_for_prompt(
        [{"shot_index": 1, "shot_action": "child walked away", "speech_line": "bye"}],
        this_shot_index=2,
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
            "shot_index": 1,
            "shot_action": "Dad sits by the window",
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
    assert "navy sweater" in text
    assert "LANGUAGE LOCK" in text


def test_empty_actions_are_time_windows_not_angle_restages() -> None:
    prompt = (
        "10-second scene. Father reads a letter at the table. "
        "The child walks to the door."
    )
    out = apply_shot_scope(
        {
            "shots": [
                {
                    "shot_index": 1,
                    "action": "",
                    "view_key": "front",
                    "shot_relation": "hard_cut",
                },
                {
                    "shot_index": 2,
                    "action": "",
                    "view_key": "left",
                    "shot_relation": "angle_variant",
                },
            ]
        },
        prompt,
    )
    assert out.get("duration_slicing") is False
    actions = [str(s.get("action") or "").strip() for s in out["shots"]]
    assert all(actions)
    assert actions[0][:48] != actions[1][:48]
    assert all(str(s.get("view_key") or "") == "" for s in out["shots"])
    assert out["shots"][1]["shot_relation"] == "continuation"
