# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
    clothing_lock_clause,
    costume_lock_for_ids,
    enrich_character_clothing,
    extract_clothing_parts,
    format_clothing_slots,
)


def test_extract_clothing_parts_shirt_trousers_colors():
    parts = extract_clothing_parts(
        "young man in a light blue short-sleeve button-up shirt and dark charcoal trousers, black shoes"
    )
    assert "top" in parts
    assert "blue" in parts["top"]
    assert "shirt" in parts["top"]
    assert "bottom" in parts
    assert "charcoal" in parts["bottom"] or "dark" in parts["bottom"]
    assert "trousers" in parts["bottom"]
    assert "footwear" in parts
    slots = format_clothing_slots(parts)
    assert "top=" in slots and "bottom=" in slots


def test_enrich_character_sets_detailed_costume_lock():
    ch = {
        "id": "char_1",
        "name": "年轻人",
        "description": "short black hair, light blue shirt, dark trousers, sneakers",
    }
    lock = enrich_character_clothing(ch)
    assert "年轻人" in lock
    assert "top=" in lock or "shirt" in lock.lower()
    assert "bottom=" in lock or "trousers" in lock.lower()
    assert ch.get("costume_lock")
    assert isinstance(ch.get("clothing_parts"), dict)


def test_costume_lock_for_ids_and_clip_clause():
    characters = [
        {
            "id": "char_1",
            "name": "Alice",
            "description": "red blouse and black skirt, heels",
        },
        {
            "id": "char_2",
            "name": "Bob",
            "description": "navy polo shirt and khaki chinos",
        },
    ]
    for ch in characters:
        enrich_character_clothing(ch)
    joined = costume_lock_for_ids(characters, ["char_1", "char_2"])
    assert "Alice" in joined and "Bob" in joined
    clause = clothing_lock_clause(joined, for_clip=True)
    assert "CLOTHING LOCK" in clause
    assert "CLOTHING HOLD" in clause
    assert "this clip" in clause
    assert "FORBIDDEN" in clause
