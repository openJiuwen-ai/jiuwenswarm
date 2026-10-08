# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Plan A v2 enrichments — domain-agnostic (safe for browser Play later).

Fixes observed offline failures without prompt-specific hardcodes:
  - style_lock from stylization language in the brief
  - cast dedupe (Name / Name 2)
  - identity micro-attrs (facial hair, glasses) locked across shots
  - setting_type taxonomy + cross-setting bleed forbids
  - same-setting prior-keyframe continuity (style/set carry)
  - plain portrait solo sheets (no infographic cards)
  - architecture-first scene imaging (materials/light; less narrative bleed)
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

# Generic place families — used only to stop *cross-family* landmark bleed.
_SETTING_FAMILIES: dict[str, tuple[str, ...]] = {
    "domestic": (
        "living room",
        "dining",
        "kitchen",
        "home",
        "house",
        "apartment",
        "family table",
        "wooden table",
        "sofa",
    ),
    "office": (
        "office",
        "cubicle",
        "desk",
        "workplace",
        "open-plan",
        "laptop",
        "monitor",
        "at work",
        "off work",
        "get off work",
        "leaving work",
    ),
    "restaurant": (
        "restaurant",
        "bistro",
        "cafe",
        "café",
        "dinner date",
        "dining room",
        "candlelight",
        "candlelit",
        "dinner",
    ),
    "sacred": (
        "church",
        "chapel",
        "cathedral",
        "sanctuary",
        "pulpit",
        "pew",
        "nave",
        "altar",
        "sermon",
    ),
    "outdoor": (
        "street",
        "park",
        "beach",
        "harbor",
        "harbour",
        "pier",
        "bridge",
        "lighthouse",
        "boat",
        "net",
        "wharf",
        "dock",
        "forest",
        "road",
    ),
}

_SETTING_FORBIDS: dict[str, str] = {
    "domestic": (
        "FORBIDDEN BLEED: no church pews, no cathedral nave, no pulpit/lectern as primary set, "
        "no clerical collar/cassock, no stained-glass sanctuary windows as the room's identity"
    ),
    "office": (
        "FORBIDDEN BLEED: no restaurant dining room, no sanctuary pews/pulpit, "
        "no outdoor landscape replacing the office"
    ),
    "restaurant": (
        "FORBIDDEN BLEED: no office cubicles, no sanctuary pews/pulpit, "
        "no living-room sofa set replacing the restaurant"
    ),
    "sacred": (
        "FORBIDDEN BLEED: no modern office cubicles, no restaurant bistro tables as the nave, "
        "do not relocate the sanctuary into a house dining room"
    ),
    "outdoor": (
        "FORBIDDEN BLEED: do not replace the outdoor location with an unrelated indoor set"
    ),
}

_FACIAL_HAIR_RE = re.compile(
    r"\b(?P<kind>full\s+beard|beard|goatee|mustache|moustache|stubble|"
    r"five[- ]o.?clock\s+shadow)\b",
    re.I,
)
_GLASSES_RE = re.compile(r"\b(?:glasses|spectacles|eyeglasses)\b", re.I)
_TRAILING_NUM_RE = re.compile(r"^(?P<base>.+?)\s*(?:[_-]|\s+)(?P<num>\d+)$")


def infer_style_lock(prompt: str, scene_desc: str = "") -> dict[str, str]:
    """Build the one film-wide style authority used by brief and media leaves."""
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        default_style_lock,
    )

    return default_style_lock(prompt, scene_desc)


def _strip_planning_place_mentions(text: str) -> str:
    """Remove 'book/search/arrange a <place>' phrases so planned destinations
    are not treated as the current location.
    """
    return re.sub(
        r"\b(?:book(?:s|ing)?|search(?:es|ing)?\s+for|look(?:s|ing)?\s+up|find(?:s|ing)?|"
        r"reserve(?:s|ing)?|order(?:s|ing)?|arrange(?:s|ing)?)\s+"
        r"(?:a\s+|an\s+|the\s+)?(?:\w+[-\s]+){0,3}\w+",
        " ",
        text or "",
        flags=re.I,
    )


def detect_setting_type(text: str) -> str | None:
    """Detect place family from text. Mentions of a destination while booking/searching
    do not count as being in that place.
    """
    blob = _strip_planning_place_mentions((text or "").lower())
    scores: dict[str, int] = {}
    for fam, keys in _SETTING_FAMILIES.items():
        sc = sum(1 for k in keys if k in blob)
        if sc:
            scores[fam] = sc
    if not scores:
        return None
    return max(scores.items(), key=lambda kv: kv[1])[0]


_SETTING_ARRIVAL_RE = re.compile(
    r"\b(?:"
    r"arrives?\s+(?:at|in)|enters?\s+(?:the\s+)?|cuts?\s+to|meanwhile\s+(?:at|in)|"
    r"now\s+(?:at|in)|later\s+(?:at|in)|inside\s+the|at\s+the\s+\w+|"
    r"seated\s+(?:at|in)|dining\s+(?:at|in)|"
    r"enjoying\s+(?:a\s+)?(?:\w+\s+){0,3}(?:dinner|meal|date)|"
    r"ends?\s+with\b"
    r")\b",
    re.I,
)


def infer_setting_arc(prompt: str) -> list[str]:
    """Ordered unique place families by first *presence* mention in the brief.

    Planning nouns ('book/search a <place>') do not count. Uses the user prompt only
    so invented set dressing cannot reverse location order.
    """
    blob = _strip_planning_place_mentions((prompt or "").lower())
    hits: list[tuple[int, str]] = []
    for fam, keys in _SETTING_FAMILIES.items():
        idxs = [blob.find(k) for k in keys if k in blob]
        if idxs:
            hits.append((min(idxs), fam))
    hits.sort(key=lambda x: x[0])
    ordered: list[str] = []
    for _, fam in hits:
        if fam not in ordered:
            ordered.append(fam)
    return ordered


def enrich_spatial_lock_for_setting(
    spatial_lock: dict[str, Any] | None,
    *,
    prompt: str,
    scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    lock = dict(spatial_lock or {})
    scene0 = scenes[0] if scenes else {}
    # Arc from prompt only; primary may still use scene text for single-set briefs.
    arc = infer_setting_arc(prompt)
    blob = " ".join(
        [
            prompt,
            str(scene0.get("name") or ""),
            str(scene0.get("description") or ""),
            str(lock.get("setting") or ""),
            str(lock.get("architecture") or ""),
        ]
    )
    primary = arc[0] if arc else (detect_setting_type(blob) or "generic")
    lock["setting_type"] = primary
    lock["setting_arc"] = arc
    if primary in _SETTING_FORBIDS:
        lock["forbid_bleed"] = _SETTING_FORBIDS[primary]
    lock["image_focus"] = (
        "IMAGE FOCUS: architecture, furniture, materials, and lighting only. "
        "No readable text overlays, no ceremonial costume close-ups, no labeled diagrams."
    )
    lock.setdefault(
        "static_rule",
        "STATIC OBJECTS LOCKED: landmarks and light direction match across shots — "
        "only camera/framing/action may change.",
    )
    return lock


def assign_shot_settings(
    prompt: str,
    shots: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> None:
    """Stamp setting_id / setting_type per shot; advance only on arrival/presence.

    Always recomputes chronologically. Do not keep LLM/heuristic stamps that jump
    setting on planned destinations (book/search/arrange) while still in the prior
    place. Do not evenly split a multi-place arc across shot indices.
    """
    arc = infer_setting_arc(prompt)
    blob_all = _strip_planning_place_mentions(
        " ".join(
            [prompt]
            + [
                str(s.get("name") or "") + " " + str(s.get("description") or "")
                for s in scenes
            ]
        )
    )
    primary = arc[0] if arc else (detect_setting_type(blob_all) or "generic")

    prev_id = "set_1"
    prev_type = primary
    set_i = 1
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        blob = " ".join(
            [
                str(shot.get("action") or ""),
                str(shot.get("keyframe_prompt") or ""),
                str(shot.get("title") or ""),
            ]
        )
        # Ignore lock suffixes so "FORBIDDEN BLEED: no restaurant…" cannot flip place.
        blob = re.split(
            r"\s(?:BLOCKING:|IDENTITY LOCK:|FORBIDDEN BLEED:|ASPECT LOCK:)",
            blob,
            maxsplit=1,
        )[0]
        # Presence type ignores planning nouns; arrival requires explicit presence cues.
        st = detect_setting_type(blob)
        arrived = bool(_SETTING_ARRIVAL_RE.search(blob))
        if arrived and st and st != prev_type and st != "generic":
            set_i += 1
            prev_id = f"set_{set_i}"
            prev_type = st
        shot["setting_id"] = prev_id
        shot["setting_type"] = prev_type
        if prev_type in _SETTING_FORBIDS:
            shot["forbid_bleed"] = _SETTING_FORBIDS[prev_type]


def dedupe_characters(characters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Merge 'Name' / 'Name 2' duplicates. Returns (chars, old_id -> kept_id)."""
    alias: dict[str, str] = {}
    by_base: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for ch in characters:
        if not isinstance(ch, dict) or not ch.get("id"):
            continue
        cid = str(ch["id"])
        name = str(ch.get("name") or cid).strip()
        m = _TRAILING_NUM_RE.match(name)
        base = (m.group("base") if m else name).strip().lower()
        base = re.sub(r"\s+", " ", base)
        if base in by_base:
            keep = by_base[base]
            keep_id = str(keep["id"])
            alias[cid] = keep_id
            if len(str(ch.get("description") or "")) > len(str(keep.get("description") or "")):
                keep["description"] = ch.get("description")
            continue
        by_base[base] = ch
        ordered.append(ch)
        alias[cid] = cid
    return ordered, alias


def remap_shot_character_ids(shots: list[dict[str, Any]], alias: dict[str, str]) -> None:
    for shot in shots:
        for key in ("character_ids", "on_screen", "exiting", "exiting_character_ids", "staying"):
            raw = shot.get(key)
            if not isinstance(raw, list):
                continue
            mapped = []
            for x in raw:
                cid = alias.get(str(x), str(x))
                if cid and cid not in mapped:
                    mapped.append(cid)
            shot[key] = mapped


def infer_identity_attrs(ch: dict[str, Any]) -> dict[str, str]:
    blob = " ".join(
        str(ch.get(k) or "") for k in ("name", "role", "description", "costume_lock")
    )
    attrs: dict[str, str] = {}
    m = _FACIAL_HAIR_RE.search(blob)
    if m:
        kind = re.sub(r"\s+", "_", m.group("kind").lower())
        attrs["facial_hair"] = kind
    else:
        attrs["facial_hair"] = "none"
    attrs["glasses"] = "yes" if _GLASSES_RE.search(blob) else "no"
    desc = str(ch.get("description") or "").strip()
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            extract_clothing_parts,
            format_clothing_slots,
        )

        parts = extract_clothing_parts(f"{desc} {ch.get('costume_lock') or ''}")
        slots = format_clothing_slots(parts, fallback=desc or str(ch.get("name") or ""))
        attrs["wardrobe"] = slots[:280] if slots else (desc[:220] if desc else str(ch.get("name") or "character")[:80])
        if parts:
            attrs["clothing_parts"] = "; ".join(f"{k}={v}" for k, v in parts.items())[:280]
    except Exception:  # noqa: BLE001
        if desc and not re.match(r"^(?:the\s+)?(?:father|mother|child|son|shot)\b", desc, re.I):
            attrs["wardrobe"] = desc[:220]
        else:
            attrs["wardrobe"] = str(ch.get("name") or "character")[:80]
    return attrs


def identity_lock_clause(ch: dict[str, Any]) -> str:
    attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
    hair = str(attrs.get("facial_hair") or "none")
    if hair == "none":
        hair_bit = "clean-shaven — NO beard, NO stubble, NO mustache"
    else:
        hair_bit = f"facial_hair={hair} (keep exactly; do not change)"
    glasses = str(attrs.get("glasses") or "no")
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            detailed_costume_lock_for_character,
        )

        wardrobe = detailed_costume_lock_for_character(ch)
        # Drop leading "Name: " — clause already prefixes name.
        if wardrobe.lower().startswith(str(ch.get("name") or "").lower() + ":"):
            wardrobe = wardrobe.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        wardrobe = str(attrs.get("wardrobe") or ch.get("description") or "")[:200]
    sex = str(attrs.get("sex") or "")
    age = str(attrs.get("age_band") or "")
    occ = str(attrs.get("occlusion_rule") or "")
    demo = ""
    if sex or age:
        demo = f"; identity={sex or 'unspecified'}/{age or 'adult'} — keep under occlusion"
    return (
        f"{ch.get('name')}: {hair_bit}; glasses={glasses}; clothing lock: {wardrobe}{demo}"
        + (f"; {occ}" if occ else "")
    )


def stamp_identity_attrs(characters: list[dict[str, Any]]) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import infer_demographics
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        enrich_character_clothing,
    )

    for ch in characters:
        if not isinstance(ch, dict):
            continue
        attrs = infer_identity_attrs(ch)
        attrs.update(infer_demographics(ch))
        ch["identity_attrs"] = attrs
        enrich_character_clothing(ch)
        if not str(ch.get("costume_lock") or "").strip():
            ch["costume_lock"] = str(attrs.get("wardrobe") or ch.get("name") or "")[:240]
        desc = str(ch.get("description") or "")
        if re.match(
            r"^(?:the\s+)?(?:father|mother|child|son|daughter|man|woman).{0,40}:"
            r"|^\d{1,2}:\d{2}|^(?:close-up|medium|camera)\b",
            desc,
            re.I,
        ):
            ch["description"] = str(ch.get("costume_lock") or attrs.get("wardrobe") or desc)[:300]


def align_blocking_to_on_screen(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
) -> None:
    """Keep blocking.positions in sync with shot character_ids (no scene-specific rules).

    Director LLMs often invent parallel cast names (Father 2) or stale ids in blocking
    while character_ids list the real on-screen set — that causes wrong heroes in KFs.
    """
    on_screen = [str(x) for x in (shot.get("character_ids") or []) if str(x).strip()]
    if not on_screen:
        return
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    blk = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    landmark = str(blk.get("landmark") or "").strip() or "primary_set_landmark"
    old_pos = [p for p in (blk.get("positions") or []) if isinstance(p, dict)]
    by_old = {str(p.get("character_id") or ""): p for p in old_pos}
    zones = ("center", "left", "right", "foreground", "background")
    positions: list[dict[str, str]] = []
    for i, cid in enumerate(on_screen):
        ch = by_id.get(cid) or {}
        prev = by_old.get(cid) or {}
        zone = str(prev.get("zone") or zones[min(i, len(zones) - 1)])
        pose = str(prev.get("pose") or "engaged_in_beat")
        facing = str(prev.get("facing") or "").strip()
        if not facing or "toward_camera" in facing.lower():
            facing = (
                f"toward_{landmark}"
                if landmark and landmark != "primary_set_landmark"
                else "toward_primary_action_focus"
            )
        positions.append(
            {
                "character_id": cid,
                "name": str(ch.get("name") or prev.get("name") or cid),
                "zone": zone,
                "pose": pose,
                "facing": facing,
            }
        )
    shot["blocking"] = {
        "landmark": landmark,
        "positions": positions,
        "rule": (
            "Place ONLY the on_screen character_ids in their zones relative to the landmark. "
            "Featured subjects face the landmark / action focus. "
            "Do not invent extra named leads. Do not clone one face onto two bodies."
        ),
    }


def apply_plan_a_v2(prompt: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Apply v2 locks on top of an existing Plan A director analysis."""
    out = deepcopy(analysis)
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    scenes = [s for s in (out.get("scenes") or []) if isinstance(s, dict)]

    characters, alias = dedupe_characters(characters)
    remap_shot_character_ids(shots, alias)
    stamp_identity_attrs(characters)

    style_lock = (
        dict(out["style_lock"])
        if isinstance(out.get("style_lock"), dict) and out.get("style_lock")
        else infer_style_lock(
            prompt, str((scenes[0] if scenes else {}).get("description") or "")
        )
    )
    out["style_lock"] = style_lock

    spatial = enrich_spatial_lock_for_setting(
        out.get("spatial_lock") if isinstance(out.get("spatial_lock"), dict) else {},
        prompt=prompt,
        scenes=scenes,
    )
    out["spatial_lock"] = spatial
    # Drop style/logo/meta lines that leaked into shot actions (LLM or heuristic).
    try:
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _is_non_story_beat,
            _strip_prompt_filler,
        )

        cleaned_shots: list[dict[str, Any]] = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            raw_action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
            # Strip appended BLOCKING / IDENTITY LOCK suffixes for the meta check.
            core = re.split(
                r"\s(?:BLOCKING:|IDENTITY LOCK:|FORBIDDEN BLEED:)",
                raw_action,
                maxsplit=1,
            )[0]
            core = _strip_prompt_filler(core)
            if _is_non_story_beat(core):
                continue
            cleaned_shots.append(shot)
        if cleaned_shots:
            shots = cleaned_shots
    except Exception:  # noqa: BLE001
        pass
    assign_shot_settings(prompt, shots, scenes)

    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        align_blocking_to_on_screen(shot, characters)

    out["characters"] = characters
    out["shots"] = shots
    out["experiment_plan"] = "A"
    out["plan_a_version"] = "v12"
    out["skip_domain_role_locks"] = True
    out["scene_continuity_mode"] = "scene_card_plus_clip_shots"
    # Repair cast/props BEFORE compose policy so human leads stay heroes
    # and brand mascots stay on-device UI (not free-flying characters).
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.cast_prop_locks import (
            apply_cast_prop_and_setting_locks,
        )

        out = apply_cast_prop_and_setting_locks(out, user_prompt=prompt)
        characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
        shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
        # Recompute settings after cast/action repair (planned destination ≠ presence).
        assign_shot_settings(prompt, shots, scenes)
        out["shots"] = shots
    except Exception:  # noqa: BLE001
        pass
    # Per setting: compose ALL human cast solos into the scene specs, then edit-prior.
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.keyframe_policy import (
            apply_compose_solos_setting_policy,
        )

        out = apply_compose_solos_setting_policy(out)
        # One-pass setting lock only (cast already repaired above — do not re-repair).
        from jiuwenswarm.server.runtime.designer.pipeline.cast_prop_locks import (
            enforce_setting_transitions,
        )

        enforce_setting_transitions(out)
    except Exception:  # noqa: BLE001
        out["keyframe_policy"] = "scene_card_plus_clip_shots"
        out["scene_continuity_mode"] = "scene_card_plus_clip_shots"

    # Identity / bleed clauses on featured (camera-focus) cast after ensemble stamp.
    by_id = {str(c.get("id")): c for c in characters}
    for shot in out.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        focus = [
            str(x)
            for x in (shot.get("featured_cast_ids") or shot.get("character_ids") or [])
            if str(x)
        ]
        id_bits = [
            identity_lock_clause(by_id[cid])
            for cid in focus
            if cid in by_id
        ]
        if id_bits:
            clause = " IDENTITY LOCK: " + " | ".join(id_bits)
            action = str(shot.get("action") or "")
            if "IDENTITY LOCK:" not in action:
                shot["action"] = (action + clause)[:650]
            kp = str(shot.get("keyframe_prompt") or action)
            if "IDENTITY LOCK:" not in kp:
                shot["keyframe_prompt"] = (kp + clause)[:650]
        bleed = str(shot.get("forbid_bleed") or spatial.get("forbid_bleed") or "")
        if bleed and "FORBIDDEN BLEED" not in str(shot.get("action") or ""):
            shot["action"] = (str(shot.get("action") or "") + " " + bleed)[:700]

    try:
        from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
            infer_aspect_lock,
            stamp_axis_locks,
        )

        out["aspect_lock"] = infer_aspect_lock(prompt)
        out = stamp_axis_locks(out)
    except Exception:  # noqa: BLE001
        out.setdefault("aspect_lock", {})
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_shot_sheet import (
            stamp_director_shot_sheets,
        )

        out = stamp_director_shot_sheets(out, prompt=prompt)
    except Exception:  # noqa: BLE001
        pass
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.movie_continuity_guide import (
            continuity_guide_clause,
        )

        out["continuity_guide"] = continuity_guide_clause(out)
    except Exception:  # noqa: BLE001
        out["continuity_guide"] = ""
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
            build_production_bible,
        )

        out["production_bible"] = build_production_bible(out, user_prompt=prompt)
    except Exception:  # noqa: BLE001
        out.setdefault("production_bible", "")
    out["director_contract"] = {
        **(out.get("director_contract") if isinstance(out.get("director_contract"), dict) else {}),
        "version": "plan_a.v2.compose_solos_setting.v12_cast_prop_locks",
        "style_look": style_lock.get("look"),
        "style_medium": style_lock.get("medium"),
        "setting_type": spatial.get("setting_type"),
        "cast_deduped": len(alias) > len(characters),
        "has_continuity_guide": bool(out.get("continuity_guide")),
        "aspect": (out.get("aspect_lock") or {}).get("ratio"),
        "has_axis_lock": bool(out.get("axis_lock")),
        "keyframe_policy": out.get("keyframe_policy"),
        "has_production_bible": bool(out.get("production_bible")),
        "cast_prop_repair": list(
            (out.get("director_contract") or {}).get("cast_prop_repair") or []
        )[:20],
    }
    return out
