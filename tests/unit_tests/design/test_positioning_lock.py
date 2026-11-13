"""Positioning lock across five different staging shapes."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.positioning_lock import (
    build_lock,
    stamp_lock,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
)

NAMES = {"char_1": "Grandmother", "char_2": "Father", "char_3": "Saul", "char_4": "Goliath"}


def _cfg(**overrides):
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "clip_action": "They sit down.",
    }
    cfg.update(overrides)
    return cfg


def test_a_seated_table_states_each_side_once() -> None:
    cfg = _cfg(
        blocking={
            "landmark": "long table",
            "positions": [
                {"character_id": "char_1", "zone": "left"},
                {"character_id": "char_2", "zone": "right"},
            ],
        }
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert "Grandmother on the left of frame" in lock
    assert "Father on the right of frame" in lock
    assert "long table" in lock


def test_two_armies_keep_their_ridges() -> None:
    cfg = _cfg(
        on_screen=["char_3", "char_4"],
        spatial_lock={
            "layout": "Saul's army holds the northern ridge; the Philistine line holds the southern ridge",
            "landmark": "valley floor",
        },
        blocking={
            "positions": [
                {"character_id": "char_3", "zone": "background", "landmark": "northern ridge"},
                {"character_id": "char_4", "zone": "foreground", "landmark": "southern ridge"},
            ]
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert "northern ridge" in lock
    assert "southern ridge" in lock
    assert "background" in lock and "foreground" in lock


def test_a_room_with_a_door_and_a_window_uses_both_landmarks() -> None:
    cfg = _cfg(
        blocking={
            "landmark": "door",
            "positions": [{"character_id": "char_1", "zone": "left", "facing": "the window"}],
        }
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert "near the door" in lock
    assert "facing the window" in lock


def test_a_crowd_setting_falls_back_to_the_authored_layout() -> None:
    # No per-person blocking: the setting's own layout carries the arrangement.
    cfg = _cfg(
        blocking=None,
        spatial_lock={"layout": "fires on the west, wagons on the south, horses on the east"},
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert lock.startswith("Positions held for this setting:")
    assert "fires on the west" in lock


def test_the_lock_reaches_every_shot_and_leaks_no_negatives() -> None:
    cfg = _cfg(
        blocking={
            "landmark": "banner pole",
            "positions": [
                {"character_id": "char_1", "zone": "left"},
                {"character_id": "char_2", "zone": "right"},
            ],
        }
    )
    stamped = stamp_lock(cfg, names=NAMES, lang="en")
    assert cfg["positioning_lock_text"] == stamped

    shot_one = compose_practice_prompt(cfg=cfg, graph=None, action="They walk in.")
    shot_two = compose_practice_prompt(cfg=cfg, graph=None, action="They sit.")
    for prompt in (shot_one, shot_two):
        assert "Grandmother on the left of frame" in prompt
        assert "Father on the right of frame" in prompt
        # Sound keeps its negatives on purpose; the staging text never carries one.
        visual = prompt.split("Sound:")[0].split("声音：")[0].lower()
        for banned in (" no ", "not ", "never ", "without ", "do not"):
            assert banned not in visual
