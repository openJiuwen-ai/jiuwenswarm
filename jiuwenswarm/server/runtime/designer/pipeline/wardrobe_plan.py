# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-shot wardrobe authored by the Director model.

A film-wide identity card cannot express a costume change between settings, and
a clipboard rule cannot tell which garment belongs to which shot. The Director
already decides who appears in a shot and what they wear on their identity card,
so the same model answers the narrower question per shot: for each character
whose look differs from their card, the garment clause this shot wears.

Nothing here re-derives wardrobe from text. The model supplies it; this module
only checks the shape of the answer and trims it, so a malformed field can never
reach a video prompt.
"""

from __future__ import annotations

from typing import Any

MAX_LOOKS_PER_SHOT = 12
MAX_LOOK_CHARS = 240


def wardrobe_by_character(shot: dict[str, Any] | None) -> dict[str, str]:
    """Read ``wardrobe_by_character`` from one Director shot.

    Returns character id -> garment clause for the shots where the Director said
    the look differs from the identity card. An empty mapping means "the card is
    correct for this shot", which keeps the plate authoritative.
    """
    if not isinstance(shot, dict):
        return {}
    raw = shot.get("wardrobe_by_character")
    if not isinstance(raw, dict):
        return {}
    looks: dict[str, str] = {}
    for cid, clause in raw.items():
        key = str(cid or "").strip()
        text = " ".join(str(clause or "").split())
        if not key or not text:
            continue
        looks[key] = text[:MAX_LOOK_CHARS]
        if len(looks) >= MAX_LOOKS_PER_SHOT:
            break
    return looks
