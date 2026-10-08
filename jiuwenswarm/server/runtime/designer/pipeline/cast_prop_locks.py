# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cast vs prop locks + setting-transition repair (domain-agnostic).

General rules (no scene-genre branches):
  1. Brand mascots / logos are props — never featured human heroes.
  2. Props appear only as on-device UI (phone/laptop/app), not free-flying bodies.
  3. setting_id change ⇒ compose_from_solo_refs; same setting ⇒ edit_prior_keyframe.
  4. Within a setting, humans who did not exit stay in occupancy.must_appear.
  5. New setting starts from this shot's storyboard cast (no cross-set occupancy leak).
"""

from __future__ import annotations

import re
from typing import Any

_MASCOT_RE = re.compile(
    r"\b(?:mascot|logo|brand\s+mark|app\s+icon|emblem|watermark|icon)\b",
    re.I,
)
_CREATURE_PROP_RE = re.compile(
    r"\b(?:fuzzy|cute|little)\s+(?:\w+\s+){0,2}(?:mascot|critter|creature)\b",
    re.I,
)
_HUMAN_RE = re.compile(
    r"\b(?:man|woman|boy|girl|father|mother|son|daughter|partner|person|people|"
    r"child|teen|he|she|they)\b",
    re.I,
)
_DEVICE_UI_RE = re.compile(
    r"\b(?:phone|smartphone|laptop|tablet|screen|app|ui|interface|"
    r"booking|search|device)\b",
    re.I,
)
_LEAVE_RE = re.compile(
    r"\b(?:leav(?:e|es|ing)|exit(?:s|ing)?|walks?\s+out|walks?\s+away|"
    r"already\s+gone|gone\b|off[_\s-]?screen)\b",
    re.I,
)
_FALSE_LEAVE_RE = re.compile(
    r"\b(?:"
    r"(?:just\s+)?before\s+(?:leaving|exiting|departing)|"
    r"about\s+to\s+(?:leave|exit|depart)|"
    r"ready\s+to\s+(?:leave|exit|go\s+home)|"
    r"get(?:ting)?\s+off\s+work|off\s+work|leaving\s+work|"
    r"end\s+of\s+(?:the\s+)?(?:day|shift)|clock(?:ing)?\s+out|"
    r"on\s+(?:his|her|their)\s+way\s+(?:out|home)"
    r")\b",
    re.I,
)


def _beat_has_leave(beat: str) -> bool:
    cleaned = _FALSE_LEAVE_RE.sub(" ", beat or "")
    return bool(_LEAVE_RE.search(cleaned))


def classify_cast_kind(ch: dict[str, Any]) -> str:
    """Return human | brand_mascot | prop from character text only."""
    blob = " ".join(
        str(ch.get(k) or "")
        for k in ("name", "role", "description", "costume_lock", "kind")
    )
    if ch.get("is_prop") is True or str(ch.get("cast_kind") or "") in {
        "brand_mascot",
        "prop",
    }:
        kind = str(ch.get("cast_kind") or "prop")
        return "brand_mascot" if kind == "brand_mascot" else (
            "brand_mascot" if _MASCOT_RE.search(blob) or _CREATURE_PROP_RE.search(blob) else "prop"
        )
    # Logo/mascot language without a clear human role → prop/mascot.
    if (_MASCOT_RE.search(blob) or _CREATURE_PROP_RE.search(blob)) and not _HUMAN_RE.search(
        blob
    ):
        return "brand_mascot"
    if re.search(r"\b(?:logo|icon|watermark|emblem)\b", blob, re.I) and not _HUMAN_RE.search(
        blob
    ):
        return "prop"
    return "human"


def stamp_cast_kinds(characters: list[dict[str, Any]]) -> None:
    for ch in characters:
        if not isinstance(ch, dict):
            continue
        kind = classify_cast_kind(ch)
        ch["cast_kind"] = kind
        attrs = (
            dict(ch.get("identity_attrs") or {})
            if isinstance(ch.get("identity_attrs"), dict)
            else {}
        )
        attrs["cast_kind"] = kind
        if kind in {"brand_mascot", "prop"}:
            attrs["presentation"] = (
                "PROP/UI LOCK: appear only as a logo/icon ON a phone, laptop, or app UI — "
                "FORBIDDEN: free-flying creature, walking character, or replacing a human hero."
            )
            ch["is_prop"] = True
        else:
            ch["is_prop"] = False
        ch["identity_attrs"] = attrs


def _human_ids(characters: list[dict[str, Any]]) -> list[str]:
    return [
        str(c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id") and classify_cast_kind(c) == "human"
    ]


def _prop_ids(characters: list[dict[str, Any]]) -> list[str]:
    return [
        str(c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id") and classify_cast_kind(c) != "human"
    ]


_STOPWORDS = frozenset(
    """
    a an the and or but if to of in on at by for with from as is are was were be
    been being this that these those it its he she they them their his her him
    we us our you your me my i not no yes so than then too very just only also
    into over after before about above below between out up down off all any
    each few more most other some such can will shall may must should would
    could do does did done having have has had
    """.split()
)


def _score_human(ch: dict[str, Any], blob: str) -> int:
    """Score how strongly this human is named/described in the shot text."""
    text = (blob or "").lower()
    name = str(ch.get("name") or "").strip().lower()
    desc = str(ch.get("description") or "").lower()
    score = 0
    if name and re.search(rf"\b{re.escape(name)}\b", text):
        score += 8
    for term in ch.get("match_terms") or []:
        t = str(term or "").strip().lower()
        if len(t) < 2 or t in _STOPWORDS:
            continue
        # Multi-word terms OK; single tokens must not be stopwords.
        parts = [p for p in t.split() if p and p not in _STOPWORDS]
        if not parts:
            continue
        if re.search(rf"\b{re.escape(t)}\b", text):
            score += max(2, min(6, len(t)))
    for term in re.findall(r"[a-z]{3,}", name):
        if term not in _STOPWORDS and re.search(rf"\b{re.escape(term)}\b", text):
            score += 2
    # Weak cue from description tokens that also appear in the shot.
    for term in re.findall(r"[a-z]{3,}", desc)[:16]:
        if term not in _STOPWORDS and re.search(rf"\b{re.escape(term)}\b", text):
            score += 1
    return score


def _beat_text_for_scoring(action: str) -> str:
    """Strip director lock suffixes so scoring uses the storyboard shot only."""
    text = str(action or "")
    for marker in (
        "IDENTITY LOCK:",
        "PROP/UI LOCK:",
        "BLOCKING:",
        "FORBIDDEN BLEED",
        "SCREEN AXIS",
    ):
        idx = text.find(marker)
        if idx >= 0:
            text = text[:idx]
    return text.strip()


def _co_present_ids(
    action: str,
    *,
    humans: list[str],
    by_id: dict[str, dict[str, Any]],
    fallback_lead: str,
) -> list[str]:
    """Humans named in the shot, including co-presence ('with X' ⇒ subject stays)."""
    beat = _beat_text_for_scoring(action)
    mentioned = [
        c for c in humans if _score_human(by_id.get(c) or {}, beat) > 0
    ]
    if not mentioned and fallback_lead and re.search(
        r"\b(?:he|she|they)\b", beat, re.I
    ):
        # Bare subject pronoun with no name hit → continuing film lead.
        mentioned = [fallback_lead]
    if not mentioned:
        return []
    # "with <name/role>" ⇒ keep a subject if the shot has one; else film lead.
    if re.search(r"\bwith\b", beat, re.I) and len(mentioned) == 1 and fallback_lead:
        if fallback_lead in humans and fallback_lead not in mentioned:
            if re.search(r"\b(?:he|him|his|she|her|they|them|their)\b", beat, re.I):
                mentioned = [fallback_lead, mentioned[0]]
    return mentioned


def repair_shot_cast(
    analysis: dict[str, Any],
    *,
    user_prompt: str = "",
) -> list[str]:
    """Strip props from heroes; carry occupancy within setting_id only."""
    notes: list[str] = []
    out = analysis if isinstance(analysis, dict) else {}
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    stamp_cast_kinds(characters)
    humans = _human_ids(characters)
    props = _prop_ids(characters)
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    prompt_l = (user_prompt or "").lower()
    human_set = set(humans)

    # Fallback lead = first human (order from brief), never a prop.
    fallback_lead = humans[0] if humans else ""

    exited: set[str] = set()
    prev_set = ""
    prev_must: list[str] = []

    for shot in shots:
        idx = int(shot.get("shot_index") or 0) or 0
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
        beat = _beat_text_for_scoring(action)
        old = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        listed = [c for c in old if c in human_set]
        had_prop_in_cast = any(c in props for c in old)

        # Score humans against This shot (action first, then brief as weak tiebreak).
        ranked = sorted(
            humans,
            key=lambda cid: (
                _score_human(by_id.get(cid) or {}, beat),
                _score_human(by_id.get(cid) or {}, user_prompt),
            ),
            reverse=True,
        )
        mentioned = _co_present_ids(
            beat,
            humans=ranked,
            by_id=by_id,
            fallback_lead=fallback_lead,
        )
        if not mentioned:
            # Beat named nobody — keep stripped listing, else single brief lead.
            mentioned = list(listed) or (
                [fallback_lead] if fallback_lead else []
            )

        def _resolve_focus(base: list[str]) -> list[str]:
            """Prefer storyboard humans; if cast listed a prop, trust beat mentions more."""
            if had_prop_in_cast:
                # Prop-as-hero is untrusted — rebuild from who the shot actually names.
                pick = [c for c in mentioned if c not in exited] or [
                    c for c in base if c not in exited
                ]
            else:
                pick = [c for c in base if c not in exited] or [
                    c for c in mentioned if c not in exited
                ]
            if not pick and fallback_lead and fallback_lead not in exited:
                pick = [fallback_lead]
            return list(dict.fromkeys(pick))

        setting_changed = bool(prev_set) and sid != prev_set
        if not prev_set or setting_changed:
            exited = set()
            focus = _resolve_focus(listed)
            if not listed and not mentioned and fallback_lead:
                notes.append(
                    f"shot{idx}: empty human cast after prop strip → fallback {fallback_lead}"
                )
            if set(old) != set(focus):
                notes.append(f"shot{idx}: cast {old} -> {focus}")
            shot["character_ids"] = list(focus)
            shot["featured_cast_ids"] = list(focus)
            prev_must = list(focus)
            _stamp_props_and_occupancy(
                shot,
                props=props,
                by_id=by_id,
                exited=exited,
                action=beat,
                prompt_l=prompt_l,
            )
            prev_set = sid
            continue

        # Same setting: carry prior must_appear unless exited; add newly trusted cast.
        carried = [c for c in prev_must if c not in exited and c in human_set]
        incoming = (
            [c for c in mentioned if c not in exited]
            if had_prop_in_cast
            else _resolve_focus(listed)
        )
        for c in incoming:
            if c not in carried and c not in exited:
                carried.append(c)
        featured = _resolve_focus(
            [c for c in (mentioned or listed) if c in carried] or carried
        )
        if not _beat_has_leave(beat):
            shot_ids = list(dict.fromkeys(carried))
        else:
            leavers = [
                c
                for c in humans
                if c
                in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
            ]
            for lid in leavers:
                exited.add(lid)
            shot_ids = [c for c in carried if c not in exited]
        shot["character_ids"] = shot_ids or list(featured)
        shot["featured_cast_ids"] = list(featured) or list(shot["character_ids"])
        prev_must = [c for c in shot["character_ids"] if c not in exited]
        if set(old) != set(shot["character_ids"]):
            notes.append(f"shot{idx}: cast {old} -> {shot['character_ids']}")
        _stamp_props_and_occupancy(
            shot,
            props=props,
            by_id=by_id,
            exited=exited,
            action=beat,
            prompt_l=prompt_l,
        )
        prev_set = sid

    out["characters"] = characters
    out["shots"] = shots
    out["prop_ids"] = props
    return notes


def _stamp_props_and_occupancy(
    shot: dict[str, Any],
    *,
    props: list[str],
    by_id: dict[str, dict[str, Any]],
    exited: set[str],
    action: str,
    prompt_l: str,
) -> None:
    humans_here = [
        c
        for c in (shot.get("character_ids") or [])
        if str(c) and str(c) not in exited
    ]
    shot["ensemble_cast_ids"] = list(dict.fromkeys(humans_here))
    shot["compose_cast_ids"] = list(shot["ensemble_cast_ids"])

    # Props never occupy character_ids; attach UI lock when device/app is in beat
    # or when any prop was wrongly listed in the original cast (caller already stripped).
    device_beat = bool(_DEVICE_UI_RE.search(action) or _DEVICE_UI_RE.search(prompt_l))
    prop_mentions = list(props) if (props and device_beat) else []
    # Also lock props if the shot text names them (logo/mascot words).
    if props and (_MASCOT_RE.search(action) or _CREATURE_PROP_RE.search(action)):
        prop_mentions = list(props)
    shot["prop_ids"] = list(dict.fromkeys(prop_mentions))
    if shot["prop_ids"]:
        names = [str((by_id.get(p) or {}).get("name") or p) for p in shot["prop_ids"]]
        shot["prop_presentation"] = (
            f"PROP/UI LOCK: {', '.join(names)} appear ONLY as logo/icon on phone/app UI "
            "in this shot — FORBIDDEN as free-flying or walking characters."
        )
        if shot["prop_presentation"] not in str(shot.get("action") or ""):
            shot["action"] = (
                str(shot.get("action") or "") + " " + shot["prop_presentation"]
            )[:750]

    shot["occupancy"] = {
        **(shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}),
        "must_appear": list(shot["ensemble_cast_ids"]),
        "must_not_appear": sorted(exited),
        "featured": list(shot.get("featured_cast_ids") or shot["character_ids"]),
        "rule": (
            "Keep every must_appear human from the prior same-setting keyframe unless "
            "storyboard marks them exiting. Props/logos stay on-device UI only. "
            "New setting_id does not inherit prior-setting occupancy."
        ),
    }


def enforce_setting_transitions(analysis: dict[str, Any]) -> list[str]:
    """Director cross-check: setting_id change ⇒ new compose, not edit-across-set."""
    notes: list[str] = []
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    prev_set = ""
    for shot in shots:
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        idx = int(shot.get("shot_index") or 0)
        changed = bool(prev_set) and sid != prev_set
        if not prev_set or changed:
            if str(shot.get("keyframe_strategy") or "") != "compose_from_solo_refs":
                notes.append(
                    f"shot{idx}: setting {sid} first KF forced compose_from_solo_refs"
                )
            shot["keyframe_strategy"] = "compose_from_solo_refs"
            shot["same_setting_prior_edit"] = False
            shot["compose_setting_master"] = True
            shot["ensemble_master"] = False
        else:
            shot["keyframe_strategy"] = "edit_prior_keyframe"
            shot["same_setting_prior_edit"] = True
            shot["compose_setting_master"] = False
        prev_set = sid
    analysis["shots"] = shots
    return notes


def occupancy_clause_for_clip(
    shot: dict[str, Any] | None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    shot = shot if isinstance(shot, dict) else {}
    by_id = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in (characters or [])
        if isinstance(c, dict) and c.get("id")
    }
    occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
    must = [by_id.get(str(x), str(x)) for x in (occ.get("must_appear") or []) if str(x)]
    gone = [
        by_id.get(str(x), str(x)) for x in (occ.get("must_not_appear") or []) if str(x)
    ]
    props = str(shot.get("prop_presentation") or "").strip()
    bits = [
        "R2V CAST LOCK: show ONLY people bound as character1, character2, … "
        "(on-screen solos). Off-screen / already-exited people must not appear. "
        "Do not attach a peopled scene master as the last environment ref. "
        "Place people by STAGING LOCK / SEAT HOLDS / previous Wan story state. "
        "Do not restage finished exits, walk-aways, or onsets. "
        "Do not swap sex/identity; do not invent a different hero."
    ]
    if must:
        bits.append(
            f"MUST STILL BE PRESENT (unless this shot's exit list says otherwise): {', '.join(must)}."
        )
    if gone:
        bits.append(f"ALREADY EXITED (keep off-screen): {', '.join(gone)}.")
    if props:
        bits.append(props)
    bits.append(
        "Creative motion/camera OK, but FORBIDDEN: restyle, recast, remove a must_appear "
        "person who did not leave, or turn a phone logo into a free-flying mascot."
    )
    return " ".join(bits)


def ensure_brief_cast(analysis: dict[str, Any], *, user_prompt: str = "") -> list[str]:
    """Note thin placeholder cast; do not invent characters from regex heuristics."""
    notes: list[str] = []
    out = analysis if isinstance(analysis, dict) else {}
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    thin = (
        len(characters) < 2
        or all(
            str(c.get("name") or "").lower() in {"lead", "primary", "subject", ""}
            or "inferred from the user prompt"
            in str(c.get("description") or "").lower()
            for c in characters
        )
    )
    if thin and (user_prompt or "").strip():
        notes.append(
            f"brief_cast_thin: {len(characters)} placeholder character(s); "
            "LLM cast is authoritative (no heuristic rehydrate)."
        )
    return notes


def apply_cast_prop_and_setting_locks(
    analysis: dict[str, Any],
    *,
    user_prompt: str = "",
) -> dict[str, Any]:
    """Repair cast/props then enforce setting-transition compose/edit rules."""
    out = analysis if isinstance(analysis, dict) else {}
    notes = ensure_brief_cast(out, user_prompt=user_prompt)
    notes.extend(repair_shot_cast(out, user_prompt=user_prompt))
    notes.extend(enforce_setting_transitions(out))
    out.setdefault("director_contract", {})
    if isinstance(out["director_contract"], dict):
        out["director_contract"]["cast_prop_repair"] = notes[:40]
    return out
