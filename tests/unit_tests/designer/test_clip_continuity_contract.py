# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic shot consistency contract tests."""

from __future__ import annotations


def _two_clip_graph() -> dict:
    return {
        "nodes": [
            {
                "id": "n_clip_1",
                "config": {
                    "role": "clip",
                    "setting_id": "set_a",
                    "shot_index": 1,
                    "shot_action": "Alex turns to look at the calendar",
                    "speech_line": "Oh no, it's Valentine's Day!",
                    "camera": "medium shot",
                    "on_screen": ["char_alex"],
                },
            },
            {
                "id": "n_clip_2",
                "config": {
                    "role": "clip",
                    "setting_id": "set_a",
                    "shot_index": 2,
                    "shot_action": "Alex walks to the window",
                    "speech_line": "Oh no, it's Valentine's Day!",
                    "camera": "tracking shot",
                    "on_screen": ["char_alex"],
                },
            },
        ]
    }


def test_agent_prior_story_block_has_no_raw_wan_dump() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        agent_prior_story_block,
    )

    cfg = {
        "previous_clip_shot_index": 1,
        "previous_clip_action": "Alex turns to look at the calendar",
        "previous_clip_speech": "Oh no!",
        "previous_clip_wan_prompt": (
            "Alex from Image 1 turns from his desk to look at the wall calendar. "
            "Colleagues leave. Alex says: \"Oh no!\""
        ),
        "already_done": ["shot 1 already filmed: Alex turns to look at the calendar"],
        "pose_holds": ["Already past: Alex turns to look at the calendar."],
        "forbidden_speech": ["Oh no!"],
        "shot_action": "Alex walks to the window",
        "speech_line": "",
    }
    block = agent_prior_story_block(cfg)
    assert "PREVIOUS WAN PROMPT" not in block
    assert "Alex from Image 1" not in block
    assert "CONTINUITY STATE" in block
    assert "already" in block.lower() or "Prior action" in block


def test_enforce_speech_uniqueness_clears_duplicate() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
        enforce_speech_uniqueness,
        merge_storyboard_continuity,
    )

    graph = _two_clip_graph()
    cfg = dict(graph["nodes"][1]["config"])
    out, notes = merge_storyboard_continuity(cfg, graph=graph)
    assert "cleared_duplicate_speech" in notes or not str(out.get("speech_line") or "").strip()
    assert not str(out.get("speech_line") or "").strip()
    assert out.get("forbidden_speech")
    assert out.get("end_state")
    assert out.get("already_done")

    # Direct enforce path
    cfg2 = dict(graph["nodes"][1]["config"])
    cfg2["previous_clip_speech"] = "Oh no, it's Valentine's Day!"
    cfg2["forbidden_speech"] = ["Oh no, it's Valentine's Day!"]
    cleared, n2 = enforce_speech_uniqueness(cfg2, graph=graph)
    assert not str(cleared.get("speech_line") or "").strip()
    assert "cleared_duplicate_speech" in n2


def test_prompt_violates_restated_speech_and_prior_action() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
        prompt_violates_continuity,
    )

    cfg = {
        "shot_action": "Alex walks to the window",
        "previous_clip_action": "Alex turns to look at the calendar",
        "already_done": ["shot 1 already filmed: Alex turns to look at the calendar"],
        "forbidden_speech": ["Oh no, it's Valentine's Day!"],
        "previous_clip_speech": "Oh no, it's Valentine's Day!",
    }
    bad = (
        'Alex turns to look at the calendar. Alex says: "Oh no, it\'s Valentine\'s Day!"'
    )
    reasons = prompt_violates_continuity(bad, cfg=cfg)
    assert reasons
    assert (
        "restates_forbidden_speech" in reasons
        or "replays_finished_events" in reasons
        or "films_prior_action_not_this_row" in reasons
    )

    good = "Alex walks to the window and looks outside quietly."
    assert not prompt_violates_continuity(good, cfg=cfg)


def test_cross_setting_does_not_carry_speech() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
        merge_storyboard_continuity,
    )

    graph = _two_clip_graph()
    graph["nodes"][1]["config"]["setting_id"] = "set_b"
    cfg = dict(graph["nodes"][1]["config"])
    out, _notes = merge_storyboard_continuity(cfg, graph=graph)
    # Different setting: speech uniqueness should not clear from set_a.
    assert str(out.get("speech_line") or "") == "Oh no, it's Valentine's Day!"
