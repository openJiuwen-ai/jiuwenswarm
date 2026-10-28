# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Positive still prompts — no LOCK essays on the image API body."""

from __future__ import annotations


def test_looks_like_lock_essay() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        looks_like_lock_essay,
    )

    assert looks_like_lock_essay("")
    assert looks_like_lock_essay("STYLE LOCK: photoreal. FORBID: no people.")
    assert looks_like_lock_essay("Empty room. No faces, no bodies.")
    assert not looks_like_lock_essay(
        "Empty scene specs of the warm kitchen: furniture, walls, windows, light."
    )


def test_compose_scene_spec_positive() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        compose_scene_specs_prompt,
    )

    cfg = {
        "role": "scene",
        "style_lock": {"look": "photoreal cinematic"},
        "scene_specs": {
            "place": "a quiet office at dusk",
            "lighting": "soft dusk through the window",
            "objects": ["desk", "chair"],
        },
        "time_of_day_lock": {"time_of_day": "dusk", "lighting": "soft dusk through the window"},
        "image_size": "1K",
    }
    text = compose_scene_specs_prompt(cfg=cfg, seed="")
    low = text.lower()
    assert "office" in low
    assert "interior" in low
    assert "desk" in low and "chair" in low
    assert "dusk" in low or "soft dusk" in low
    assert "photoreal" in low
    assert "the setting is empty" in low
    assert "STYLE LOCK" not in text
    assert "SPATIAL LOCK" not in text
    assert "do not" not in low
    assert "forbid" not in low


def test_scene_spec_skips_ids_placeholders_and_planner_rules() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        compose_scene_specs_prompt,
    )

    cfg = {
        "role": "scene",
        "style_lock": {"look": "flat illustrated color"},
        "scene_specs": {
            "scene_name": "set_1",
            "description": "A city rooftop with a low parapet.",
            "objects": [
                "primary landmark / architecture massing",
                "round table",
                "background depth cues (walls/trees/skyline as appropriate)",
            ],
            "lighting": "dusk / golden-hour warmth; long shadows; keep dusk across same-setting shots",
        },
        "spatial_lock": {
            "setting": "set_1",
            "architecture": "keep one coherent place",
            "static_rule": "Never invent an empty environment plate.",
        },
        "time_of_day_lock": {
            "time_of_day": "dusk",
            "lighting": "dusk / golden-hour warmth; long shadows; keep dusk across same-setting shots",
        },
    }
    text = compose_scene_specs_prompt(cfg=cfg, seed="")
    low = text.lower()
    assert "rooftop" in low
    assert "exterior" in low
    assert "round table" in low
    assert "set_1" not in low
    assert "primary landmark" not in low
    assert "skyline" not in low
    assert "keep one coherent place" not in low
    assert "keep dusk" not in low
    assert "never" not in low
    assert "spatial lock" not in low
    assert "room" not in low
    assert "illustrated" in low


def test_compose_character_sheet_positive() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        compose_character_sheet_prompt,
    )

    cfg = {
        "role": "character",
        "character_name": "Alex",
        "costume_lock": "grey coat over a white shirt",
        "style_lock": {"look": "photoreal cinematic"},
    }
    text = compose_character_sheet_prompt(cfg=cfg, seed="")
    low = text.lower()
    assert "alex" in low
    assert "wearing" in low
    assert "grey coat" in low
    assert "CLOTHING LOCK" not in text
    assert "do not" not in low


def test_ensure_rewrites_lock_essay_scene() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        ensure_still_tool_prompt,
    )

    essay = (
        "SCENE SPECS — environment only. No people, no faces. "
        "STYLE LOCK: photoreal. TIME OF DAY LOCK=night: candlelight."
    )
    cfg = {
        "role": "scene",
        "style_lock": {"look": "photoreal cinematic"},
        "scene_specs": {"scene_name": "candlelit restaurant", "objects": ["table"]},
        "time_of_day_lock": {"time_of_day": "night", "lighting": "warm candlelight"},
    }
    text, notes = ensure_still_tool_prompt(essay, role="scene", cfg=cfg)
    assert notes
    assert "STYLE LOCK" not in text
    assert "TIME OF DAY LOCK" not in text
    assert "no people" not in text.lower()
    assert "night" in text.lower() or "candle" in text.lower()


def test_ensure_rewrites_lock_essay_character() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        ensure_still_tool_prompt,
    )

    essay = "ONE person. CLOTHING LOCK: red dress. No room, no furniture."
    cfg = {
        "role": "character",
        "character_name": "Sam",
        "costume_lock": "red dress",
        "style_lock": {"look": "photoreal cinematic"},
    }
    text, notes = ensure_still_tool_prompt(essay, role="character", cfg=cfg)
    assert notes
    assert "CLOTHING LOCK" not in text
    assert "sam" in text.lower()
    assert "wearing" in text.lower() or "red dress" in text.lower()


def test_continue_line_does_not_paste_prior_wan() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _continue_line,
        compose_practice_prompt,
    )

    prior = (
        "The scene is as in Image 3: the warm dining room. "
        "Mother from Image 1, wearing dusty rose, is listening carefully."
    )
    cfg = {
        "cast_names": ["Mother"],
        "on_screen": ["Mother"],
        "clip_action": "Mother nods once.",
        "costume_lock": "Mother: dusty rose blouse",
        "previous_clip_wan_prompt": prior,
        "previous_clip_action": "Mother listens",
        "already_done": ["Mother listened to the news"],
        "scene_specs": {"scene_name": "the warm dining room", "lighting": "warm light"},
        "style_lock": {"look": "photoreal cinematic"},
    }
    cue = _continue_line(cfg)
    assert cue
    assert "listening carefully" not in cue.lower()
    assert "continue after:" not in cue.lower()
    text = compose_practice_prompt(cfg=cfg, graph={}, action=cfg["clip_action"])
    assert "listening carefully" not in text.lower()
    assert "same setting" in text.lower() or "same placement" in text.lower()
    assert "STYLE LOCK" not in text
