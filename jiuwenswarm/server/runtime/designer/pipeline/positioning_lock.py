# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Where each visible character stands, stated once per setting and never contradicted.

A shot is written from text, so the only way two shots of one place agree on who is
where is to state the arrangement in positive words: the same sides of the frame,
the same landmark, the same neighbours. The layout comes from the shot's declared
blocking when the Director wrote one, and from the setting's spatial layout
otherwise; either way it is phrased once and reused by every shot of that setting.
"""

from __future__ import annotations

from typing import Any

_ZONE_WORDS = {
    "en": {
        "left": "on the left of frame",
        "right": "on the right of frame",
        "center": "at frame centre",
        "foreground": "in the foreground",
        "background": "in the background",
    },
    "zh": {
        "left": "\u753b\u9762\u5de6\u4fa7",
        "right": "\u753b\u9762\u53f3\u4fa7",
        "center": "\u753b\u9762\u4e2d\u95f4",
        "foreground": "\u524d\u666f",
        "background": "\u80cc\u666f",
    },
}

# Screen sides belong to one camera, not to the room. A layout sentence copied into a
# shot that looks from the other side still reads as a frame-side claim, and the model
# then keeps the framing instead of moving the camera (or mirrors the people to satisfy
# it). Only room-relative words survive into the per-setting lock.
_SCREEN_SIDE = (
    "\u753b\u9762\u5de6",
    "\u753b\u9762\u53f3",
    "\u753b\u9762\u4e2d",
    "\u753b\u9762\u4e0a\u65b9",
    "\u753b\u9762\u4e0b\u65b9",
    "\u753b\u9762\u8fd1\u5904",
    "\u753b\u9762\u6df1\u5904",
    "\u753b\u9762\u5185",
    "left of frame",
    "right of frame",
    "left side of the frame",
    "right side of the frame",
    "screen left",
    "screen right",
    "upper frame",
    "lower frame",
    "frame left",
    "frame right",
)
_ORDER_SIDE = (
    "\u4ece\u5de6\u5230\u53f3",
    "\u81ea\u5de6\u81f3\u53f3",
    "\u4ece\u5de6\u81f3\u53f3",
    "\u4ece\u53f3\u5230\u5de6",
    "\u81ea\u53f3\u81f3\u5de6",
    "left to right",
    "right to left",
    "from left",
    "from right",
)
_CLAUSE_SPLIT = ("\uff1b", ";", "\u3002", "\uff0c", ",")
_ORDER_JOINERS = ("\u2014", "\u2013", "-", "\u2192", "\uff0c", ",", "\u3001")


def _sentences(text: str) -> list[str]:
    parts = [text]
    for sep in ("\uff1b", ";", "\u3002", "\n"):
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(sep))
        parts = expanded
    return [p.strip(" \t") for p in parts if p.strip(" \t")]


def _phrases(sentence: str) -> list[str]:
    parts = [sentence]
    for sep in ("\uff0c", ","):
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(sep))
        parts = expanded
    return [p.strip() for p in parts if p.strip()]


def _is_screen_side(clause: str) -> bool:
    low = clause.lower()
    return any(word in low for word in _SCREEN_SIDE)


def _drop_brackets(text: str) -> str:
    """Remove bracketed hints ? a row's "left end / middle" notes only fit one camera."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch in "(\uff08[\u3010":
            depth += 1
            continue
        if ch in ")\uff09]\u3011":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _ordered_sequence(sentence: str) -> list[str]:
    """The seat names of a "left to right A, B, C" phrase, or []."""
    low = sentence.lower()
    if not any(word in low for word in _ORDER_SIDE):
        return []
    tail = sentence
    for sep in ("\uff1a", ":", "\u4e3a", "="):
        if sep in tail:
            tail = tail.split(sep, 1)[1]
            break
    names = [tail]
    for joiner in _ORDER_JOINERS:
        expanded: list[str] = []
        for part in names:
            expanded.extend(part.split(joiner))
        names = expanded
    cleaned: list[str] = []
    for raw in names:
        name = _drop_brackets(raw.strip())
        name = name.strip("()\uff08\uff09[]\u3010\u3011, \t")
        if name and len(name) <= 24:
            cleaned.append(name)
    return cleaned if len(cleaned) >= 2 else []


def room_relative(text: str, lang: str = "en") -> str:
    """Drop the camera-dependent parts of a layout sentence, keep the room's own terms.

    A seat map written as a screen order becomes an invariant order instead: the row
    keeps its sequence in the room, and a shot from the other side sees that same row
    mirrored. Everything that names a screen side is dropped, because it is only true
    for the one camera it was written for.
    """
    kept: list[str] = []
    for sentence in _sentences(str(text or "")):
        sequence = _ordered_sequence(sentence)
        if sequence:
            joined = " \u2192 ".join(sequence)
            kept.append(
                f"seating order fixed as {joined}; the same order reads mirrored from the "
                "other side of the room, and nobody changes seat"
                if lang == "en"
                else f"\u5ea7\u6b21\u987a\u5e8f\u56fa\u5b9a\u4e3a{joined}\uff0c"
                f"\u4ece\u53e6\u4e00\u4fa7\u62cd\u6444\u65f6\u540c\u4e00\u987a\u5e8f\u5728\u753b\u9762\u4e2d\u955c\u50cf\uff0c"
                f"\u4eba\u7269\u4e0d\u6362\u5ea7"
            )
            continue
        for phrase in _phrases(sentence):
            if _is_screen_side(phrase):
                continue
            kept.append(phrase)
    return _JOINER[lang].join(kept)


_SEPARATOR = {"en": ", ", "zh": "\uff0c"}
_JOINER = {"en": "; ", "zh": "\uff1b"}


def _seat_parts(
    anchor: dict[str, Any], geo: str, lang: str, *, include_zone: bool = True
) -> list[str]:
    bits: list[str] = []
    zone = str(anchor.get("zone") or "").strip().lower()
    if include_zone and zone in _ZONE_WORDS[lang]:
        bits.append(_ZONE_WORDS[lang][zone])
    elif str(anchor.get("seat") or "").strip():
        bits.append(str(anchor["seat"]).strip())
    landmark = str(anchor.get("landmark") or geo or "").strip()
    if landmark:
        bits.append(
            f"near the {landmark}" if lang == "en" else f"\u9760\u8fd1{landmark}"
        )
    facing = str(anchor.get("facing") or "").strip()
    if facing:
        bits.append(f"facing {facing}" if lang == "en" else f"\u9762\u5411{facing}")
    return bits


def merged_positions(shots: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One arrangement for a setting, taken from every shot's declared blocking.

    The place has one layout; the individual shots only ever see part of it. Walking
    the shots in film order and keeping the first statement per character gives a
    complete map that no single shot can drift away from.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        blocking = (
            shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
        )
        landmark = str(blocking.get("landmark") or "").strip()
        for pos in blocking.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            token = str(pos.get("character_id") or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            entry = dict(pos)
            if landmark and not str(entry.get("landmark") or "").strip():
                entry["landmark"] = landmark
            merged.append(entry)
    return merged


def build_lock(
    *,
    cfg: dict[str, Any] | None,
    names: dict[str, str] | None = None,
    lang: str = "en",
    visible: list[str] | None = None,
) -> str:
    """Who stands where in this room, stated so a camera move cannot rewrite it.

    Three things travel with every shot: the arrangement, the fixed anchors of the
    room, and the fact that frame sides follow the camera while the room does not
    move. A close-up also learns who is still in the room behind it, so the room does
    not empty out just because the shot looks at one detail of it.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    names = names or {}
    blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
    spatial = (
        cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    )
    geo = str(spatial.get("landmark") or blocking.get("landmark") or "").strip()
    setting_positions = [
        p for p in (spatial.get("positions") or []) if isinstance(p, dict)
    ]
    positions = setting_positions or [
        p for p in (blocking.get("positions") or []) if isinstance(p, dict)
    ]
    if not visible:
        visible = [
            str(x).strip()
            for x in (cfg.get("on_screen") or cfg.get("character_ids") or [])
            if str(x).strip()
        ]
    rest: list[str] = []
    if visible:
        wanted = {str(v).strip() for v in visible if str(v).strip()}
        narrowed = [
            p for p in positions if str(p.get("character_id") or "").strip() in wanted
        ]
        rest = [
            names.get(str(p.get("character_id") or "").strip())
            or str(p.get("name") or "").strip()
            for p in positions
            if str(p.get("character_id") or "").strip() not in wanted
        ]
        rest = [name for name in rest if name]
        if narrowed:
            positions = narrowed
    phrases: list[str] = []
    for pos in positions:
        token = str(pos.get("character_id") or "").strip()
        name = names.get(token) or str(pos.get("name") or "").strip()
        if not name:
            continue
        # No frame zone here: this sentence travels to every camera in the setting.
        bits = _seat_parts(pos, geo, lang, include_zone=False)
        if not bits:
            continue
        joined = _SEPARATOR[lang].join(bits)
        phrases.append(f"{name} {joined}" if lang == "en" else f"{name}{joined}")
    layout = str(spatial.get("layout") or "").strip()
    while layout and layout[-1] in ".;\u3002\uff1b":
        layout = layout[:-1].rstrip()
    arrangement = _JOINER[lang].join(phrases) or room_relative(layout, lang)
    landmarks = room_relative(str(spatial.get("landmarks") or "").strip(), lang)
    while landmarks and landmarks[-1] in ".;\u3002\uff1b":
        landmarks = landmarks[:-1].rstrip()

    clauses: list[str] = []
    if arrangement:
        clauses.append(
            f"Positions held for this setting: {arrangement}."
            if lang == "en"
            else f"\u672c\u573a\u666f\u56fa\u5b9a\u4f4d\u7f6e\uff1a{arrangement}\u3002"
        )
    if landmarks:
        clauses.append(
            f"Anchors of this room: {landmarks}."
            if lang == "en"
            else f"\u573a\u666f\u951a\u70b9\uff1a{landmarks}\u3002"
        )
    if rest:
        who = _SEPARATOR[lang].join(rest)
        clauses.append(
            f"Still in this room and keeping their places: {who}."
            if lang == "en"
            else f"\u4ecd\u5728\u672c\u573a\u666f\u3001\u4f4d\u7f6e\u4e0d\u53d8\uff1a{who}\u3002"
        )
    entities = [names.get(str(v).strip()) or "" for v in (visible or [])]
    entities = [n for n in entities if n]
    leaving = [
        str(x)
        for x in (
            cfg.get("exiting_character_ids") or cfg.get("exit_character_ids") or []
        )
        if str(x).strip()
    ]
    entering = [
        str(x) for x in (cfg.get("entering_character_ids") or []) if str(x).strip()
    ]
    if entities and not leaving and not entering:
        who = _SEPARATOR[lang].join(entities)
        clauses.append(
            f"All {len(entities)} of them - {who} - stay in frame from the first frame to "
            "the last, and the frame holds exactly this group."
            if lang == "en"
            else f"\u8fd9{len(entities)}\u4eba\uff08{who}\uff09\u4ece\u7b2c\u4e00\u5e27\u5230\u6700\u540e\u4e00\u5e27\u90fd\u5728\u753b\u9762\u5185\uff0c"
            f"\u753b\u9762\u59cb\u7ec8\u53ea\u6709\u8fd9\u4e00\u7ec4\u4eba"
        )
    clauses.append(
        "Everyone keeps the posture and seat they have when the shot opens, through the "
        "last frame; a reach, a turn or a gesture stays inside that posture."
        if lang == "en"
        else "\u6bcf\u4eba\u4ece\u955c\u5934\u5f00\u59cb\u5230\u6700\u540e\u4e00\u5e27\u4fdd\u6301"
        "\u8d77\u59cb\u7684\u59ff\u6001\u4e0e\u5ea7\u4f4d\uff1b\u4f38\u624b\u3001\u8f6c\u8eab\u6216\u624b\u52bf"
        "\u90fd\u5728\u8fd9\u4e2a\u59ff\u6001\u4e4b\u5185"
    )
    if clauses:
        clauses.append(
            "Frame sides follow the camera while the walls, the furniture and the people "
            "keep their places; when the camera looks the other way the same chairs appear "
            "on the other side of frame and the same people sit in them."
            if lang == "en"
            else "\u753b\u9762\u5de6\u53f3\u968f\u955c\u5934\u6539\u53d8\uff0c\u5899\u9762\u3001\u9648\u8bbe\u4e0e\u4eba\u7269\u5404\u5b88\u5176\u4f4d\uff1b"
            "\u955c\u5934\u53cd\u6253\u65f6\uff0c\u540c\u4e00\u6279\u6905\u5b50\u51fa\u73b0\u5728\u753b\u9762\u53e6\u4e00\u4fa7\uff0c"
            "\u5750\u5728\u4e0a\u9762\u7684\u8fd8\u662f\u540c\u4e00\u6279\u4eba"
        )
    return "\n".join(clauses)


def stamp_lock(
    cfg: dict[str, Any] | None,
    *,
    names: dict[str, str] | None = None,
    lang: str = "en",
    visible: list[str] | None = None,
) -> str:
    """Compute and store the lock on the config; returns the sentence."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lock = build_lock(cfg=cfg, names=names, lang=lang, visible=visible)
    if lock:
        cfg["positioning_lock_text"] = lock
    return lock
