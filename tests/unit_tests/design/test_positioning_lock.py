"""Positioning lock across very different staging shapes.

The lock has to work for a dinner table, a diner, a car, a cartoon living room and
an open-air setting alike: what matters is that every shot of one setting reads the
same arrangement in positive words.
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.positioning_lock import (
    build_lock,
    merged_positions,
    stamp_lock,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
)

NAMES = {
    "char_1": "Grandmother",
    "char_2": "Father",
    "char_3": "Maya",
    "char_4": "Theo",
    "char_5": "Driver",
    "char_6": "Dog",
}


def _cfg(**overrides):
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "clip_action": "They sit down.",
    }
    cfg.update(overrides)
    return cfg


def test_a_family_dinner_keeps_every_seat() -> None:
    cfg = _cfg(
        on_screen=["char_1", "char_2", "char_3"],
        blocking={
            "landmark": "dining table",
            "positions": [
                {
                    "character_id": "char_1",
                    "zone": "left",
                    "facing": "the kitchen door",
                },
                {"character_id": "char_2", "zone": "right", "facing": "the window"},
                {
                    "character_id": "char_3",
                    "zone": "center",
                    "facing": "the birthday cake",
                },
            ],
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert (
        "Grandmother on the left of frame, near the dining table, facing the kitchen door"
        in lock
    )
    assert (
        "Father on the right of frame, near the dining table, facing the window" in lock
    )
    assert (
        "Maya at frame centre, near the dining table, facing the birthday cake" in lock
    )


def test_a_roadside_diner_splits_counter_and_booth() -> None:
    cfg = _cfg(
        on_screen=["char_3", "char_4"],
        blocking={
            "landmark": "window booth",
            "positions": [
                {
                    "character_id": "char_3",
                    "zone": "foreground",
                    "landmark": "counter stool",
                },
                {
                    "character_id": "char_4",
                    "zone": "background",
                    "landmark": "window booth",
                },
            ],
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert "Maya in the foreground, near the counter stool" in lock
    assert "Theo in the background, near the window booth" in lock


def test_a_road_trip_car_keeps_driver_passenger_and_back_seat() -> None:
    cfg = _cfg(
        on_screen=["char_5", "char_2", "char_4"],
        blocking={
            "landmark": "windscreen",
            "positions": [
                {"character_id": "char_5", "zone": "left", "facing": "the road ahead"},
                {"character_id": "char_2", "zone": "right", "facing": "the driver"},
                {
                    "character_id": "char_4",
                    "zone": "background",
                    "facing": "the side window",
                },
            ],
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert (
        "Driver on the left of frame, near the windscreen, facing the road ahead"
        in lock
    )
    assert "Father on the right of frame" in lock
    assert "Theo in the background" in lock


def test_a_cartoon_living_room_holds_the_same_geometry() -> None:
    # Style never changes staging: sofa, rug and television keep their places.
    cfg = _cfg(
        on_screen=["char_3", "char_6"],
        style_lock="flat 2D cartoon, bold outlines",
        blocking={
            "landmark": "sofa",
            "positions": [
                {
                    "character_id": "char_3",
                    "zone": "center",
                    "facing": "the television",
                },
                {"character_id": "char_6", "zone": "foreground", "landmark": "rug"},
            ],
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert "Maya at frame centre, near the sofa, facing the television" in lock
    assert "Dog in the foreground, near the rug" in lock


def test_an_authored_layout_carries_a_setting_without_blocking() -> None:
    # Nobody declared per-person blocking, so the setting's own layout carries it.
    cfg = _cfg(
        blocking=None,
        spatial_lock={
            "layout": "kitchen at the back, long counter on the right, front door on the left"
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en")

    assert lock.startswith("Positions held for this setting:")
    assert "long counter on the right" in lock
    assert "front door on the left." in lock
    assert "Frame sides follow the camera" in lock


def test_a_close_up_still_states_who_stays_in_the_room() -> None:
    # The camera looks at the dog; the people behind it keep their places and the
    # shot learns the fixed anchors of the room.
    cfg = _cfg(
        on_screen=["char_6"],
        spatial_lock={
            "landmarks": "television on the left wall; wide window on the right wall; kitchen doorway at the back",
            "positions": [
                {"character_id": "char_1", "zone": "left", "landmark": "sofa"},
                {"character_id": "char_3", "zone": "right", "landmark": "sofa"},
                {"character_id": "char_6", "zone": "foreground", "landmark": "rug"},
            ],
        },
    )

    lock = build_lock(cfg=cfg, names=NAMES, lang="en", visible=["char_6"])

    assert "Dog in the foreground, near the rug" in lock
    assert "Still in this room and keeping their places: Grandmother, Maya." in lock
    assert "Anchors of this room: television on the left wall" in lock
    assert "Frame sides follow the camera" in lock


def test_four_shots_of_one_setting_share_one_arrangement() -> None:
    # Each shot declares only part of the cast; the setting's merged arrangement
    # still places every character the same way in all four shots.
    shots = [
        {
            "blocking": {
                "landmark": "dining table",
                "positions": [
                    {"character_id": "char_1", "zone": "left"},
                    {"character_id": "char_2", "zone": "right"},
                ],
            }
        },
        {
            "blocking": {
                "landmark": "dining table",
                "positions": [{"character_id": "char_2", "zone": "right"}],
            }
        },
        {
            "blocking": {
                "landmark": "dining table",
                "positions": [
                    {"character_id": "char_3", "zone": "center"},
                    {"character_id": "char_1", "zone": "left"},
                ],
            }
        },
        {
            "blocking": {
                "landmark": "dining table",
                "positions": [{"character_id": "char_3", "zone": "center"}],
            }
        },
    ]
    merged = merged_positions(shots)
    assert [p["character_id"] for p in merged] == ["char_1", "char_2", "char_3"]

    locks = []
    for shot, cast in zip(
        shots, (["char_1", "char_2"], ["char_2"], ["char_1", "char_3"], ["char_3"])
    ):
        cfg = _cfg(
            on_screen=cast,
            blocking=shot["blocking"],
            spatial_lock={"landmark": "dining table", "positions": merged},
        )
        locks.append(build_lock(cfg=cfg, names=NAMES, lang="en", visible=cast))

    for lock in locks[1:]:
        for seat in (
            "Grandmother on the left of frame, near the dining table",
            "Father on the right of frame, near the dining table",
            "Maya at frame centre, near the dining table",
        ):
            for other in locks:
                if seat.split(",")[0] in other:
                    assert seat in other


def test_the_lock_reads_the_same_in_every_shot_and_leaks_no_negatives() -> None:
    cfg = _cfg(
        on_screen=["char_1", "char_2"],
        blocking={
            "landmark": "kitchen island",
            "positions": [
                {"character_id": "char_1", "zone": "left"},
                {"character_id": "char_2", "zone": "right"},
            ],
        },
    )
    stamped = stamp_lock(cfg, names=NAMES, lang="en")
    assert cfg["positioning_lock_text"] == stamped

    actions = ("They walk in.", "She pours the tea.", "He laughs.")
    prompts = [compose_practice_prompt(cfg=cfg, graph=None, action=a) for a in actions]
    for prompt in prompts:
        assert "Grandmother on the left of frame" in prompt
        assert "Father on the right of frame" in prompt
        # Sound keeps its negatives on purpose; the staging text never carries one.
        visual = prompt.split("Sound:")[0].split("声音：")[0].lower()
        for banned in (" no ", "not ", "never ", "without ", "do not"):
            assert banned not in visual
