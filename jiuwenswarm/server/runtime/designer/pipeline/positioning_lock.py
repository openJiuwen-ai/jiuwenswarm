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

_SEPARATOR = {"en": ", ", "zh": "\uff0c"}
_JOINER = {"en": "; ", "zh": "\uff1b"}


def _seat_parts(anchor: dict[str, Any], geo: str, lang: str) -> list[str]:
    bits: list[str] = []
    zone = str(anchor.get("zone") or "").strip().lower()
    if zone in _ZONE_WORDS[lang]:
        bits.append(_ZONE_WORDS[lang][zone])
    elif str(anchor.get("seat") or "").strip():
        bits.append(str(anchor["seat"]).strip())
    landmark = str(anchor.get("landmark") or geo or "").strip()
    if landmark:
        bits.append(f"near the {landmark}" if lang == "en" else f"\u9760\u8fd1{landmark}")
    facing = str(anchor.get("facing") or "").strip()
    if facing:
        bits.append(f"facing {facing}" if lang == "en" else f"\u9762\u5411{facing}")
    return bits


def build_lock(
    *,
    cfg: dict[str, Any] | None,
    names: dict[str, str] | None = None,
    lang: str = "en",
) -> str:
    """One positive sentence fixing each visible character's place in this setting."""
    cfg = cfg if isinstance(cfg, dict) else {}
    names = names or {}
    blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    geo = str(spatial.get("landmark") or blocking.get("landmark") or "").strip()
    positions = [p for p in (blocking.get("positions") or []) if isinstance(p, dict)]
    sentences: list[str] = []
    for pos in positions:
        token = str(pos.get("character_id") or "").strip()
        name = names.get(token) or str(pos.get("name") or "").strip()
        if not name:
            continue
        bits = _seat_parts(pos, geo, lang)
        if not bits:
            continue
        joined = _SEPARATOR[lang].join(bits)
        sentences.append(f"{name} {joined}" if lang == "en" else f"{name}{joined}")
    if sentences:
        joined = _JOINER[lang].join(sentences)
        return (
            f"Positions held for this setting: {joined}."
            if lang == "en"
            else f"\u672c\u573a\u666f\u56fa\u5b9a\u4f4d\u7f6e\uff1a{joined}\u3002"
        )
    layout = str(spatial.get("layout") or "").strip()
    while layout and layout[-1] in ".;\u3002\uff1b":
        layout = layout[:-1].rstrip()
    if layout:
        if lang == "en":
            return f"Positions held for this setting: {layout}."
        return f"\u672c\u573a\u666f\u56fa\u5b9a\u4f4d\u7f6e\uff1a{layout}\u3002"
    return ""


def stamp_lock(cfg: dict[str, Any] | None, *, names: dict[str, str] | None = None, lang: str = "en") -> str:
    """Compute and store the lock on the config; returns the sentence."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lock = build_lock(cfg=cfg, names=names, lang=lang)
    if lock:
        cfg["positioning_lock_text"] = lock
    return lock
