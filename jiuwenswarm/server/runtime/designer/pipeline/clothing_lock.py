# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Structured clothing / wardrobe locks for cast consistency across keyframes + clips.

costume_lock must name garment slots (top/shirt, bottom/trousers, footwear, outerwear,
accessories) with style + color — not a vague one-liner — and the same lock is injected
into both still and video prompts.
"""

from __future__ import annotations

import re
from typing import Any

_COLOR = (
    r"(?:light|dark|pale|deep|bright|muted|navy|charcoal|beige|cream|ivory|khaki|"
    r"olive|maroon|burgundy|teal|coral|pastel|black|white|grey|gray|blue|red|green|"
    r"brown|tan|pink|purple|yellow|orange|gold|silver|denim)"
)
_COLOR_PHRASE = rf"(?:{_COLOR}(?:[\s\-]+{_COLOR})*)"

_ACCESSORY_RE = re.compile(
    r"\b(?P<item>glasses|eyeglasses|sunglasses|watch|belt|scarf|hat|cap|tie|necklace|"
    r"earrings|backpack|handbag|bag|briefcase)\b",
    re.I,
)


def _clean_bit(*parts: str) -> str:
    raw = " ".join(p.strip() for p in parts if p and str(p).strip())
    return re.sub(r"\s+", " ", raw).strip(" -,").lower()


def _window_before(blob: str, end: int, *, span: int = 48) -> str:
    return blob[max(0, end - span) : end]


def _color_in(window: str) -> str:
    matches = list(re.finditer(_COLOR_PHRASE, window, flags=re.I))
    if not matches:
        return ""
    return _clean_bit(matches[-1].group(0))


def _styles_in(window: str, tokens: tuple[str, ...]) -> str:
    found: list[str] = []
    low = window.lower()
    for tok in tokens:
        if re.search(rf"\b{re.escape(tok)}\b", low):
            found.append(tok)
    return " ".join(found)


_TOP_STYLES = (
    "short-sleeve",
    "short sleeve",
    "long-sleeve",
    "long sleeve",
    "sleeveless",
    "button-up",
    "button up",
    "button-down",
    "collared",
    "polo",
    "v-neck",
    "crew-neck",
    "turtleneck",
    "fitted",
    "loose",
    "oversized",
)
_BOTTOM_STYLES = (
    "slim",
    "straight",
    "wide-leg",
    "wide leg",
    "cropped",
    "pleated",
    "ripped",
    "skinny",
    "relaxed",
)
_DRESS_STYLES = ("midi", "maxi", "mini", "cocktail", "evening", "casual")


def extract_clothing_parts(text: str) -> dict[str, str]:
    """Parse garment slots from free-text character description / costume_lock."""
    blob = re.sub(r"\s+", " ", (text or "").strip())
    parts: dict[str, str] = {}
    if not blob:
        return parts

    m = re.search(
        r"\b(?P<item>dress|gown|suit|jumpsuit|romper)\b",
        blob,
        re.I,
    )
    if m:
        window = _window_before(blob, m.start())
        parts["outfit"] = _clean_bit(
            _color_in(window),
            _styles_in(window, _DRESS_STYLES),
            m.group("item"),
        )

    m = re.search(
        r"\b(?P<item>shirt|blouse|tee|t[\s\-]?shirt|top|sweater|jumper|hoodie|"
        r"tank[\s\-]?top|camisole|henley|cardigan)\b",
        blob,
        re.I,
    )
    if m:
        window = _window_before(blob, m.start())
        parts["top"] = _clean_bit(
            _color_in(window),
            _styles_in(window, _TOP_STYLES),
            m.group("item"),
        )

    m = re.search(
        r"\b(?P<item>trousers|pants|jeans|slacks|chinos|shorts|skirt|leggings|"
        r"cargo[\s\-]?pants)\b",
        blob,
        re.I,
    )
    if m:
        window = _window_before(blob, m.start())
        parts["bottom"] = _clean_bit(
            _color_in(window),
            _styles_in(window, _BOTTOM_STYLES),
            m.group("item"),
        )

    m = re.search(
        r"\b(?P<item>jacket|coat|blazer|windbreaker|parka|trench|vest)\b",
        blob,
        re.I,
    )
    if m:
        window = _window_before(blob, m.start())
        parts["outerwear"] = _clean_bit(_color_in(window), m.group("item"))

    m = re.search(
        r"\b(?P<item>shoes|sneakers|boots|loafers|heels|sandals|trainers|oxfords|flats)\b",
        blob,
        re.I,
    )
    if m:
        window = _window_before(blob, m.start())
        parts["footwear"] = _clean_bit(_color_in(window), m.group("item"))

    accessories = []
    for m in _ACCESSORY_RE.finditer(blob):
        item = m.group("item").lower()
        if item not in accessories:
            accessories.append(item)
    if accessories:
        parts["accessories"] = ", ".join(accessories[:6])

    return parts


def format_clothing_slots(parts: dict[str, str] | None, *, fallback: str = "") -> str:
    """Render garment slots as a stable lock string."""
    parts = {k: str(v).strip() for k, v in (parts or {}).items() if str(v).strip()}
    order = ("outfit", "top", "bottom", "outerwear", "footwear", "accessories")
    bits = [f"{key}={parts[key]}" for key in order if key in parts]
    if bits:
        return "; ".join(bits)
    fb = re.sub(r"\s+", " ", (fallback or "").strip())
    return fb[:280] if fb else ""


def clothing_hold_rule() -> str:
    return (
        "CLOTHING HOLD: keep exact garment types, styles, sleeve/collar cuts, fabrics, "
        "and colors for top/shirt, trousers/skirt/bottom, outerwear, footwear, and "
        "accessories — FORBIDDEN: swap colors, change sleeve length, redesign collar, "
        "switch jeans↔trousers, invent a new jacket, or restyle wardrobe mid-film."
    )


def detailed_costume_lock_for_character(ch: dict[str, Any] | None) -> str:
    """Build a garment-level costume_lock string for one character."""
    if not isinstance(ch, dict):
        return ""
    name = str(ch.get("name") or ch.get("id") or "character").strip()
    existing_parts = (
        ch.get("clothing_parts") if isinstance(ch.get("clothing_parts"), dict) else {}
    )
    blob = " ".join(
        str(ch.get(k) or "")
        for k in ("costume_lock", "description", "wardrobe_lock", "prompt")
    )
    attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
    if attrs.get("wardrobe"):
        blob = f"{blob} {attrs.get('wardrobe')}"
    parts = dict(existing_parts) if existing_parts else extract_clothing_parts(blob)
    if not parts and str(ch.get("costume_lock") or "").strip():
        # Already structured (top=...; bottom=...).
        raw = str(ch.get("costume_lock") or "")
        if "=" in raw and ("top=" in raw or "bottom=" in raw or "outfit=" in raw):
            return raw if raw.lower().startswith(name.lower()) else f"{name}: {raw}"[:480]
    slots = format_clothing_slots(parts, fallback=str(ch.get("description") or ch.get("costume_lock") or name))
    if not slots:
        slots = f"canonical film wardrobe for {name} — keep exact top/bottom/shoes colors+styles"
    return f"{name}: {slots}"[:480]


def enrich_character_clothing(ch: dict[str, Any]) -> str:
    """Stamp clothing_parts + detailed costume_lock onto a character dict. Returns lock."""
    if not isinstance(ch, dict):
        return ""
    blob = " ".join(
        str(ch.get(k) or "")
        for k in ("description", "costume_lock", "wardrobe_lock")
    )
    attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
    if attrs.get("wardrobe"):
        blob = f"{blob} {attrs.get('wardrobe')}"
    parts = extract_clothing_parts(blob)
    if parts:
        ch["clothing_parts"] = parts
    lock = detailed_costume_lock_for_character(ch)
    if lock:
        ch["costume_lock"] = lock[:480]
        if isinstance(attrs, dict):
            attrs = dict(attrs)
            attrs["wardrobe"] = format_clothing_slots(parts, fallback=lock)[:280]
            ch["identity_attrs"] = attrs
    return lock


def costume_lock_for_ids(
    characters: list[dict[str, Any]] | None,
    character_ids: list[str] | None,
) -> str:
    """Join detailed clothing locks for the on-screen cast."""
    id_to = {
        str(c.get("id")): c
        for c in (characters or [])
        if isinstance(c, dict) and c.get("id")
    }
    parts: list[str] = []
    for cid in character_ids or []:
        ch = id_to.get(str(cid))
        if not ch:
            continue
        lock = detailed_costume_lock_for_character(ch)
        if lock:
            parts.append(lock)
    return "; ".join(parts)[:720]


def clothing_lock_clause(
    costume_lock: str,
    *,
    for_clip: bool = False,
) -> str:
    """Prompt block injected into keyframes and clips."""
    text = re.sub(r"\s+", " ", (costume_lock or "").strip())
    if not text:
        return ""
    where = "this clip (match Image 1 + identity sheets)" if for_clip else "this keyframe"
    return (
        f"CLOTHING LOCK ({where}): {text[:720]}\n"
        + clothing_hold_rule()
    )


def ensure_cfg_clothing_lock(
    cfg: dict[str, Any],
    *,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Ensure node config has a detailed costume_lock; return it."""
    if not isinstance(cfg, dict):
        return ""
    existing = str(cfg.get("costume_lock") or "").strip()
    cids = [str(x) for x in (cfg.get("character_ids") or cfg.get("on_screen") or []) if str(x)]
    detailed = ""
    if characters and cids:
        detailed = costume_lock_for_ids(characters, cids)
    # Upgrade vague locks (no garment slots) when we can detail them.
    needs_upgrade = bool(detailed) and (
        not existing
        or (
            "top=" not in existing
            and "bottom=" not in existing
            and "outfit=" not in existing
            and len(existing) < 40
        )
    )
    if needs_upgrade:
        cfg["costume_lock"] = detailed[:720]
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        identity = dict(identity)
        identity["costume_lock"] = detailed[:720]
        cfg["identity_refs"] = identity
        return detailed[:720]
    if existing:
        # Still attach hold-friendly structured copy when missing slots but text is rich.
        if characters and cids and "top=" not in existing and "bottom=" not in existing:
            upgraded = costume_lock_for_ids(characters, cids)
            if upgraded and ("top=" in upgraded or "bottom=" in upgraded or "outfit=" in upgraded):
                cfg["costume_lock"] = upgraded[:720]
                return upgraded[:720]
        return existing[:720]
    if detailed:
        cfg["costume_lock"] = detailed[:720]
        return detailed[:720]
    return ""
