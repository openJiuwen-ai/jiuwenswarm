"""Group cast dual-track: varied-crowd plate + intra-group varied action."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
    compose_character_sheet_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    _cast_form_for,
    compose_practice_prompt,
)

_GRAPH = {
    "metadata": {
        "script_analysis": {
            "characters": [
                {"id": "char_1", "name": "Village Elders", "cast_form": "group"},
                {"id": "char_2", "name": "Fisherman", "cast_form": "single"},
            ],
        },
    },
}


def test_group_plate_prompt_demands_distinct_faces() -> None:
    text = compose_character_sheet_prompt(
        cfg={
            "character_name": "Village Elders",
            "cast_form": "group",
            "cast_group_size": "six",
            "costume_lock": "muted linen robes",
        },
        seed="",
    )
    assert "distinct people" in text
    assert "varied faces" in text
    assert "same wardrobe, same styling" not in text


def test_single_plate_prompt_unchanged() -> None:
    text = compose_character_sheet_prompt(
        cfg={"character_name": "Fisherman", "cast_form": "single"},
        seed="",
    )
    assert "One person only" in text
    assert "distinct people" not in text


def test_cast_form_lookup_by_id_and_name() -> None:
    assert _cast_form_for("char_1", "Village Elders", _GRAPH) == "group"
    assert _cast_form_for("char_2", "Fisherman", _GRAPH) == "single"
    assert _cast_form_for("Village Elders", "Village Elders", _GRAPH) == "group"
    assert _cast_form_for("char_9", "", _GRAPH) == ""


def test_group_cast_sentence_carries_variation_clause() -> None:
    text = compose_practice_prompt(
        cfg={
            "on_screen": ["char_1"],
            "cast_actions": {"char_1": "play Go around the stone table"},
            "clip_action": "Village elders play Go around the stone table.",
            "scene_specs": {"scene_name": "peach village square", "lighting": "warm dusk"},
            "style_lock": {"look": "photoreal cinematic"},
            "content_language": "en",
        },
        graph=_GRAPH,
    )
    assert "each person differs in gesture" in text
    assert "no two share the same pose" in text


def test_single_cast_sentence_has_no_variation_clause() -> None:
    text = compose_practice_prompt(
        cfg={
            "on_screen": ["char_2"],
            "cast_actions": {"char_2": "poles the boat gently"},
            "clip_action": "The fisherman poles the boat gently.",
            "scene_specs": {"scene_name": "river", "lighting": "morning mist"},
            "style_lock": {"look": "photoreal cinematic"},
            "content_language": "en",
        },
        graph=_GRAPH,
    )
    assert "each person differs" not in text
