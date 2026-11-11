# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A costume edited on a character's identity plate reaches its dependent clips.

Reproduced on the canvas: the identity plate was re-rendered in a crimson
sweater and black oilskin coat, yet the clip prompt still asked for the cream
sweater it was authored with, because the wardrobe was stamped into
``script_analysis`` when the graph was built and a later regenerate never
refreshed it.
"""

from __future__ import annotations

import copy

from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
    costume_clause_from_spec_prompt,
    costume_lock_for_ids,
    detailed_costume_lock_for_character,
    spec_costume_by_character,
    sync_costume_from_specs,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
)

_PLATE_PROMPT = (
    "One person only on a plain empty studio backdrop. "
    "Identity (locked): man in his early 60s, grey stubble, deep-set pale blue eyes. "
    "Costume (locked, exact): deep crimson fisherman's cable-knit sweater, charcoal grey "
    "wool trousers, black oilskin storm coat worn open over the sweater, brown leather boots, "
    "black watch cap pulled low; a brass storm lantern with warm glass carried in his right hand. "
    "Photoreal cinematic look: 35mm lens, soft studio key light."
)
_OLD_LOCK = "Lighthouse Keeper: top=cream wool sweater; bottom=navy trousers; outerwear=olive storm coat"


def _graph(plate_prompt: str = _PLATE_PROMPT) -> dict:
    return {
        "nodes": [
            {
                "id": "n_character",
                "type": "image",
                "config": {
                    "pipeline": "character",
                    "character_ids": ["char_1"],
                    "generate": {"prompt": plate_prompt},
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "pipeline": "clip",
                    "character_ids": ["char_1"],
                    "on_screen": ["char_1"],
                    "costume_lock": _OLD_LOCK,
                    "clip_action": "The keeper climbs the stair.",
                },
            },
        ],
        "metadata": {
            "script_analysis": {
                "characters": [
                    {
                        "id": "char_1",
                        "name": "Lighthouse Keeper",
                        "costume_lock": "cream wool sweater, navy trousers, olive storm coat",
                    }
                ]
            }
        },
    }


def test_the_plate_declared_costume_is_read_verbatim() -> None:
    clause = costume_clause_from_spec_prompt(_PLATE_PROMPT)
    assert clause.startswith("deep crimson fisherman's cable-knit sweater")
    assert "black oilskin storm coat" in clause
    assert "black watch cap" in clause
    # The prop after the semicolon is not part of the wardrobe lock.
    assert "lantern" not in clause
    assert "Photoreal" not in clause


def test_an_unedited_plate_or_a_plain_prompt_yields_nothing() -> None:
    assert costume_clause_from_spec_prompt("") == ""
    assert costume_clause_from_spec_prompt("A lighthouse on a cliff at dawn.") == ""


def test_a_plate_that_separates_garments_with_semicolons_keeps_them_all() -> None:
    # Real plates use either commas or semicolons between garments, and the
    # semicolon also introduces props - only garment segments belong in the lock.
    prompt = (
        "SUBJECT: One man. COSTUME (exact, nothing else): cream cable-knit wool sweater with "
        "visible twisted cable pattern at chest and cuffs, worn ribbed collar at the neck; navy wool "
        "trousers, straight leg, slightly creased; olive canvas coat, mid-length field coat with "
        "corduroy collar; dark brown leather boots, laced, scuffed toes; olive-brown tweed flat cap "
        "pulled level on the head. FRAMING: three-quarter body. BACKGROUND: plain grey backdrop."
    )
    clause = costume_clause_from_spec_prompt(prompt)
    assert "cable-knit wool sweater" in clause
    assert "navy wool trousers" in clause
    assert "olive canvas coat" in clause
    assert "leather boots" in clause
    assert "flat cap" in clause
    assert "FRAMING" not in clause and "BACKGROUND" not in clause


def test_the_clause_label_may_be_uppercase_or_wardrobe() -> None:
    assert costume_clause_from_spec_prompt("WARDROBE: white linen shirt, navy skirt.") == (
        "white linen shirt, navy skirt"
    )
    assert costume_clause_from_spec_prompt("Clothing (locked): grey coat, black boots. Pose: neutral.") == (
        "grey coat, black boots"
    )


def test_syncing_copies_the_plate_costume_onto_the_analysis_cast() -> None:
    graph = _graph()
    synced = sync_costume_from_specs(graph)
    assert synced == {"char_1"}
    ch = graph["metadata"]["script_analysis"]["characters"][0]
    assert "deep crimson" in ch["costume_lock"]
    assert "cream" not in ch["costume_lock"]
    # Fidelity survives into the lock a clip receives.
    lock = costume_lock_for_ids([ch], ["char_1"])
    assert "deep crimson fisherman's cable-knit sweater" in lock
    assert "black oilskin" in lock
    assert "cream" not in lock


def test_spec_map_exposes_each_plates_costume_by_character_id() -> None:
    assert "deep crimson" in spec_costume_by_character(_graph())["char_1"]
    assert spec_costume_by_character({}) == {}


def test_clip_prompt_wears_the_edited_plate_costume_not_the_stale_lock() -> None:
    graph = _graph()
    clip_cfg = copy.deepcopy(graph["nodes"][1]["config"])
    prompt = compose_practice_prompt(cfg=clip_cfg, graph=graph, action=clip_cfg["clip_action"])
    assert "deep crimson" in prompt
    assert "cream wool sweater" not in prompt


def test_a_plate_without_a_costume_clause_leaves_the_stored_lock_alone() -> None:
    graph = _graph(plate_prompt="A lighthouse keeper identity plate, neutral pose.")
    assert sync_costume_from_specs(graph) == set()
    ch = graph["metadata"]["script_analysis"]["characters"][0]
    assert ch["costume_lock"] == "cream wool sweater, navy trousers, olive storm coat"
    assert detailed_costume_lock_for_character(ch).startswith("Lighthouse Keeper:")
