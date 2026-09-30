# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Clip-to-clip story state: prior Wan *text*, finished events, seat anchors.

Last-frame stills are not layout. The next clip agent continues from the previous
Wan prompt; the next Wan API call gets a NEW prompt plus compact locks only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_LOCK_MARKERS = (
    "PRIOR KEYFRAME PROMPT",
    "PRIOR SHOT CONSISTENCY",
    "PREVIOUS CLIP HAD",
    "YOUR ASSIGNMENT",
    "MASTER SCENE PROMPT",
    "LANGUAGE LOCK",
    "SPEECH LOCK",
    "BGM LOCK",
    "SCENE SPECS",
    "ASPECT LOCK",
    "STYLE LOCK",
    "STYLE HOLD",
    "AUDIO ROUTE",
    "CLOTHING LOCK",
    "CLOTHING HOLD",
    "STAGING LOCK",
    "SET/ORIENTATION LOCK",
    "CONTACT / ANTI-PENETRATION",
    "ANTI-PENETRATION",
    "WAN R2V PROMPT FORMULA",
    "WAN REFERENCE MEDIA RULES",
    "[WAN REFERENCE MODE",
    "KEYFRAME CAST LOCK",
    "R2V CAST LOCK",
    "CHARACTER CONSISTENCY",
    "SAME-SCENE CONSISTENCY GATE",
    "COMPOSED SCENE MASTER",
)

_EXIT_RE = re.compile(
    r"\b(?:walk(?:s|ed|ing)?\s+away|leave(?:s|ing)?|left|exit(?:s|ed|ing)?|"
    r"depart(?:s|ed|ing)?|gone)\b",
    re.I,
)
_ONSET_RE = re.compile(
    r"\b(?:start(?:s|ed|ing)?|begin(?:s|ning)?)\s+(?:to\s+)?\w+",
    re.I,
)
# Domain-agnostic finished beats: gaze / turn / reach / check / speak onset, etc.
_BEAT_RE = re.compile(
    r"\b(?:"
    r"turn(?:s|ed|ing)?|look(?:s|ed|ing)?|gaze(?:s|d)?|glance(?:s|d)?|"
    r"face(?:s|d|ing)?|check(?:s|ed|ing)?|reach(?:es|ed|ing)?|"
    r"pick(?:s|ed|ing)?\s+up|open(?:s|ed|ing)?|grab(?:s|bed|bing)?|"
    r"point(?:s|ed|ing)?|nod(?:s|ded|ding)?|stand(?:s|ing)?\s+up|"
    r"sit(?:s|ting)?\s+down|arrive(?:s|d|ing)?|enter(?:s|ed|ing)?"
    r")\b",
    re.I,
)
_CROWD_WORD_RE = re.compile(
    r"\b(?:crowd|colleagues?|coworkers?|extras?|bystanders?|people|"
    r"office\s+floor|staff|workers|passers[- ]?by)\b",
    re.I,
)


def extract_finished_events(
    text: str,
    *,
    characters: list[dict[str, Any]] | None = None,
    on_screen: list[str] | None = None,
    shot_index: int | None = None,
) -> list[dict[str, str]]:
    """Typed already-done events from prior Wan/storyboard text (domain-agnostic).

    Only exit + motion-onset — never sit/stand or genre-specific pose rules.
    Placement continuity comes from seat_anchors / storyboard, not hardcodes.
    """
    blob = str(text or "").strip()
    if not blob:
        return []
    names = character_name_map(characters)
    present = [str(x) for x in (on_screen or []) if str(x).strip()] or list(names)
    prefix = f"shot {shot_index}: " if shot_index else ""
    events: list[dict[str, str]] = []

    def add(kind: str, note: str, cid: str = "") -> None:
        events.append(
            {
                "type": kind,
                "character_id": cid,
                "already_done": f"{prefix}{note}"[:220],
            }
        )

    matched_exit = False
    sentences = [s.strip() for s in re.split(r"[.\n;]+", blob) if s.strip()]
    for cid in present:
        who = names.get(cid, cid)
        token = str(who or "").strip()
        if not token or len(token) < 2:
            continue
        hits = [s for s in sentences if token.lower() in s.lower()]
        if not hits:
            continue
        local = " ".join(hits)
        if _EXIT_RE.search(local):
            add(
                "exit",
                f"{who} left this setting — omit from later same-setting prompts until storyboard returns them",
                cid,
            )
            matched_exit = True
        if _ONSET_RE.search(local) or _BEAT_RE.search(local):
            # Prefer a concrete clause from the prior sentence over a generic onset note.
            clause = hits[0][:160].rstrip(".")
            add(
                "beat",
                f"{who} already finished: {clause} — open this shot already past that beat",
                cid,
            )
    if _EXIT_RE.search(blob) and not matched_exit:
        add("exit", "exit already happened — omit that cast from later same-setting prompts until returned")
    if _CROWD_WORD_RE.search(blob) and _EXIT_RE.search(blob):
        add(
            "crowd_exit",
            "crowd/extras exit already happened — keep them gone or off-camera unless storyboard returns them",
        )
    if _BEAT_RE.search(blob) and not any(e["type"] == "beat" for e in events):
        # Generic beat without a matched name — still block restage.
        clause = next(
            (s[:160].rstrip(".") for s in sentences if _BEAT_RE.search(s)),
            "prior beat",
        )
        add("beat", f"already finished: {clause} — open this shot past that beat")
    elif _ONSET_RE.search(blob) and not any(e["type"] in {"onset", "beat"} for e in events):
        add("onset", "onset already happened — continue without restarting that motion")

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in events:
        key = str(item.get("already_done") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:12]


def _norm_ids(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def on_screen_ids(cfg: dict[str, Any] | None) -> list[str]:
    cfg = cfg if isinstance(cfg, dict) else {}
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    return list(
        dict.fromkeys(
            _norm_ids(cfg.get("on_screen"))
            or _norm_ids(cfg.get("character_ids"))
            or _norm_ids(occ.get("must_appear"))
        )
    )


def offscreen_ids(cfg: dict[str, Any] | None) -> list[str]:
    cfg = cfg if isinstance(cfg, dict) else {}
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    return list(
        dict.fromkeys(
            _norm_ids(cfg.get("offscreen"))
            + _norm_ids(occ.get("must_not_appear"))
            + _norm_ids(occ.get("offscreen"))
        )
    )


def character_name_map(characters: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for ch in characters or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if cid:
            out[cid] = str(ch.get("name") or cid).strip() or cid
    return out


def characters_from_graph(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    chars = analysis.get("characters") if isinstance(analysis.get("characters"), list) else []
    return [c for c in chars if isinstance(c, dict)]


def narrative_from_wan_prompt(text: str, *, limit: int = 2500) -> str:
    """Keep the motion/dialogue body; drop lock banners so the next agent can read it."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    lines: list[str] = []
    skip_block = False
    for line in raw.splitlines():
        upper = line.strip().upper()
        if any(upper.startswith(m) or m in upper for m in _LOCK_MARKERS):
            skip_block = True
            continue
        if skip_block:
            if not line.strip():
                skip_block = False
            elif re.match(r"^[A-Z][A-Z /_-]{3,}:", line.strip()):
                skip_block = any(m in line.upper() for m in _LOCK_MARKERS)
                if skip_block:
                    continue
            else:
                continue
        lines.append(line)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not cleaned:
        cleaned = re.sub(r"\s+", " ", raw)[:limit]
    return cleaned[:limit]

def seat_anchors_from_cfg(cfg: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Per-character zone / landmark / screen from this shot's blocking."""
    cfg = cfg if isinstance(cfg, dict) else {}
    out: dict[str, dict[str, str]] = {}
    blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
    landmark = str(blocking.get("landmark") or "").strip()
    positions = blocking.get("positions") if isinstance(blocking.get("positions"), list) else []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        cid = str(pos.get("character_id") or "").strip()
        if not cid:
            continue
        anchor = {
            "zone": str(pos.get("zone") or "").strip(),
            "landmark": str(pos.get("near") or pos.get("beside") or landmark or "").strip(),
            "pose": str(pos.get("pose") or "").strip(),
            "screen": str(pos.get("screen") or "").strip(),
        }
        out[cid] = {k: v for k, v in anchor.items() if v}
        if landmark and "landmark" not in out[cid]:
            out[cid]["landmark"] = landmark
    screen = cfg.get("screen_axis") or cfg.get("screen_positions")
    if isinstance(screen, dict):
        for cid, side in screen.items():
            key = str(cid).strip()
            val = str(side or "").strip()
            if not key or not val:
                continue
            out.setdefault(key, {})
            out[key].setdefault("screen", val)
    if landmark:
        for cid in on_screen_ids(cfg):
            out.setdefault(cid, {})
            out[cid].setdefault("landmark", landmark)
    return {k: v for k, v in out.items() if v}


def setting_seat_store(graph: dict[str, Any] | None) -> dict[str, dict[str, dict[str, str]]]:
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    store = meta.get("setting_seat_map") if isinstance(meta.get("setting_seat_map"), dict) else {}
    return store


def merge_setting_seats(
    graph: dict[str, Any] | None,
    *,
    setting_id: str,
    anchors: dict[str, dict[str, str]],
    still_present: list[str] | None = None,
    exited: list[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Update per-setting seat map; drop people who left this setting."""
    graph = graph if isinstance(graph, dict) else {}
    sid = str(setting_id or "").strip() or "set_1"
    meta = dict(graph.get("metadata") or {})
    store = dict(meta.get("setting_seat_map") or {}) if isinstance(meta.get("setting_seat_map"), dict) else {}
    current = dict(store.get(sid) or {}) if isinstance(store.get(sid), dict) else {}
    for cid, anchor in (anchors or {}).items():
        if isinstance(anchor, dict) and str(cid).strip():
            prev = dict(current.get(cid) or {}) if isinstance(current.get(cid), dict) else {}
            prev.update({k: str(v).strip() for k, v in anchor.items() if str(v).strip()})
            current[str(cid).strip()] = prev
    gone = {str(x).strip() for x in (exited or []) if str(x).strip()}
    if gone:
        for cid in list(current):
            if cid in gone:
                current.pop(cid, None)
    if still_present is not None:
        keep = {str(x).strip() for x in still_present if str(x).strip()}
        # Keep anchors for people who remain; do not wipe unknown ids.
        _ = keep
    store[sid] = current
    meta["setting_seat_map"] = store
    graph["metadata"] = meta
    return current


def compact_wan_story_clause(
    cfg: dict[str, Any] | None,
    *,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Positive continuity for the Wan API — seats and this window, no prior quotes."""
    from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
        positive_continuity_clause,
    )

    return positive_continuity_clause(cfg, characters=characters)


def agent_prior_story_block(cfg: dict[str, Any] | None) -> str:
    """Structured continuity for the leaf agent — never dump prior Wan prose.

    Raw previous_clip_wan_prompt stays on cfg for debug/regenerate and event
    mining only; leaf context uses storyboard-derived end_state fields.
    """
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
            agent_structured_continuity_block,
        )

        return agent_structured_continuity_block(cfg)
    except Exception:  # noqa: BLE001
        cfg = cfg if isinstance(cfg, dict) else {}
        action = str(cfg.get("previous_clip_action") or "").strip()
        if not action:
            return ""
        idx = cfg.get("previous_clip_shot_index") or ""
        return (
            f"CONTINUITY STATE (prior shot {idx} finished — structured only):\n"
            f"- Prior action finished: {action[:220]}"
        )


def ensure_prior_clip_story_on_cfg(
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None,
) -> dict[str, Any]:
    """Legacy prior-clip Wan pull — skipped when storyboard start_state is present.

    Continuity authority is storyboard start/end. This helper is a no-op for
    modern graphs so leaf agents never receive prior Wan prose as lore.
    """
    out = dict(cfg or {})
    if isinstance(out.get("start_state"), dict) and out.get("start_state"):
        out.pop("continuity_clip_node_id", None)
        return out
    # Older graphs without start_state: still avoid dumping Wan into leaf context.
    out.pop("continuity_clip_node_id", None)
    return out


def apply_story_state_to_next_cfg(
    next_cfg: dict[str, Any],
    *,
    from_cfg: dict[str, Any] | None = None,
    from_prompt: str = "",
    from_action: str = "",
    from_shot_index: int = 0,
    graph: dict[str, Any] | None = None,
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Stamp prior narrative + already_done + seat holds onto the next clip config."""
    cfg = dict(next_cfg or {})
    src = from_cfg if isinstance(from_cfg, dict) else {}
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
            _same_setting,
        )

        if src and not _same_setting(src, cfg):
            # Different room: keep plot already_done notes only — never seats/prior Wan text.
            for key in (
                "previous_clip_wan_prompt",
                "previous_clip_action",
                "previous_clip_speech",
                "seat_anchors",
            ):
                cfg.pop(key, None)
            cfg["previous_clip_handoff_ready"] = False
            return cfg
    except Exception:  # noqa: BLE001
        pass
    narrative = narrative_from_wan_prompt(from_prompt, limit=2500)
    action = str(from_action or src.get("shot_action") or "").strip()
    seed = " ".join(x for x in (action, narrative) if x)
    src_on = on_screen_ids(src)
    events = extract_finished_events(
        seed,
        characters=characters,
        on_screen=src_on,
        shot_index=from_shot_index,
    )
    done = [str(x).strip() for x in (cfg.get("already_done") or []) if str(x).strip()]
    for item in events:
        note = str(item.get("already_done") or "").strip()
        if note and note not in done:
            done.append(note)
    cfg["already_done"] = done[:16]
    cfg["previous_clip_finished_events"] = events[:12]
    # Always give the next agent positive prior story state. Prefer cleaned Wan
    # body; fall back to shot_action so already_done is never alone without prior.
    if narrative:
        cfg["previous_clip_wan_prompt"] = narrative
    elif action:
        cfg["previous_clip_wan_prompt"] = action[:2500]
    if action:
        cfg["previous_clip_action"] = action[:220]
    if from_shot_index:
        cfg["previous_clip_shot_index"] = int(from_shot_index)

    cfg = stamp_continuity_story_fields(
        cfg,
        events=events,
        prior_cfg=src,
        prior_text=narrative or action,
        prior_action=action,
    )

    exited = [str(e.get("character_id") or "").strip() for e in events if e.get("type") == "exit" and e.get("character_id")]
    # Carry prior exited_ids within the same setting.
    prior_exited = _norm_ids(src.get("exited_ids")) + _norm_ids(
        (src.get("occupancy") or {}).get("exited") if isinstance(src.get("occupancy"), dict) else []
    )
    this_on = set(on_screen_ids(cfg))
    # Storyboard return: anyone listed on_screen again is no longer exited.
    carried_exit = [cid for cid in list(dict.fromkeys(prior_exited + exited)) if cid and cid not in this_on]
    if carried_exit:
        cfg["exited_ids"] = carried_exit
        off = [c for c in offscreen_ids(cfg) if c not in this_on]
        for cid in carried_exit:
            if cid not in off:
                off.append(cid)
        cfg["offscreen"] = [c for c in off if c not in this_on]
        occ = dict(cfg.get("occupancy") or {}) if isinstance(cfg.get("occupancy"), dict) else {}
        occ["exited"] = list(carried_exit)
        must_not = _norm_ids(occ.get("must_not_appear"))
        for cid in carried_exit:
            if cid not in must_not:
                must_not.append(cid)
        occ["must_not_appear"] = must_not
        must = [c for c in _norm_ids(occ.get("must_appear")) if c not in carried_exit]
        if must:
            occ["must_appear"] = must
        cfg["occupancy"] = occ
        # Strip exited from on_screen / cast_names so prompts cannot spawn them.
        cfg["on_screen"] = [c for c in on_screen_ids(cfg) if c not in set(carried_exit)]
        if isinstance(cfg.get("cast_names"), list):
            # cast_names may be display names — drop tokens that match exited ids only.
            pass

    forced_off = list(carried_exit)
    sid = str(cfg.get("setting_id") or src.get("setting_id") or "").strip()
    src_seats = seat_anchors_from_cfg(src)
    remaining = [c for c in src_on if c not in set(forced_off)]
    merged = merge_setting_seats(
        graph,
        setting_id=sid,
        anchors=src_seats,
        still_present=remaining,
        exited=forced_off,
    )
    this_seats: dict[str, dict[str, str]] = {}
    store = merged if sid else {}
    for cid in on_screen_ids(cfg):
        if cid in store:
            this_seats[cid] = dict(store[cid])
    local = seat_anchors_from_cfg(cfg)
    for cid, anchor in local.items():
        this_seats.setdefault(cid, {}).update(anchor)
    if this_seats:
        cfg["seat_anchors"] = this_seats
    return cfg


def _hold_from_beat_note(note: str) -> str:
    """Turn a finished-beat note into a positive opening-hold sentence."""
    raw = str(note or "").strip()
    if not raw:
        return ""
    # "Name already finished: <clause> — …" → "Name is already past: <clause>."
    m = re.match(
        r"(?i)^(?:shot\s+\d+:\s*)?(?P<body>.+?)\s*(?:—|-)\s*open this shot",
        raw,
    )
    body = (m.group("body") if m else raw).strip().rstrip(".")
    body = re.sub(r"(?i)\balready finished:\s*", "already past: ", body)
    body = re.sub(r"(?i)\bmotion onset already happened.*", "already mid-action", body)
    if not body:
        return ""
    if _EXIT_RE.search(body) and "crowd" in body.lower():
        return ""
    if body.lower().startswith(("omit", "do not", "keep them")):
        return ""
    if "already" not in body.lower():
        body = "Already past: " + body
    return body[:200].rstrip(".") + "."


def pose_holds_from_events(
    events: list[dict[str, str]] | None,
    *,
    prior_action: str = "",
) -> list[str]:
    """Positive opening holds so the next clip does not restage finished beats."""
    holds: list[str] = []
    prior = str(prior_action or "").strip().lower()
    for item in events or []:
        kind = str(item.get("type") or "").strip().lower()
        if kind not in {"beat", "onset"}:
            continue
        hold = _hold_from_beat_note(str(item.get("already_done") or ""))
        if not hold or hold in holds:
            continue
        low = hold.lower()
        if "already past:" in low or (prior and prior in low):
            continue
        holds.append(hold)
    return holds[:8]


def infer_crowd_state(
    cfg: dict[str, Any] | None,
    *,
    prior_cfg: dict[str, Any] | None = None,
    prior_text: str = "",
    events: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Per-shot crowd disposition for story-form prompts (domain-agnostic)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    prior = prior_cfg if isinstance(prior_cfg, dict) else {}
    crowd = cfg.get("crowd_lock") if isinstance(cfg.get("crowd_lock"), dict) else {}
    if not crowd:
        occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
        crowd = occ.get("crowd_lock") if isinstance(occ.get("crowd_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    prior_crowd = prior.get("crowd_state") if isinstance(prior.get("crowd_state"), dict) else {}
    prior_lock = prior.get("crowd_lock") if isinstance(prior.get("crowd_lock"), dict) else {}

    disposition = str(crowd.get("disposition") or "").strip().lower()
    present = crowd.get("present")
    density = str(crowd.get("density") or prior_lock.get("density") or "").strip()
    rule = str(crowd.get("rule") or prior_lock.get("rule") or bible.get("crowd") or "").strip()

    done_notes = " ".join(str(e.get("already_done") or "") for e in (events or []))
    # Conditional lock rules constrain continuity; they do not describe past events.
    story_history = " ".join(x for x in (prior_text, done_notes) if x)

    if any(str(e.get("type") or "") == "crowd_exit" for e in (events or [])):
        disposition = "exited"
        present = False
    elif _CROWD_WORD_RE.search(story_history) and _EXIT_RE.search(story_history):
        disposition = "exited"
        present = False
    elif str(prior_crowd.get("disposition") or "").lower() == "exited":
        # Carry exited unless this shot's lock says they returned.
        if present is True or str(crowd.get("disposition") or "").lower() in {
            "in_frame",
            "background_hold",
            "returned",
        }:
            disposition = disposition or "in_frame"
        else:
            disposition = "exited"
            present = False

    if not disposition:
        if present is False or (isinstance(present, str) and present.lower() in {"false", "0", "no", "none"}):
            disposition = "empty"
        elif present is True or density or rule:
            # Explicit off-camera rule wins.
            if re.search(r"(?i)\b(?:off[- ]?camera|elsewhere|out of frame|corridor|hallway)\b", rule):
                disposition = "off_camera"
            elif re.search(r"(?i)\b(?:empty|cleared|deserted|no\s+crowd|nobody)\b", rule):
                disposition = "empty"
            elif re.search(r"(?i)\b(?:background|distant|far)\b", rule):
                disposition = "background_hold"
            else:
                disposition = "in_frame"
        elif str(bible.get("crowd") or "").strip():
            disposition = "in_frame"
        else:
            disposition = "unspecified"

    if present is None:
        present = disposition in {"in_frame", "background_hold", "off_camera"}

    holds = {
        "in_frame": "Extras remain visible in the background, same layout as before.",
        "background_hold": "Extras stay distant in the background, unchanged.",
        "off_camera": "Extras are off-camera elsewhere in the setting.",
        "exited": "The area is clear of the earlier crowd — they already left.",
        "empty": "The setting is empty of extras.",
        "unspecified": "",
    }
    hold = str(crowd.get("hold") or "").strip()
    if not hold:
        hold = holds.get(disposition, "")
    if density and disposition in {"in_frame", "background_hold"} and density.lower() not in hold.lower():
        hold = (hold.rstrip(".") + f", density {density}.").strip()

    return {
        "disposition": disposition,
        "present": bool(present),
        "density": density[:80],
        "rule": rule[:280],
        "hold": hold[:280],
    }


def stamp_continuity_story_fields(
    cfg: dict[str, Any],
    *,
    events: list[dict[str, str]] | None = None,
    prior_cfg: dict[str, Any] | None = None,
    prior_text: str = "",
    prior_action: str = "",
) -> dict[str, Any]:
    """Stamp beat_done / pose_holds / crowd_state for story-form clip prompts."""
    cfg = dict(cfg or {})
    events = list(events or cfg.get("previous_clip_finished_events") or [])
    beat_done = [str(x).strip() for x in (cfg.get("beat_done") or []) if str(x).strip()]
    for item in events:
        note = str(item.get("already_done") or "").strip()
        if note and note not in beat_done:
            beat_done.append(note)
    # Also fold plain already_done notes.
    for note in cfg.get("already_done") or []:
        text = str(note or "").strip()
        if text and text not in beat_done:
            beat_done.append(text)
    cfg["beat_done"] = beat_done[:16]
    cfg["previous_clip_finished_events"] = events[:12]
    holds = pose_holds_from_events(events, prior_action=prior_action)
    prior_holds = [
        str(x).strip()
        for x in ((prior_cfg or {}).get("pose_holds") or [])
        if str(x).strip()
    ]
    merged_holds: list[str] = []
    for h in holds + prior_holds:
        if h and h not in merged_holds:
            merged_holds.append(h)
    cfg["pose_holds"] = merged_holds[:8]
    cfg["crowd_state"] = infer_crowd_state(
        cfg,
        prior_cfg=prior_cfg,
        prior_text=prior_text or str(cfg.get("previous_clip_wan_prompt") or ""),
        events=events,
    )
    return cfg



def cap_r2v_reference_paths(paths: list[Path], *, max_refs: int = 5) -> list[Path]:
    """Keep on-screen solos + last scene specs within Wan's 5-ref cap."""
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths or []:
        if path is None:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.resolve())
    cap = max(1, int(max_refs or 5))
    if len(unique) <= cap:
        return unique
    if len(unique) >= 2:
        return [*unique[: cap - 1], unique[-1]]
    return unique[:cap]
