# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Film-wide axis / occupancy locks — domain-agnostic.

Structural locks derived from analysis blocking / cast (not an LLM substitute):
  - identity under occlusion (partially hidden person must keep sex/age/hair)
  - 180-degree screen L/R (camera pan must not flip who sits left vs right)
  - landmark placements stay put; extras do not spawn/vanish
"""

from __future__ import annotations

import re
from typing import Any

_MALE_RE = re.compile(
    r"\b(?:boy|son|father|dad|man|male|brother|husband|gentleman|"
    r"young\s+man)\b",
    re.I,
)
# Note: bare "he/him/his" omitted — "his partner" was flipping partner to male.
_FEMALE_RE = re.compile(
    r"\b(?:girl|daughter|mother|mom|woman|female|she|her|hers|sister|wife|lady|"
    r"partner|girlfriend|dress|gown|blouse)\b",
    re.I,
)
_CHILD_RE = re.compile(r"\b(?:child|kid|boy|girl|son|daughter|toddler|infant)\b", re.I)
_ADULT_RE = re.compile(r"\b(?:father|mother|dad|mom|man|woman|adult|teen)\b", re.I)
_CROSS_RE = re.compile(
    r"\b(?:cross(?:es|ing)?|walks?\s+to\s+the\s+other|swaps?\s+sides|moves?\s+left|moves?\s+right)\b",
    re.I,
)
_NIGHT_WORDS = (
    "midnight",
    "late night",
    "at night",
    "nighttime",
    "night time",
    "nocturnal",
    "moonlit",
    "星夜",
    "夜晚",
    "夜里",
    "深夜",
)


def infer_time_of_day_lock(prompt: str = "", scene_desc: str = "") -> dict[str, str]:
    """Film-wide / per-setting time-of-day + lighting cue (domain-agnostic)."""
    blob = f"{prompt or ''} {scene_desc or ''}".lower()
    if any(w in blob for w in _NIGHT_WORDS):
        tod = "night"
        light = "night lighting — cool/dark key with practicals; keep night across same-setting shots"
    elif any(w in blob for w in ("dusk", "sunset", "twilight", "golden hour", "傍晚", "黄昏", "日落")):
        tod = "dusk"
        light = "dusk / golden-hour warmth; long shadows; keep dusk across same-setting shots"
    elif any(w in blob for w in ("dawn", "sunrise", "early morning", "破晓", "黎明", "日出")):
        tod = "dawn"
        light = "dawn light — cool-warm horizon glow; keep dawn across same-setting shots"
    elif any(w in blob for w in ("morning", "breakfast", "上午", "早上", "清晨")):
        tod = "morning"
        light = "morning daylight; soft clear key; keep morning across same-setting shots"
    elif any(w in blob for w in ("noon", "midday", "afternoon", "中午", "午后", "下午")):
        tod = "day"
        light = "daytime daylight; stable sun side; keep day across same-setting shots"
    elif any(w in blob for w in ("evening", "dinner", "晚饭", "晚餐", "晚上")):
        tod = "evening"
        light = "evening interior/exterior practicals; keep evening across same-setting shots"
    else:
        tod = "unspecified"
        light = "motivated key light with stable direction; no relight mid-scene"
    return {
        "time_of_day": tod,
        "lighting": light,
        "rule": (
            f"TIME OF DAY LOCK={tod}: every same-setting image/clip must keep this "
            "time-of-day and lighting direction — no day↔night jump mid-scene."
        ),
    }


def infer_demographics(ch: dict[str, Any]) -> dict[str, str]:
    blob = " ".join(
        str(ch.get(k) or "") for k in ("name", "role", "description", "costume_lock")
    )
    # Drop possessive "his/her X" so "his partner" does not mark the partner male.
    blob_sex = re.sub(r"\b(?:his|her|their)\s+", " ", blob, flags=re.I)
    male = bool(_MALE_RE.search(blob_sex))
    female = bool(_FEMALE_RE.search(blob_sex))
    if male and not female:
        sex = "male"
    elif female and not male:
        sex = "female"
    else:
        sex = "unspecified"
    if _CHILD_RE.search(blob) and not _ADULT_RE.search(blob):
        age = "child"
    elif re.search(r"\bteen", blob, re.I):
        age = "teen"
    else:
        age = "adult"
    return {
        "sex": sex,
        "age_band": age,
        "occlusion_rule": (
            f"sex={sex}, age={age} even if body is cropped, behind furniture, "
            "out of focus, or only a silhouette — never recast as a different sex/age"
        ),
    }


def _zone_to_axis(zone: str) -> str:
    z = (zone or "").lower()
    if "left" in z:
        return "screen_left"
    if "right" in z:
        return "screen_right"
    if "back" in z or "bg" in z:
        return "background_center"
    return "screen_center"


def stamp_axis_locks(analysis: dict[str, Any]) -> dict[str, Any]:
    """Heuristic 180-degree + occupancy + landmark floor on analysis (mutates copy)."""
    out = analysis if isinstance(analysis, dict) else {}
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (out.get("clips") or []) if isinstance(s, dict)]
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}

    for ch in characters:
        demo = infer_demographics(ch)
        attrs = dict(ch.get("identity_attrs") or {}) if isinstance(ch.get("identity_attrs"), dict) else {}
        attrs.update({k: v for k, v in demo.items() if v})
        ch["identity_attrs"] = attrs

    canonical_axis: dict[str, str] = {}
    landmarks: list[str] = []
    already_gone: set[str] = set()
    for shot in shots:
        blk = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
        lm = str(blk.get("landmark") or "").strip()
        if lm and lm not in landmarks:
            landmarks.append(lm)
        on = [str(x) for x in (shot.get("character_ids") or []) if str(x).strip()]
        axis_map: dict[str, str] = {}
        for p in blk.get("positions") or []:
            if not isinstance(p, dict):
                continue
            cid = str(p.get("character_id") or "")
            if not cid:
                continue
            axis = _zone_to_axis(str(p.get("zone") or ""))
            action = str(shot.get("action") or "")
            if cid in canonical_axis and not _CROSS_RE.search(action):
                axis = canonical_axis[cid]
            else:
                canonical_axis[cid] = axis
            axis_map[cid] = axis
        for cid in on:
            if cid not in axis_map:
                axis_map[cid] = canonical_axis.get(cid, "screen_center")
                canonical_axis.setdefault(cid, axis_map[cid])
        shot["screen_axis"] = axis_map
        prev_bits = [
            f"{(by_id.get(cid) or {}).get('name') or cid}={axis_map[cid]}"
            for cid in axis_map
        ]
        shot["screen_positions"] = (
            (str(shot.get("screen_positions") or "") + " ").strip()
            + (" SCREEN AXIS (180-rule, pan-stable): " + "; ".join(prev_bits) if prev_bits else "")
        )[:280]
        exiting_now = [
            str(x)
            for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
            if str(x)
        ]
        # Already-gone from earlier beats only. Current leavers still appear while exiting.
        # Draw on_screen / featured only — not the whole film cast.
        off = [
            str(x)
            for x in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
            if str(x)
        ]
        must = [c for c in (on or []) if c not in already_gone]
        prev_occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        shot["occupancy"] = {
            **prev_occ,
            "must_appear": must or list(prev_occ.get("must_appear") or []),
            "offscreen": off,
            "must_not_appear": sorted(set(already_gone) | {c for c in off if c not in must}),
            "featured": list(on) or list(prev_occ.get("featured") or []),
            "rule": (
                "OCCUPANCY: must_appear / on_screen are drawn; offscreen stay out of frame; "
                "cast from other scenes (must_not_appear) never appear. Same setting_id keeps "
                "architecture from the compose scene spec; only camera + cast_actions change."
            ),
        }
        already_gone.update(exiting_now)

    spatial = dict(out.get("spatial_lock") or {}) if isinstance(out.get("spatial_lock"), dict) else {}
    prompt_blob = str(out.get("user_prompt") or out.get("summary") or "")
    tod_global = infer_time_of_day_lock(prompt_blob)
    out["time_of_day_lock"] = tod_global
    for shot in shots:
        place = str(shot.get("setting_description") or shot.get("action") or "")
        tod = infer_time_of_day_lock(prompt_blob, place)
        # Prefer explicit shot lock if already set.
        prior = shot.get("time_of_day_lock") if isinstance(shot.get("time_of_day_lock"), dict) else {}
        if prior.get("time_of_day") and prior.get("time_of_day") != "unspecified":
            tod = {**tod, **{k: v for k, v in prior.items() if str(v).strip()}}
        shot["time_of_day_lock"] = tod
        bible = shot.get("scene_specs") if isinstance(shot.get("scene_specs"), dict) else None
        if bible is not None:
            bible = dict(bible)
            bible.setdefault("time_of_day", tod.get("time_of_day"))
            if tod.get("lighting") and (
                not bible.get("lighting")
                or "motivated key light" in str(bible.get("lighting") or "").lower()
            ):
                bible["lighting"] = tod.get("lighting")
            shot["scene_specs"] = bible
    # Keep existing spatial merge below.
    if landmarks:
        spatial["landmarks"] = landmarks[:8]
        spatial["landmark_rule"] = (
            "Each named landmark keeps its place vs the scene specs "
            "(front/back/left/right). Camera move reframes — it does not teleport furniture."
        )
    out["spatial_lock"] = spatial
    out["axis_lock"] = {
        "canonical_screen_axis": canonical_axis,
        "landmarks": landmarks[:8],
        "rule": (
            "180-degree: a person on screen_left stays screen_left across pans/cuts "
            "unless the shot says they cross. Occupancy: no random appear/disappear."
        ),
    }
    out["characters"] = characters
    out["clips"] = shots
    return out


def format_style_clause(analysis: dict[str, Any] | None) -> str:
    """Film-wide style lock line for stills and clips."""
    analysis = analysis if isinstance(analysis, dict) else {}
    style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}
    if not style:
        return ""
    try:
        from jiuwenswarm.server.runtime.designer.media_model_playbook import style_lock_clause

        return (style_lock_clause(style) or "").strip()
    except Exception:  # noqa: BLE001
        look = str(style.get("look") or style.get("medium") or "").strip()
        return f"STYLE LOCK (film-wide): {look[:280]}" if look else ""


def format_axis_clause(shot: dict[str, Any] | None, analysis: dict[str, Any] | None) -> str:
    shot = shot if isinstance(shot, dict) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    axis = shot.get("screen_axis") if isinstance(shot.get("screen_axis"), dict) else {}
    occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
    spatial = analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else {}
    chars = {str(c.get("id")): c for c in (analysis.get("characters") or []) if isinstance(c, dict)}
    bits: list[str] = []
    style_bit = format_style_clause(analysis)
    if style_bit and "STYLE LOCK" in style_bit:
        bits.append(style_bit)
    if axis:
        named = []
        for cid, side in axis.items():
            nm = str((chars.get(cid) or {}).get("name") or cid)
            attrs = (chars.get(cid) or {}).get("identity_attrs") or {}
            sex = str(attrs.get("sex") or "")
            age = str(attrs.get("age_band") or "")
            demo = f"{sex}/{age}" if sex or age else ""
            named.append(f"{nm}@{side}" + (f" ({demo})" if demo else ""))
        bits.append(
            "SCREEN AXIS (do not flip on pan/cut): " + "; ".join(named)
        )
    must = [str((chars.get(x) or {}).get("name") or x) for x in (occ.get("must_appear") or [])]
    no = [str((chars.get(x) or {}).get("name") or x) for x in (occ.get("must_not_appear") or [])]
    if must:
        bits.append("OCCUPANCY must appear: " + ", ".join(must))
    if no:
        bits.append("OCCUPANCY must NOT appear: " + ", ".join(no))
    bits.append(
        str(occ.get("rule") or "No extra named people; no dropping listed leads.")
    )
    lms = spatial.get("landmarks") or []
    if lms:
        bits.append(
            "LANDMARKS frozen vs scene specs: "
            + ", ".join(str(x) for x in lms[:6])
            + ". "
            + str(spatial.get("landmark_rule") or "")
        )
    bits.append(
        "OCCLUSION: a partly hidden person is still the same identity "
        "(sex/age/hair/wardrobe) as the character specs — never recast."
    )
    tod = (
        shot.get("time_of_day_lock")
        if isinstance(shot.get("time_of_day_lock"), dict)
        else analysis.get("time_of_day_lock")
        if isinstance(analysis.get("time_of_day_lock"), dict)
        else {}
    )
    tod_label = str((tod or {}).get("time_of_day") or "").strip()
    if tod_label and tod_label != "unspecified":
        bits.append(
            str((tod or {}).get("rule") or "").strip()
            or f"TIME OF DAY LOCK={tod_label}: {(tod or {}).get('lighting') or ''}".strip()
        )
    return " ".join(b for b in bits if str(b).strip())[:1100]


def apply_axis_locks_to_graph(graph: dict[str, Any]) -> list[str]:
    """Stamp style + axis clauses onto scene/clip configs."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    analysis = stamp_axis_locks(analysis)
    style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}
    if not style:
        style = meta.get("style_lock") if isinstance(meta.get("style_lock"), dict) else {}
    if not style:
        try:
            from jiuwenswarm.server.runtime.designer.media_model_playbook import default_style_lock

            style = default_style_lock(
                str(graph.get("description") or meta.get("user_prompt") or ""),
                str((analysis.get("scene") or {}).get("description") or ""),
            )
            analysis["style_lock"] = style
        except Exception:  # noqa: BLE001
            style = {}
    shots = {
        int(s.get("clip_index") or 0): s
        for s in (analysis.get("clips") or [])
        if isinstance(s, dict)
    }
    clause_global = format_axis_clause({}, analysis)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        role = str(
            cfg.get("role")
            or node.get("pipeline")
            or node.get("type")
            or ""
        ).lower()
        # Normalize role aliases used by graph builders.
        if "clip" in role or role == "video":
            cfg.setdefault("role", "clip")
            role = "clip"
        elif "character" in role:
            cfg.setdefault("role", "character")
            role = "character"
        elif "scene" in role:
            cfg.setdefault("role", "scene")
            role = "scene"
        idx = int(cfg.get("clip_index") or 0)
        clause = format_axis_clause(shots.get(idx), analysis) if idx else clause_global
        if role in {"scene", "clip", "character"} and clause:
            # The leaf agent reads axis_clause; the user-visible prompt stays lock-free.
            cfg["axis_clause"] = clause[:900]
            notes.append(f"axis:{node.get('id')}")
            if idx and shots.get(idx):
                cfg["screen_axis"] = shots[idx].get("screen_axis")
                cfg["occupancy"] = shots[idx].get("occupancy")
        if style:
            cfg["style_lock"] = style
        # Per-shot or film-wide time-of-day lock on every image/clip.
        tod_node = (
            (shots.get(idx) or {}).get("time_of_day_lock")
            if idx and isinstance((shots.get(idx) or {}).get("time_of_day_lock"), dict)
            else analysis.get("time_of_day_lock")
            if isinstance(analysis.get("time_of_day_lock"), dict)
            else {}
        )
        if tod_node:
            cfg["time_of_day_lock"] = tod_node
            bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
            if bible is not None and role in {"scene", "clip"}:
                bible = dict(bible)
                bible.setdefault("time_of_day", tod_node.get("time_of_day"))
                if tod_node.get("lighting") and (
                    not bible.get("lighting")
                    or "motivated key light" in str(bible.get("lighting") or "").lower()
                ):
                    bible["lighting"] = tod_node.get("lighting")
                cfg["scene_specs"] = bible
        node["config"] = cfg
    meta["script_analysis"] = analysis
    if style:
        meta["style_lock"] = style
    meta["axis_lock"] = analysis.get("axis_lock")
    if isinstance(analysis.get("time_of_day_lock"), dict):
        meta["time_of_day_lock"] = analysis["time_of_day_lock"]
    graph["metadata"] = meta
    return notes
