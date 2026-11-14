"""Declared blocking is real: schema carries it, Python never invents it."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.clip_policy import (
    _expand_blocking_to_visible,
)
from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
    enrich_analysis_heuristically,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    _seat_for,
)
from jiuwenswarm.server.runtime.designer.script_analysis import (
    _normalize_llm_analysis,
    _normalize_shot_blocking,
    heuristic_analysis,
)

_VALID = {"char_1", "char_2", "char_3"}
_BY_NAME = {
    "char_1": "char_1",
    "char_2": "char_2",
    "char_3": "char_3",
    "ah q": "char_1",
    "squire zhao": "char_2",
}


def test_normalize_blocking_resolves_refs_and_keeps_declared_geometry() -> None:
    out = _normalize_shot_blocking(
        {
            "landmark": "courtyard gate",
            "positions": [
                {
                    "character_id": "Ah Q",
                    "zone": "CENTER",
                    "facing": "away from Squire Zhao",
                    "looks_at": "Squire Zhao",
                    "talks_to": "nobody-real",
                },
                {"character_id": "ghost_char", "zone": "left"},
            ],
        },
        valid_ids=_VALID,
        by_name=_BY_NAME,
    )
    assert out["landmark"] == "courtyard gate"
    assert len(out["positions"]) == 1
    pos = out["positions"][0]
    assert pos["character_id"] == "char_1"
    assert pos["zone"] == "center"
    assert pos["looks_at"] == "char_2"
    # Unknown cast ref dropped; self-reference dropped.
    assert "talks_to" not in pos


def test_normalize_blocking_empty_when_nothing_declared() -> None:
    assert _normalize_shot_blocking(None, valid_ids=_VALID, by_name=_BY_NAME) == {}
    assert _normalize_shot_blocking({"positions": []}, valid_ids=_VALID, by_name=_BY_NAME) == {}
    assert _normalize_shot_blocking(
        {"positions": [{"character_id": "ghost", "zone": "left"}]},
        valid_ids=_VALID,
        by_name=_BY_NAME,
    ) == {}


def test_normalize_llm_analysis_carries_blocking_into_clips() -> None:
    base = heuristic_analysis("A two shot film in a courtyard.")
    parsed = {
        "story_name": "Courtyard",
        "characters": [
            {"id": "char_1", "name": "Ah Q", "description": "grey jacket"},
            {"id": "char_2", "name": "Squire Zhao", "description": "dark gown"},
        ],
        "target_clip_count": 2,
        "clips": [
            {
                "clip_index": i,
                "action": f"beat {i}",
                "on_screen": ["char_1", "char_2"],
                "setting_id": "set_1",
                "blocking": {
                    "landmark": "stone table",
                    "positions": [
                        {"character_id": "char_1", "zone": "left", "next_to": "char_2"},
                        {"character_id": "char_2", "zone": "right"},
                    ],
                },
            }
            for i in (1, 2)
        ],
    }
    out = _normalize_llm_analysis(parsed, base)
    assert out is not None
    blocking = out["clips"][0].get("blocking") or {}
    assert blocking.get("landmark") == "stone table"
    assert [p["character_id"] for p in blocking.get("positions") or []] == ["char_1", "char_2"]
    assert blocking["positions"][0].get("next_to") == "char_2"


def _base_analysis(*, with_blocking: bool) -> dict:
    shot1: dict = {
        "clip_index": 1,
        "action": "Ah Q argues with Squire Zhao in the courtyard over a debt.",
        "on_screen": ["char_1", "char_2"],
        "character_ids": ["char_1", "char_2"],
        "setting_id": "set_1",
    }
    if with_blocking:
        shot1["blocking"] = {
            "landmark": "courtyard gate",
            "positions": [
                {"character_id": "char_1", "zone": "left", "looks_at": "char_2"},
                {"character_id": "char_2", "zone": "right"},
                {"character_id": "char_3", "zone": "background"},
            ],
        }
    return {
        "characters": [
            {"id": "char_1", "name": "Ah Q", "description": "grey jacket"},
            {"id": "char_2", "name": "Squire Zhao", "description": "dark gown"},
            {"id": "char_3", "name": "Wu Ma", "description": "apron"},
        ],
        "clips": [shot1],
        "target_clip_count": 1,
    }


def test_enrich_never_invents_blocking_when_absent() -> None:
    out = enrich_analysis_heuristically("one shot film", _base_analysis(with_blocking=False))
    shot = out["clips"][0]
    assert "blocking" not in shot
    assert "BLOCKING:" not in str(shot.get("action") or "")
    assert "BLOCKING:" not in str(shot.get("clip_prompt") or "")


def test_enrich_keeps_declared_blocking_pruned_to_on_screen() -> None:
    out = enrich_analysis_heuristically("one shot film", _base_analysis(with_blocking=True))
    shot = out["clips"][0]
    blocking = shot.get("blocking") or {}
    ids = [p.get("character_id") for p in blocking.get("positions") or []]
    # char_3 is not on screen this shot: declared position pruned, not replaced.
    assert ids == ["char_1", "char_2"]
    assert blocking.get("landmark") == "courtyard gate"
    assert "BLOCKING:" not in str(shot.get("action") or "")


def test_expand_blocking_to_visible_keeps_declared_only() -> None:
    shot = {
        "blocking": {
            "landmark": "stone table",
            "positions": [
                {"character_id": "char_1", "zone": "left", "facing": "inland"},
                {"character_id": "char_2", "zone": "right"},
            ],
        },
    }
    _expand_blocking_to_visible(shot, ["char_1"], {"char_1": "bows deeply"})
    blocking = shot.get("blocking") or {}
    positions = blocking.get("positions") or []
    assert [p.get("character_id") for p in positions] == ["char_1"]
    # Declared zone/facing survive; pose refreshes from cast_actions.
    assert positions[0].get("zone") == "left"
    assert positions[0].get("facing") == "inland"
    assert positions[0].get("pose") == "bows deeply"
    assert blocking.get("landmark") == "stone table"


def test_expand_blocking_to_visible_drops_key_when_nothing_declared() -> None:
    shot: dict = {}
    _expand_blocking_to_visible(shot, ["char_1", "char_2"], {"char_1": "waves"})
    assert "blocking" not in shot


def test_seat_for_renders_declared_gaze_and_address() -> None:
    graph = {
        "metadata": {
            "script_analysis": {
                "characters": [
                    {"id": "char_1", "name": "Ah Q"},
                    {"id": "char_2", "name": "Squire Zhao"},
                ],
            },
        },
    }
    cfg = {
        "blocking": {
            "landmark": "stone table",
            "positions": [
                {
                    "character_id": "char_1",
                    "zone": "left",
                    "looks_at": "char_2",
                    "talks_to": "char_2",
                },
            ],
        },
    }
    text = _seat_for("char_1", cfg, graph, lang="en")
    assert "at frame left" in text
    assert "near the stone table" in text
    assert "looking at Squire Zhao" in text
    assert "addressing Squire Zhao" in text


def test_seat_for_silent_when_no_gaze_declared() -> None:
    cfg = {
        "blocking": {
            "positions": [{"character_id": "char_1", "zone": "center"}],
        },
    }
    text = _seat_for("char_1", cfg, None, lang="en")
    assert text == "at center frame"
    # Unresolvable id refs never leak as prose.
    cfg2 = {"blocking": {"positions": [{"character_id": "char_1", "looks_at": "char_9"}]}}
    assert _seat_for("char_1", cfg2, None, lang="en") == ""
