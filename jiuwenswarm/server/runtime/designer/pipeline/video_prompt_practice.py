# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Story-form reference-video prompts for Wan, Seedance, and MiniMax-H3.

Locks live on the node. The video call is a concise positive narrative only:
image refs, wardrobe, placement, visibility, action, speech — never forbid lists,
sit/stand examples, or scene-genre hardcodes. Exited cast is omitted until the
storyboard returns them to this setting.
"""

from __future__ import annotations

import os
import re
from typing import Any

_BAD_DIRECTIVE = re.compile(
    r"(?i)(\bforbid\b|\bforbidden\b|\bdo not\b|\bdon't\b|\bnever\b|"
    r"\banti-penetration\b|\bstyle lock\b|\bwardrobe lock\b|\bclothing lock\b|"
    r"\boccupancy\b|\bmust not\b|\bdoes not appear\b|\bstays fully offscreen\b|"
    r"\bno morphing\b|\bno teleport|\bno extras\b|\bno subtitles\b|"
    r"\bno whip\b|\bno style\b|\balready_done\b|"
    r"\bfor example\b|\be\.g\.\b|\beg\b)"
)
_WORD = re.compile(r"[a-z0-9']{4,}")
_STOP = {
    "this", "that", "with", "from", "into", "they", "them", "their", "then",
    "shot", "clip", "scene", "camera", "while", "about", "there", "where",
}


def active_video_family(model: str | None = None) -> str:
    """wan | seedance | minimax, from the configured video model."""
    text = (model or os.getenv("VIDEO_GEN_MODEL_NAME") or "").strip().lower()
    if "seedance" in text or "doubao-seedance" in text:
        return "seedance"
    if "minimax" in text or "hailuo" in text:
        return "minimax"
    return "wan"


def _image_word(index: int, family: str) -> str:
    if family == "seedance":
        return f"@Image {index}"
    return f"Image {index}"


def _analysis(graph: dict[str, Any] | None) -> dict[str, Any]:
    meta = (graph or {}).get("metadata") if isinstance((graph or {}).get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    return analysis


def _name_for_id(token: str, graph: dict[str, Any] | None) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    for char in _analysis(graph).get("characters") or []:
        if not isinstance(char, dict):
            continue
        if str(char.get("id") or "") == raw or str(char.get("name") or "") == raw:
            return str(char.get("name") or raw).strip()
    if raw.startswith("char_"):
        return raw[5:].replace("_", " ").strip().title()
    return raw


def on_screen_names(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    names = [str(x).strip() for x in (cfg.get("cast_names") or []) if str(x).strip()]
    if names:
        return names
    ids = [
        str(x).strip()
        for x in (cfg.get("on_screen") or cfg.get("character_ids") or [])
        if str(x).strip()
    ]
    out: list[str] = []
    for token in ids:
        label = _name_for_id(token, graph)
        if label and label not in out:
            out.append(label)
    return out


def _scene_name(cfg: dict[str, Any]) -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    place = str(bible.get("scene_name") or bible.get("place") or "").strip()
    if place:
        place = place.split(".")[0].strip()[:80]
        if len(place.split()) <= 6:
            return place
    setting = str(cfg.get("setting_id") or "").strip()
    if setting and not setting.lower().startswith("set_"):
        return setting
    return "the empty room"


def _style_phrase(cfg: dict[str, Any]) -> str:
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or style.get("medium") or "").strip()
    look = re.split(r"\s+[—–-]\s+|\bnever\b|\bno style\b", look, maxsplit=1, flags=re.I)[0]
    look = look.strip(" .;")
    return look


def _doing_line(cfg: dict[str, Any], graph: dict[str, Any] | None, action: str) -> str:
    actions = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}
    raw_lines = [
        str(value or "").strip().rstrip(".")
        for value in actions.values()
        if str(value or "").strip() and not _BAD_DIRECTIVE.search(str(value))
    ]
    unique = []
    for line in raw_lines:
        if line.casefold() not in {item.casefold() for item in unique}:
            unique.append(line)
    bits: list[str] = []
    if len(unique) <= 1:
        bits = []
    else:
        for key, value in actions.items():
            line = str(value or "").strip().rstrip(".")
            if not line or _BAD_DIRECTIVE.search(line):
                continue
            who = _name_for_id(str(key), graph)
            if who and who.lower() not in line.lower():
                bits.append(f"{who} {line[:1].lower() + line[1:] if line[:1].isupper() else line}")
            else:
                bits.append(line)
    if len(bits) >= 2:
        return f"{bits[0]}, while {bits[1]}."
    if len(bits) == 1:
        text = bits[0]
        return text if text.endswith(".") else text + "."
    beat = str(action or cfg.get("shot_action") or cfg.get("character_action") or "").strip()
    if not beat or _BAD_DIRECTIVE.search(beat):
        return ""
    return beat if beat.endswith(".") else beat + "."


def _speech_lines(cfg: dict[str, Any], graph: dict[str, Any] | None, names: list[str]) -> list[str]:
    spoken: list[str] = []
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    on_screen = {name.casefold() for name in names}
    if by_char:
        for key, value in by_char.items():
            line = str(value or "").strip().strip('"')
            if not line:
                continue
            who = _name_for_id(str(key), graph) or str(key)
            if who.casefold() in on_screen:
                spoken.append(f'{who} says: "{line}".')
            else:
                spoken.append(f'{who}, heard offscreen, says: "{line}".')
        return spoken
    line = str(cfg.get("speech_line") or "").strip().strip('"')
    if not line:
        return []
    if names:
        return [f'{names[0]} says: "{line}".']
    return [f'A voice says: "{line}".']


def _music_line(cfg: dict[str, Any]) -> str:
    """One positive score phrase. Lock rules and generic defaults stay off the call."""
    bgm = cfg.get("bgm_lock") if isinstance(cfg.get("bgm_lock"), dict) else {}
    generic = {"cinematic", "soft underscore", "tasteful non-vocal", "same bed across clips"}
    bits: list[str] = []
    for key in ("instruments", "style", "mood"):
        value = str(bgm.get(key) or "").strip().rstrip(".")
        if not value or value.casefold() in generic or _BAD_DIRECTIVE.search(value):
            continue
        bits.append(value)
    if not bits:
        return ""
    return "Audio: " + ", ".join(bits[:3]) + "."


def exited_ids(cfg: dict[str, Any] | None) -> list[str]:
    """Cast that left this setting and stays gone until storyboard returns them.

    Does NOT include ordinary offscreen (still in-setting) — only exit marks.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    return list(
        dict.fromkeys(
            [
                str(x).strip()
                for x in (
                    list(cfg.get("exited_ids") or [])
                    + list(cfg.get("exiting_character_ids") or [])
                    + list(cfg.get("exiting") or [])
                    + list(occ.get("exited") or [])
                )
                if str(x).strip()
            ]
        )
    )


def exited_names(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    on = {n.casefold() for n in on_screen_names(cfg, graph)}
    out: list[str] = []
    for token in exited_ids(cfg):
        label = _name_for_id(token, graph)
        if not label or label.casefold() in on or label.casefold() in {x.casefold() for x in out}:
            continue
        # Storyboard return: listed on_screen wins; otherwise treat as gone.
        out.append(label)
    return out


def offscreen_names(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    """Still in this setting but not in this camera frame — not exited/gone."""
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    gone = {x.casefold() for x in exited_ids(cfg)}
    gone_names = {n.casefold() for n in exited_names(cfg, graph)}
    ids = [
        str(x).strip()
        for x in (list(cfg.get("offscreen") or []) + list(occ.get("offscreen") or []))
        if str(x).strip()
    ]
    on = {name.casefold() for name in on_screen_names(cfg, graph)}
    out: list[str] = []
    for token in ids:
        if token.casefold() in gone or token in exited_ids(cfg):
            continue
        label = _name_for_id(token, graph)
        if (
            not label
            or label.casefold() in on
            or label.casefold() in gone
            or label.casefold() in gone_names
            or label.casefold() in {x.casefold() for x in out}
        ):
            continue
        out.append(label)
    return out


def _language_display(code: str) -> str:
    """Map common BCP-47 / short codes to a spoken language name (domain-agnostic)."""
    raw = str(code or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", "-")
    known = {
        "en": "English",
        "en-us": "English",
        "en-gb": "English",
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "zh-tw": "Chinese",
        "ja": "Japanese",
        "ko": "Korean",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ar": "Arabic",
        "hi": "Hindi",
        "pt": "Portuguese",
        "ru": "Russian",
        "it": "Italian",
    }
    if key in known:
        return known[key]
    # Already a language name (e.g. "English") — keep as-is.
    if re.fullmatch(r"[A-Za-z][A-Za-z\s-]{1,40}", raw):
        return raw[0].upper() + raw[1:]
    return raw


def _has_spoken_dialogue(cfg: dict[str, Any]) -> bool:
    if str(cfg.get("speech_line") or "").strip():
        return True
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    return any(str(v or "").strip() for v in by_char.values())


def _language_sentence(cfg: dict[str, Any]) -> str:
    """Positive dialogue-language cue — only when this clip has speech."""
    if not _has_spoken_dialogue(cfg):
        return ""
    lang = str(cfg.get("language_lock") or "").strip()
    if not lang:
        return ""
    name = _language_display(lang)
    if not name or _BAD_DIRECTIVE.search(name):
        return ""
    return f"Spoken dialogue is in {name}."


def _time_of_day_payload(cfg: dict[str, Any]) -> dict[str, str]:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = _scrub_lock_phrase(
        str((tod or {}).get("lighting") or bible.get("lighting") or "")
    )
    return {"time_of_day": label, "lighting": lighting}


def _time_of_day_sentence(cfg: dict[str, Any]) -> str:
    """Positive time-of-day / lighting continuity — no LOCK banners."""
    payload = _time_of_day_payload(cfg)
    label = payload.get("time_of_day") or ""
    lighting = payload.get("lighting") or ""
    if label and label.lower() != "unspecified":
        if lighting:
            if label.lower() in lighting.lower():
                return lighting.rstrip(".") + "."
            return f"It is {label}; {lighting.rstrip('.')}."
        return f"It is {label}."
    if lighting:
        return lighting.rstrip(".") + "."
    return ""


def _tod_covered_in_text(text: str, cfg: dict[str, Any]) -> bool:
    pl = str(text or "").lower()
    payload = _time_of_day_payload(cfg)
    label = (payload.get("time_of_day") or "").lower()
    if label and label != "unspecified" and label in pl:
        return True
    lighting = (payload.get("lighting") or "").lower()
    for word in (
        "night",
        "dawn",
        "dusk",
        "morning",
        "evening",
        "daylight",
        "afternoon",
        "midnight",
        "sunrise",
        "sunset",
        "twilight",
        "noon",
    ):
        if word in label or word in lighting:
            if word in pl:
                return True
    # Generic lighting already present as scene description.
    if lighting and any(tok in pl for tok in lighting.split()[:4] if len(tok) > 3):
        return True
    return False


def ensure_story_lock_coverage(
    prompt: str,
    cfg: dict[str, Any] | None,
    *,
    graph: dict[str, Any] | None = None,
    max_chars: int = 2200,
) -> tuple[str, list[str]]:
    """Append missing film-lock *content* as positive story sentences (clip-safe).

    Does not paste LOCK essays — those get scrubbed by story-form practice.
    Aspect / full cast bible stay on config + stills, not in Wan prose.
    """
    del graph  # reserved for future cast-aware checks
    cfg = cfg if isinstance(cfg, dict) else {}
    text = str(prompt or "").strip()
    pl = text.lower()
    notes: list[str] = []
    additions: list[str] = []

    lang_sent = _language_sentence(cfg)
    if lang_sent:
        lang = str(cfg.get("language_lock") or "").strip()
        display = _language_display(lang).lower()
        mentioned = "spoken dialogue" in pl or (
            display and re.search(rf"(?i)\b{re.escape(display)}\b", pl)
        )
        # Short codes (en/zh) must not match as substrings of other words.
        if not mentioned and lang and len(lang) > 3:
            mentioned = lang.lower() in pl
        if not mentioned and lang and len(lang) <= 3:
            mentioned = bool(
                re.search(rf"(?i)\b(?:in\s+)?{re.escape(lang)}\b", pl)
            )
        if not mentioned:
            additions.append(lang_sent)
            notes.append("story_cover_language")

    tod_sent = _time_of_day_sentence(cfg)
    if tod_sent and not _tod_covered_in_text(text, cfg):
        additions.append(tod_sent)
        notes.append("story_cover_time_of_day")

    look = _style_phrase(cfg)
    if look and not _BAD_DIRECTIVE.search(look):
        look_l = look.lower()
        # Soft style already at end of compose — only fill if wholly missing.
        if look_l not in pl and not any(
            token in pl for token in look_l.replace("-", " ").split() if len(token) > 4
        ):
            additions.append(f"{look}.")
            notes.append("story_cover_style")

    # Crowd disposition as positive prose (never CROWD LOCK banners).
    crowd = _ensure_crowd_state(cfg)
    hold = _scrub_lock_phrase(str(crowd.get("hold") or ""))
    if hold and hold.lower() not in pl and not _BAD_DIRECTIVE.search(hold):
        # Avoid restating if disposition words already present.
        disp = str(crowd.get("disposition") or "").lower()
        covered = False
        if disp == "exited" and any(w in pl for w in ("already left", "clear of", "empty of extras", "crowd gone")):
            covered = True
        if disp == "empty" and "empty" in pl:
            covered = True
        if disp in {"in_frame", "background_hold"} and any(
            w in pl for w in ("extras", "crowd", "colleagues", "background")
        ):
            covered = True
        if disp == "off_camera" and ("off-camera" in pl or "off camera" in pl):
            covered = True
        if not covered:
            additions.append(hold if hold.endswith(".") else hold + ".")
            notes.append("story_cover_crowd_state")

    # Opening holds when prior shots exist but prose omitted them.
    for hold_line in (cfg.get("pose_holds") or [])[:3]:
        text_h = str(hold_line or "").strip()
        if not text_h or _BAD_DIRECTIVE.search(text_h):
            continue
        key = text_h.lower()[:40]
        if key and key not in pl:
            additions.append(text_h if text_h.endswith(".") else text_h + ".")
            notes.append("story_cover_pose_hold")
            break

    if not additions:
        return text[:max_chars], notes
    merged = (text.rstrip() + " " + " ".join(additions)).strip()
    return merged[:max_chars], notes


def _scrub_lock_phrase(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(
        r"(?i)\b(?:positioning(?:\s+lock)?|staging(?:\s+lock)?|seat\s+holds?|"
        r"continuity\s+state|people in frame now)\s*:\s*",
        "",
        value,
    ).strip()
    value = re.split(
        r"(?i)\b(?:do not|don't|never|forbid|forbidden|must not)\b",
        value,
        maxsplit=1,
    )[0].strip(" ,;.—-")
    if not value or _BAD_DIRECTIVE.search(value):
        return ""
    return value[:280]


def _position_lines(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    """Where on-screen people stay — from seat anchors / positioning lock."""
    lines: list[str] = []
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    for cid, anchor in seats.items():
        if not isinstance(anchor, dict):
            continue
        who = _name_for_id(str(cid), graph) or str(cid)
        bits = [who, "stays"]
        if anchor.get("pose"):
            bits.append(str(anchor["pose"]))
        elif anchor.get("zone"):
            bits.append(str(anchor["zone"]))
        if anchor.get("landmark"):
            bits.append(f"near {anchor['landmark']}")
        if anchor.get("screen"):
            bits.append(str(anchor["screen"]))
        phrase = _scrub_lock_phrase(" ".join(bits))
        if phrase:
            lines.append(phrase if phrase.endswith(".") else phrase + ".")
    if lines:
        return lines[:6]
    positioning = _scrub_lock_phrase(str(cfg.get("positioning_lock") or ""))
    if positioning:
        return [f"Positions: {positioning}."]
    blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
    positions = blocking.get("positions") if isinstance(blocking.get("positions"), list) else []
    bits: list[str] = []
    for pos in positions[:6]:
        if not isinstance(pos, dict):
            continue
        who = str(pos.get("name") or _name_for_id(str(pos.get("character_id") or ""), graph) or "").strip()
        if not who:
            continue
        zone = str(pos.get("zone") or "").strip()
        facing = str(pos.get("facing") or "").strip()
        pose = str(pos.get("pose") or "").strip()
        near = str(pos.get("near") or pos.get("beside") or "").strip()
        chunk = who
        if zone:
            chunk += f" {zone}"
        if pose:
            chunk += f" {pose}"
        if near:
            chunk += f" near {near}"
        if facing:
            chunk += f", facing {facing}"
        phrase = _scrub_lock_phrase(chunk)
        if phrase:
            bits.append(phrase)
    if bits:
        return ["Positions: " + "; ".join(bits[:4]) + "."]
    return []


def _cast_presence_lines(cfg: dict[str, Any], graph: dict[str, Any] | None, names: list[str]) -> list[str]:
    """Who is filmed now vs who stays out of frame — positive wording only."""
    lines: list[str] = []
    if names:
        if len(names) == 1:
            lines.append(f"On screen: {names[0]}.")
        else:
            lines.append(f"On screen: {', '.join(names[:-1])} and {names[-1]}.")
    away = offscreen_names(cfg, graph)
    speakers = {
        _name_for_id(str(key), graph).casefold()
        for key in (
            (cfg.get("speech_by_character") or {})
            if isinstance(cfg.get("speech_by_character"), dict)
            else {}
        )
        if str(key).strip()
    }
    heard = [name for name in away if name.casefold() in speakers]
    silent = [name for name in away if name.casefold() not in speakers]
    if heard:
        if len(heard) == 1:
            lines.append(f"{heard[0]} is heard offscreen.")
        else:
            lines.append(f"{', '.join(heard[:-1])} and {heard[-1]} are heard offscreen.")
    if silent:
        if len(silent) == 1:
            lines.append(f"{silent[0]} is away from the frame.")
        else:
            lines.append(f"{', '.join(silent[:-1])} and {silent[-1]} are away from the frame.")
    return lines


def _scene_bible_lines(cfg: dict[str, Any]) -> list[str]:
    """Positive room details from the scene specs — no lock banner."""
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    if not bible:
        return []
    lines: list[str] = []
    lighting = _scrub_lock_phrase(str(bible.get("lighting") or ""))
    if lighting:
        lines.append(f"Lighting: {lighting}.")
    objects = bible.get("objects") if isinstance(bible.get("objects"), list) else []
    props = [
        _scrub_lock_phrase(str(x))
        for x in objects[:5]
        if str(x).strip() and not _BAD_DIRECTIVE.search(str(x))
    ]
    props = [p for p in props if p]
    if props:
        lines.append("Key props: " + ", ".join(props) + ".")
    crowd = _scrub_lock_phrase(str(bible.get("crowd") or ""))
    if crowd and crowd.casefold() not in {"empty", "none", "no crowd"}:
        lines.append(f"Crowd: {crowd}.")
    return lines


def _continue_line(cfg: dict[str, Any]) -> str:
    """Positive continue from THIS row's start_state — not prior Wan text."""
    start = cfg.get("start_state") if isinstance(cfg.get("start_state"), dict) else {}
    if start.get("pose") or start.get("facing") or start.get("seats"):
        return ""  # start_state lines cover opening; no generic prior-clip sentence
    if cfg.get("pose_holds") or cfg.get("beat_done") or cfg.get("already_done"):
        return (
            "This shot opens already past earlier finished beats in the storyboard — "
            "same setting and faces continue from that authored start_state."
        )
    return ""


def _ensure_crowd_state(cfg: dict[str, Any]) -> dict[str, Any]:
    state = cfg.get("crowd_state") if isinstance(cfg.get("crowd_state"), dict) else {}
    if state.get("disposition") or state.get("hold"):
        return state
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
            infer_crowd_state,
        )

        state = infer_crowd_state(
            cfg,
            prior_text=str(cfg.get("previous_clip_wan_prompt") or ""),
            events=list(cfg.get("previous_clip_finished_events") or []),
        )
        cfg["crowd_state"] = state
    except Exception:  # noqa: BLE001
        state = {}
    return state if isinstance(state, dict) else {}


def _continuity_story_lines(cfg: dict[str, Any]) -> list[str]:
    """Opening holds from storyboard start_state + crowd + continue cue."""
    lines: list[str] = []
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
            start_end_story_lines,
        )

        for line in start_end_story_lines(cfg):
            text = line if line.endswith(".") else line + "."
            if text and not _BAD_DIRECTIVE.search(text):
                lines.append(text)
    except Exception:  # noqa: BLE001
        pass
    holds = [str(x).strip() for x in (cfg.get("pose_holds") or []) if str(x).strip()]
    if not holds and not lines:
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
                pose_holds_from_events,
            )

            events = list(cfg.get("previous_clip_finished_events") or [])
            if not events and cfg.get("beat_done"):
                events = [
                    {"type": "beat", "already_done": str(x)}
                    for x in (cfg.get("beat_done") or [])
                    if str(x).strip()
                ]
            holds = pose_holds_from_events(
                events,
                prior_action=str(cfg.get("previous_clip_action") or ""),
            )
            if holds:
                cfg["pose_holds"] = holds
        except Exception:  # noqa: BLE001
            holds = []
    prior_action = str(cfg.get("previous_clip_action") or "")
    this_action = str(cfg.get("shot_action") or cfg.get("action") or "")
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
            _is_action_restatement,
        )
    except Exception:  # noqa: BLE001
        _is_action_restatement = None  # type: ignore[assignment]
    for hold in holds[:4]:
        text = hold if hold.endswith(".") else hold + "."
        if _is_action_restatement and _is_action_restatement(text, prior_action, this_action):
            continue
        if text and not _BAD_DIRECTIVE.search(text) and text not in lines:
            lines.append(text)

    crowd = _ensure_crowd_state(cfg)
    hold = _scrub_lock_phrase(str(crowd.get("hold") or ""))
    if hold and not _BAD_DIRECTIVE.search(hold):
        lines.append(hold if hold.endswith(".") else hold + ".")

    cont = _continue_line(cfg)
    if cont:
        lines.append(cont)
    return lines


def _wardrobe_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None) -> str:
    """Short wearable phrase for one cast member — from costume_lock / analysis."""
    name = _name_for_id(token, graph) or str(token or "").strip()
    if not name:
        return ""
    # Per-id map if present.
    by_id = cfg.get("costume_by_id") if isinstance(cfg.get("costume_by_id"), dict) else {}
    raw = str(by_id.get(token) or by_id.get(name) or "").strip()
    if not raw:
        lock = str(cfg.get("costume_lock") or "").strip()
        # "Mother: dusty rose…; Young Child: pale yellow…"
        for chunk in re.split(r"[;\n]+", lock):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                who, rest = chunk.split(":", 1)
                if who.strip().casefold() == name.casefold() or who.strip() == token:
                    raw = rest.strip()
                    break
            elif name.casefold() in chunk.casefold():
                raw = chunk
                break
        if not raw and lock and len(on_screen_names(cfg, graph)) <= 1:
            raw = lock
    if not raw:
        for char in _analysis(graph).get("characters") or []:
            if not isinstance(char, dict):
                continue
            if str(char.get("name") or "") == name or str(char.get("id") or "") == token:
                raw = str(char.get("costume_lock") or char.get("description") or "").strip()
                break
    raw = _scrub_lock_phrase(raw)
    if not raw:
        return ""
    # Drop "Name:" prefix and slot= noise into a readable wear phrase.
    raw = re.sub(rf"(?i)^{re.escape(name)}\s*:\s*", "", raw).strip()
    raw = re.sub(r"\b(top|bottom|outfit|footwear|outerwear|accessories)\s*=\s*", "", raw)
    raw = re.sub(r"\s*;\s*", ", ", raw)
    return raw[:160]


def _seat_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None) -> str:
    name = _name_for_id(token, graph) or str(token or "").strip()
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    anchor = seats.get(token) if isinstance(seats.get(token), dict) else None
    if anchor is None:
        for key, value in seats.items():
            if _name_for_id(str(key), graph).casefold() == name.casefold() and isinstance(value, dict):
                anchor = value
                break
    if isinstance(anchor, dict):
        bits: list[str] = []
        pose = str(anchor.get("pose") or "").strip()
        zone = str(anchor.get("zone") or "").strip()
        landmark = str(anchor.get("landmark") or "").strip()
        screen = str(anchor.get("screen") or "").strip()
        if pose:
            bits.append(pose)
        elif zone:
            bits.append(zone)
        if landmark:
            bits.append(f"near the {landmark}" if not landmark.lower().startswith("the ") else f"near {landmark}")
        if screen:
            bits.append(screen)
        phrase = _scrub_lock_phrase(" ".join(bits))
        if phrase:
            return phrase
    positioning = _scrub_lock_phrase(str(cfg.get("positioning_lock") or ""))
    if positioning and name and name.casefold() in positioning.casefold():
        return positioning[:120]
    return ""


def _action_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None, fallback: str) -> str:
    actions = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}
    name = _name_for_id(token, graph) or str(token or "").strip()

    def _clean_doing(line: str) -> str:
        text = str(line or "").strip().rstrip(".")
        if not text or _BAD_DIRECTIVE.search(text):
            return ""
        # Drop leading "Name …" / "The Name is …" so story sentence can say "is <verb…>".
        if name:
            text = re.sub(
                rf"(?i)^(?:the\s+)?{re.escape(name)}\s+(?:is\s+|are\s+)?",
                "",
                text,
            ).strip()
        text = re.sub(r"(?i)^(is|are|was|were)\s+", "", text).strip()
        return text[:180]

    for key, value in actions.items():
        if str(key) == token or _name_for_id(str(key), graph).casefold() == name.casefold():
            cleaned = _clean_doing(str(value or ""))
            if cleaned:
                return cleaned
    unique = [
        _clean_doing(str(v))
        for v in actions.values()
        if str(v or "").strip() and not _BAD_DIRECTIVE.search(str(v))
    ]
    unique = [u for u in unique if u]
    if len({u.casefold() for u in unique}) == 1:
        return unique[0]
    # Prefer this character's clause inside a shared "A … while B …" beat.
    beat = str(fallback or cfg.get("shot_action") or "").strip().rstrip(".")
    if beat and name and " while " in beat.lower():
        for part in re.split(r"(?i)\s+while\s+", beat):
            if name.casefold() in part.casefold():
                cleaned = _clean_doing(part)
                if cleaned:
                    return cleaned
    return _clean_doing(beat)


def _partial_names(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    ids = [
        str(x).strip()
        for x in (list(occ.get("partial") or []) + list(occ.get("background") or []))
        if str(x).strip()
    ]
    on = {n.casefold() for n in on_screen_names(cfg, graph)}
    out: list[str] = []
    for token in ids:
        label = _name_for_id(token, graph)
        if label and label.casefold() not in on and label.casefold() not in {x.casefold() for x in out}:
            out.append(label)
    return out


def _on_screen_tokens(cfg: dict[str, Any]) -> list[str]:
    names = [str(x).strip() for x in (cfg.get("cast_names") or []) if str(x).strip()]
    if names:
        return names
    return [
        str(x).strip()
        for x in (cfg.get("on_screen") or cfg.get("character_ids") or [])
        if str(x).strip()
    ]


def _story_cast_sentence(
    *,
    who: str,
    image_word: str,
    wardrobe: str,
    seat: str,
    doing: str,
    scene_word: str,
    scene: str,
    visibility: str = "full",
) -> str:
    """One cast member in positive story form (no negatives, no genre examples)."""
    bits = [f"{who} from {image_word}"]
    if wardrobe:
        bits.append(f"wearing {wardrobe}")
    if visibility == "full":
        if seat:
            bits.append(f"{seat} in the scene from {scene_word}")
        else:
            bits.append(f"in the scene from {scene_word} ({scene})")
        if doing:
            bits.append(f"is {doing[:1].lower() + doing[1:] if doing[:1].isupper() else doing}")
    elif visibility == "partial":
        bits.append(f"is in the scene from {scene_word}, partially visible")
        if doing:
            bits.append(f"{doing[:1].lower() + doing[1:] if doing[:1].isupper() else doing}")
    else:
        # In-setting but out of this frame (not exited). Positive camera framing only.
        bits.append(f"is in the scene from {scene_word}, outside this camera frame")
        if doing:
            bits.append(f"({doing})")
    text = ", ".join(bits)
    return text if text.endswith(".") else text + "."


def compose_practice_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    action: str = "",
    camera: str = "",
    model: str | None = None,
    extra_image_labels: list[str] | None = None,
) -> str:
    """Story-form video prompt: only present cast; omit exited until they return."""
    cfg = cfg if isinstance(cfg, dict) else {}
    family = active_video_family(model)
    gone = {x.casefold() for x in exited_ids(cfg)}
    gone_names = {n.casefold() for n in exited_names(cfg, graph)}
    tokens = [
        t
        for t in _on_screen_tokens(cfg)
        if t.casefold() not in gone and _name_for_id(t, graph).casefold() not in gone_names
    ]
    names = [n for n in on_screen_names(cfg, graph) if n.casefold() not in gone_names]
    place = _scene_name(cfg)
    extra_labels = [str(item).strip() for item in (extra_image_labels or []) if str(item).strip()]
    scene_index = max(1, len(tokens) + len(extra_labels) + 1)
    scene_word = _image_word(scene_index, family)
    sentences: list[str] = []

    sentences.append(f"The scene is as in {scene_word}: {place}.")
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    objects = bible.get("objects") if isinstance(bible.get("objects"), list) else []
    props = [
        _scrub_lock_phrase(str(x))
        for x in objects[:4]
        if str(x).strip() and not _BAD_DIRECTIVE.search(str(x))
    ]
    props = [p for p in props if p]
    tod_sent = _time_of_day_sentence(cfg)
    if tod_sent:
        sentences.append(tod_sent)
    else:
        lighting = _scrub_lock_phrase(str(bible.get("lighting") or ""))
        desc_bits = [b for b in (lighting, ", ".join(props) if props else "") if b]
        if desc_bits:
            sentences.append("Scene description: " + "; ".join(desc_bits) + ".")
    if props and tod_sent:
        sentences.append("Props in view: " + ", ".join(props) + ".")

    for line in _continuity_story_lines(cfg):
        if line and line not in sentences:
            sentences.append(line)

    for index, token in enumerate(tokens, start=1):
        who = _name_for_id(token, graph) or str(token)
        if who.casefold() in gone_names:
            continue
        sentences.append(
            _story_cast_sentence(
                who=who,
                image_word=_image_word(index, family),
                wardrobe=_wardrobe_for(token, cfg, graph),
                seat=_seat_for(token, cfg, graph),
                doing=_action_for(token, cfg, graph, action),
                scene_word=scene_word,
                scene=place,
                visibility="full",
            )
        )

    for offset, label in enumerate(extra_labels, start=len(tokens) + 1):
        word = _image_word(offset, family)
        sentences.append(
            f"{word} is the connected still {label}, and that subject is visible in this shot."
        )

    for who in _partial_names(cfg, graph):
        if who.casefold() in gone_names:
            continue
        sentences.append(
            _story_cast_sentence(
                who=who,
                image_word="the cast references",
                wardrobe=_wardrobe_for(who, cfg, graph),
                seat=_seat_for(who, cfg, graph),
                doing=_action_for(who, cfg, graph, ""),
                scene_word=scene_word,
                scene=place,
                visibility="partial",
            )
        )

    # Still-in-setting offscreen: only if they speak this shot (heard). Never name exited.
    speakers = {
        _name_for_id(str(key), graph).casefold()
        for key in (
            (cfg.get("speech_by_character") or {})
            if isinstance(cfg.get("speech_by_character"), dict)
            else {}
        )
        if str(key).strip()
    }
    for who in offscreen_names(cfg, graph):
        if who.casefold() in gone_names or who.casefold() in {n.casefold() for n in names}:
            continue
        if who.casefold() not in speakers:
            # Omitted from the call — occupancy lock stays on the node.
            continue
        wardrobe = _wardrobe_for(who, cfg, graph)
        line = f"{who}"
        if wardrobe:
            line += f", wearing {wardrobe},"
        line += f" is in the scene from {scene_word}, outside this camera frame, and is heard."
        sentences.append(line)

    cam = str(camera or cfg.get("camera") or "").strip().rstrip(".")
    if cam and not _BAD_DIRECTIVE.search(cam):
        if cam.lower().startswith("the camera"):
            sentences.append(cam + ".")
        else:
            sentences.append(
                f"The camera {cam[0].lower() + cam[1:] if cam[:1].isupper() else cam}."
            )

    if not any(_action_for(t, cfg, graph, "") for t in tokens):
        doing = _doing_line(cfg, graph, action)
        if doing:
            sentences.append(doing)

    sentences.extend(_speech_lines(cfg, graph, names))
    lang_sent = _language_sentence(cfg)
    if lang_sent:
        sentences.append(lang_sent)
    music = _music_line(cfg)
    if music:
        sentences.append(music)
    look = _style_phrase(cfg)
    if look and not _BAD_DIRECTIVE.search(look):
        sentences.append(f"{look}.")

    text = " ".join(s for s in sentences if s and not _BAD_DIRECTIVE.search(s))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\.", ".", text)
    # Final safety: strip any exited names that leaked via speech/continue text.
    for gone_name in exited_names(cfg, graph):
        if gone_name and gone_name.casefold() not in {n.casefold() for n in names}:
            text = re.sub(rf"(?i)\b{re.escape(gone_name)}\b[^.]*\.?", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text, _cov_notes = ensure_story_lock_coverage(text, cfg, graph=graph)
    return text[:2200]


def _covers_beat(prompt: str, action: str) -> bool:
    words = [w for w in _WORD.findall(action.lower()) if w not in _STOP]
    if len(words) < 3:
        return True
    hay = prompt.lower()
    hit = sum(1 for word in words if word in hay)
    return hit >= max(2, int(len(words) * 0.4))


def _story_leads_prompt(prompt: str, action: str) -> bool:
    """Storyboard shot must appear in the narrative body, not only in a trailer line."""
    text = str(prompt or "").strip()
    beat = str(action or "").strip()
    if not beat or len(beat.split()) < 3:
        return True
    return _covers_beat(text, beat)


def prompt_respects_practice(
    prompt: str,
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
) -> list[str]:
    """Reasons the video call should be rewritten. Empty means it can be sent."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if str(cfg.get("reference_call_mode") or "").strip():
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            reference_prompt_issues,
        )

        return reference_prompt_issues(str(prompt or ""), cfg)
    text = str(prompt or "").strip()
    reasons: list[str] = []
    if not text:
        return ["empty"]
    if len(text) > 2200:
        reasons.append("too_long")
    if _BAD_DIRECTIVE.search(text):
        reasons.append("negative_or_example_or_lock_essay")
    if not re.search(r"(?i)(?:from\s+(?:@)?image\s*\d+|(?:@)?image\s*\d+\s+is\b)", text):
        reasons.append("missing_image_binding")
    if not re.search(r"(?i)scene is as in\s+(?:@)?image\s*\d+|in the scene from\s+(?:@)?image", text):
        reasons.append("missing_in_scene")
    for name in on_screen_names(cfg, graph):
        if name.casefold() in {n.casefold() for n in exited_names(cfg, graph)}:
            continue
        if name.casefold() not in text.casefold():
            reasons.append(f"missing_on_screen:{name}")
            break
    if on_screen_names(cfg, graph) and not re.search(r"(?i)\bwearing\b|\bfrom image\b", text):
        reasons.append("missing_cast_presence")
    if str(cfg.get("costume_lock") or "").strip() and not re.search(r"(?i)\bwearing\b", text):
        reasons.append("missing_wardrobe_lock")
    # Exited cast must not reappear in the call unless storyboard put them on_screen.
    on = {n.casefold() for n in on_screen_names(cfg, graph)}
    for name in exited_names(cfg, graph):
        if name.casefold() in on:
            continue
        if name.casefold() in text.casefold():
            reasons.append(f"exited_cast_leaked:{name}")
            break
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    if seats and not re.search(
        r"(?i)\b(?:stays|placement|screen-?left|screen-?right|near|beside|zone|facing|"
        r"standing|seated|sitting|leaning|kneeling)\b",
        text,
    ):
        reasons.append("missing_placement_handoff")
    elif str(cfg.get("positioning_lock") or "").strip() and not re.search(
        r"(?i)\b(?:stays|placement|facing|zone|screen-?left|screen-?right|near|beside)\b",
        text,
    ):
        reasons.append("missing_position_handoff")
    if str(cfg.get("previous_clip_wan_prompt") or cfg.get("previous_clip_action") or "").strip():
        if not re.search(r"(?i)\b(?:continue|continues|same setting|same placement|same room)\b", text):
            reasons.append("missing_prior_continue")
    action = str(cfg.get("shot_action") or "").strip()
    if action and not _covers_beat(text, action):
        reasons.append("misses_storyboard_beat")
    if action and not _story_leads_prompt(text, action):
        reasons.append("story_buried_under_locks")
    # Film-lock content coverage (story prose, not LOCK banners).
    if _has_spoken_dialogue(cfg):
        lang = str(cfg.get("language_lock") or "").strip()
        if lang:
            display = _language_display(lang).lower()
            mentioned = "spoken dialogue" in text.casefold() or (
                display
                and re.search(rf"(?i)\b{re.escape(display)}\b", text) is not None
            )
            if not mentioned and len(lang) > 3 and lang.lower() in text.casefold():
                mentioned = True
            if not mentioned:
                reasons.append("missing_language_lock")
    payload = _time_of_day_payload(cfg)
    if (
        payload.get("time_of_day")
        and payload["time_of_day"].lower() != "unspecified"
        and not _tod_covered_in_text(text, cfg)
    ):
        reasons.append("missing_time_of_day")
    quotes = [str(cfg.get("speech_line") or "").strip().strip('"')]
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    quotes.extend(str(value or "").strip().strip('"') for value in by_char.values())
    gone = {n.casefold() for n in exited_names(cfg, graph)}
    for quote in quotes:
        if not quote:
            continue
        # Skip speech attributed only to exited people — they are omitted from the call.
        skip = False
        for key, value in by_char.items():
            if str(value or "").strip().strip('"').casefold() == quote.casefold():
                if _name_for_id(str(key), graph).casefold() in gone:
                    skip = True
                    break
        if skip:
            continue
        if quote.casefold() not in text.casefold():
            reasons.append("misses_speech")
            break
    return reasons


def _pull_labeled(prompt: str, prefixes: tuple[str, ...]) -> str:
    """First matching label wins. Prefix order is priority, not line order."""
    lines = [raw.strip().lstrip("-").strip() for raw in str(prompt or "").splitlines()]
    for prefix in prefixes:
        for line in lines:
            low = line.lower()
            rest = low[len(prefix):].lstrip() if low.startswith(prefix) else ""
            if rest and re.match(r"^(?:\d+\s*)?:", rest):
                value = line.split(":", 1)[-1].strip().strip(".")
                value = re.split(
                    r"(?i)\b(?:no whip|no crash|do not|don't|never|forbid)\b",
                    value,
                    maxsplit=1,
                )[0].strip(" ,;.—-")
                if value and not _BAD_DIRECTIVE.search(value):
                    return value[:240]
    return ""


def director_prepare_video_prompt(
    prompt: str,
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    shot_index: int = 0,
    model: str | None = None,
    action: str = "",
    camera: str = "",
    extra_image_labels: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Keep a concise faithful story-form prompt; otherwise rewrite locks into narrative."""
    del shot_index
    cfg = cfg if isinstance(cfg, dict) else {}
    if str(cfg.get("reference_call_mode") or "").strip():
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            compose_reference_clip_prompt,
            reference_prompt_issues,
        )

        raw = str(prompt or "").strip()
        reasons = reference_prompt_issues(raw, cfg)
        if not reasons:
            return raw[:2200], ["kept_reference_led_prompt"]
        return compose_reference_clip_prompt(cfg)[:2200], ["rewritten_reference_led", *reasons]
    reasons = prompt_respects_practice(prompt, cfg=cfg, graph=graph)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
            prompt_violates_continuity,
        )

        for r in prompt_violates_continuity(prompt, cfg=cfg):
            if r not in reasons:
                reasons.append(r)
    except Exception:  # noqa: BLE001
        pass
    # Conciseness: lock essays / labeled banners are not story form.
    raw = str(prompt or "").strip()
    if raw and len(raw) > 1600:
        reasons = [*reasons, "not_concise"]
    if extra_image_labels:
        reasons = [*reasons, "wired_user_images"]
    if raw and re.search(
        r"(?i)\b(?:costume lock|positioning lock|scene specs|style lock|forbid|already-?done)\b"
        r"|^\s*on screen\s*:",
        raw,
        flags=re.M,
    ):
        reasons = [*reasons, "lock_banner_not_story"]
    if not reasons:
        return raw[:2200], ["kept_agent_prompt"]
    mined_action = (
        action
        or str(cfg.get("shot_action") or "")
        or _pull_labeled(prompt, ("primary action", "character action", "storyboard shot", "storyboard beat", "action"))
    )
    move = _pull_labeled(prompt, ("camera move",))
    angle = (
        camera
        or str(cfg.get("camera") or "")
        or _pull_labeled(prompt, ("camera for shot", "camera"))
    )
    if move and angle and move.casefold() not in angle.casefold():
        mined_camera = f"{move}, {angle}"
    else:
        mined_camera = move or angle
    composed = compose_practice_prompt(
        cfg=cfg,
        graph=graph,
        action=mined_action,
        camera=mined_camera,
        model=model,
        extra_image_labels=extra_image_labels,
    )
    return composed, ["director_rewrote", *reasons]


def director_approve_video_prompt(
    prompt: str,
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    shot_index: int = 0,
    model: str | None = None,
    action: str = "",
    camera: str = "",
    extra_image_labels: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Director gate: concise story-form that still enforces wardrobe/seat/visibility locks.

    Approves a leaf rewrite when it is narrative, faithful, and short. Otherwise edits
    via compose_practice_prompt into the story-form Image-N binding.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    beat = (
        action
        or str(cfg.get("shot_action") or "")
        or _pull_labeled(prompt, ("primary action", "character action", "storyboard shot", "storyboard beat", "action"))
    )
    cam = (
        camera
        or str(cfg.get("camera") or "")
        or _pull_labeled(prompt, ("camera move", "camera for shot", "camera"))
    )
    approved, notes = director_prepare_video_prompt(
        prompt,
        cfg=cfg,
        graph=graph,
        shot_index=shot_index,
        model=model,
        action=beat,
        camera=cam,
        extra_image_labels=extra_image_labels,
    )
    reasons = list(notes)
    if beat and not _covers_beat(approved, beat):
        approved = compose_practice_prompt(
            cfg=cfg,
            graph=graph,
            action=beat,
            camera=cam,
            model=model,
            extra_image_labels=extra_image_labels,
        )
        reasons = ["director_rewrote_for_storyboard", *reasons]
    elif beat and not _story_leads_prompt(approved, beat):
        approved = compose_practice_prompt(
            cfg=cfg,
            graph=graph,
            action=beat,
            camera=cam,
            model=model,
            extra_image_labels=extra_image_labels,
        )
        reasons = ["director_rewrote_story_first", *reasons]
    elif "director_rewrote" not in notes and "kept_agent_prompt" in notes:
        reasons = ["director_approved", *reasons]
    else:
        reasons = ["director_checked", *reasons]
    return approved.strip()[:2200], reasons
