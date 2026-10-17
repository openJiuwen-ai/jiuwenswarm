# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Storyboard start/end state continuity tests."""

from __future__ import annotations


def test_ensure_shot_start_end_chains_same_setting() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
        ensure_clip_start_end_states,
        validate_storyboard_state_chain,
    )

    shots = [
        {
            "clip_index": 1,
            "setting_id": "set_a",
            "action": "Alex turns to the calendar",
            "speech_line": "Oh no!",
            "on_screen": ["char_alex"],
            "exiting_character_ids": [],
            "camera": "medium",
        },
        {
            "clip_index": 2,
            "setting_id": "set_a",
            "action": "Alex walks to the window",
            "speech_line": "",
            "on_screen": ["char_alex"],
            "camera": "tracking",
        },
    ]
    out = ensure_clip_start_end_states(shots)
    assert out[0]["start_state"]
    assert out[0]["end_state"]
    assert out[1]["start_state"]
    # Shot 2 keeps the spoken result, and does not replay shot 1's motion.
    assert out[1]["start_state"].get("speech_done") == "Oh no!"
    assert "turns to the calendar" not in str(out[1]["start_state"].get("pose") or "")
    assert not validate_storyboard_state_chain(out)


def test_stamp_clears_continuity_clip_node() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
        stamp_shot_states_on_clip_cfg,
    )

    cfg = stamp_shot_states_on_clip_cfg(
        {"continuity_clip_node_id": "n_clip_1", "clip_index": 2},
        shot={
            "start_state": {"pose": "Already facing the window", "seats": {"char_alex": {"place": "desk"}}},
            "end_state": {"pose": "At the window", "speech_done": ""},
        },
    )
    assert "continuity_clip_node_id" not in cfg
    assert cfg.get("start_state")
    assert cfg.get("seat_anchors")


def test_story_curve_keeps_one_climax_and_carries_the_end() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
        ensure_clip_start_end_states,
        start_end_story_lines,
        stamp_shot_states_on_clip_cfg,
    )

    shots = ensure_clip_start_end_states(
        [
            {
                "clip_index": 1,
                "setting_id": "set_1",
                "action": "A wrapped box lands beneath the tree.",
                "irreversible": "The wrapped box rests under the tree.",
                "emotion": "climax",
                "on_screen": ["char_1"],
                "cast_states": {"char_1": {"wardrobe": "red sweater", "presence": "on_screen"}},
            },
            {
                "clip_index": 2,
                "setting_id": "set_1",
                "action": "The family opens the box together.",
                "emotion": "climax",
                "on_screen": ["char_1"],
                "cast_states": {"char_1": {"wardrobe": "red coat", "presence": "on_screen"}},
            },
            {
                "clip_index": 3,
                "setting_id": "set_1",
                "action": "They toast with the product on the table.",
                "on_screen": ["char_1"],
            },
        ]
    )

    assert [shot["emotion"] for shot in shots].count("climax") == 1
    assert shots[1]["emotion"] == "climax"
    assert "lands beneath" not in " ".join(shots[1].get("do_not_replay") or [])
    assert shots[1]["start_state"].get("pose") == "The wrapped box rests under the tree."
    assert "lands beneath" not in str(shots[1]["start_state"].get("pose"))
    assert any("red coat" in change for change in shots[1]["wardrobe_changes"])
    lines = start_end_story_lines(stamp_shot_states_on_clip_cfg({}, shot=shots[1]))
    assert any("single climax" in line for line in lines)
    assert any("red coat" in line for line in lines)
    assert "lands beneath" not in " ".join(lines)
    assert not any("already finished" in line for line in lines)
