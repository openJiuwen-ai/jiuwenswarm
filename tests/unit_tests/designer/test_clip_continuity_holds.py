# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Continuity holds + crowd_state for clip story prompts."""

from __future__ import annotations


def test_extract_finished_events_catches_turn_and_crowd_exit() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        extract_finished_events,
    )

    text = (
        "Alex turns from his desk to look at the wall calendar. "
        "Colleagues walk away toward the exit."
    )
    events = extract_finished_events(
        text,
        characters=[{"id": "char_alex", "name": "Alex"}],
        on_screen=["char_alex"],
        shot_index=1,
    )
    kinds = {e["type"] for e in events}
    assert "beat" in kinds
    assert "crowd_exit" in kinds or any("crowd" in str(e.get("already_done") or "").lower() for e in events)


def test_pose_holds_and_crowd_state_stamped() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        apply_story_state_to_next_cfg,
    )

    prior = {
        "role": "clip",
        "setting_id": "set_office",
        "shot_index": 1,
        "on_screen": ["char_alex"],
        "shot_action": "Alex turns to look at the calendar",
        "last_wan_prompt": (
            "Alex from Image 1 turns from his desk to look at the wall calendar. "
            "Colleagues leave through the door."
        ),
    }
    nxt = {
        "role": "clip",
        "setting_id": "set_office",
        "shot_index": 2,
        "on_screen": ["char_alex"],
        "shot_action": "Alex says he forgot Valentine's Day",
        "speech_line": "Oh no, it's Valentine's Day!",
        "crowd_lock": {"present": False, "density": "none", "rule": "office cleared"},
    }
    out = apply_story_state_to_next_cfg(
        nxt,
        from_cfg=prior,
        from_prompt=prior["last_wan_prompt"],
        from_action=prior["shot_action"],
        from_shot_index=1,
        characters=[{"id": "char_alex", "name": "Alex"}],
    )
    assert out.get("pose_holds") or out.get("beat_done")
    crowd = out.get("crowd_state") or {}
    assert crowd.get("disposition") in {"exited", "empty"}
    hold = str(crowd.get("hold") or "").lower()
    assert "left" in hold or "clear" in hold or "empty" in hold


def test_compose_weaves_holds_and_crowd_not_lock_banner() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    cfg = {
        "cast_names": ["Alex"],
        "on_screen": ["Alex"],
        "shot_action": "Alex speaks about Valentine's Day.",
        "speech_line": "Oh no!",
        "costume_lock": "Alex: grey shirt",
        "previous_clip_wan_prompt": "Alex turns to look at the calendar.",
        "previous_clip_action": "Alex turns to look at the calendar",
        "pose_holds": ["Alex is already looking at the wall calendar."],
        "beat_done": ["Alex already finished: turns to look at the calendar"],
        "crowd_state": {
            "disposition": "exited",
            "present": False,
            "hold": "The area is clear of the earlier crowd — they already left.",
        },
        "scene_specs": {"scene_name": "open office", "lighting": "evening light"},
        "style_lock": {"look": "photoreal cinematic"},
    }
    text = compose_practice_prompt(cfg=cfg, graph={}, action=cfg["shot_action"])
    low = text.lower()
    assert "already" in low or "looking at the wall calendar" in low
    assert "clear of the earlier crowd" in low or "already left" in low
    assert "CROWD LOCK" not in text
    assert "do not" not in low
    # This shot is speech, not a restaged turn.
    assert "valentine" in low or "speaks" in low or "oh no" in low
