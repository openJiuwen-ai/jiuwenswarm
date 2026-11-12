# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Structured clothing / wardrobe locks for cast consistency across clip specs + clips.

costume_lock must name garment slots (top/shirt, bottom/trousers, footwear, outerwear,
accessories) with style + color — not a vague one-liner — and the same lock is injected
into both image and video prompts.
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
    return blob[max(0, end - span):end]


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
_SLOT_KEYS = ("top=", "bottom=", "outfit=")
CLOTHING_SLOTS = ("outfit", "top", "bottom", "outerwear", "footwear", "accessories")
CLOTHING_PARTS_RULE = (
    "Every characters[] entry MUST carry clothing_parts — garment slots outfit (one-piece "
    "dress/suit/robe), top, bottom, outerwear, footwear, accessories — each with style and "
    "color, written in content_language; leave a slot empty when it does not apply. "
    "description locks face, age, hair, and body; clothing_parts is the wardrobe authority. "
)
_SLOT_LABELS = (
    "outer wear",
    "foot wear",
    "under wear",
    "outerwear",
    "footwear",
    "underwear",
    "accessories",
    "accessory",
    "outfit",
    "bottom",
    "top",
)


def _strip_slot_labels(text: str) -> str:
    """Drop slot labels ("outerwear=", "Outer wear:", "footwear:") and keep the garment."""
    out = str(text or "")
    changed = True
    while changed:
        changed = False
        low = out.casefold()
        for label in _SLOT_LABELS:
            at = low.find(label)
            while at >= 0:
                if at and (low[at - 1].isalnum() or low[at - 1] == "_"):
                    at = low.find(label, at + 1)
                    continue
                rest = out[at + len(label) :].lstrip(" \t")
                if rest[:1] in ("=", ":", "\uff1a"):
                    out = out[:at] + rest[1:].lstrip(" \t")
                    changed = True
                break
            if changed:
                break
    return out


def _has_clothing_slots(text: str) -> bool:
    return any(key in text for key in _SLOT_KEYS)


def _cjk_run(text: str, limit: int = 41) -> str:
    """First Chinese run of a phrase, stopping at a parenthesis."""
    start = next((i for i, ch in enumerate(text) if "\u4e00" <= ch <= "\u9fff"), -1)
    if start < 0:
        return ""
    out: list[str] = []
    for ch in text[start : start + limit]:
        if ch in "()\uff08\uff09":
            break
        out.append(ch)
    return "".join(out)


_EDGE_JUNK = " \t\r\n,;\u3001\u3002\uff0c\uff1b"


def condense_garment(text: str, *, limit: int = 160) -> str:
    """Keep one copy of a garment clause.

    A lock that has been read and re-rendered a few times can carry its own
    text inside its own parentheses ("jumper (jumper (ZH), wool), wool").
    Keep the head garment plus its first translation so the clause is stable
    when it is read again.
    """
    value = " ".join(str(text or "").split()).strip(_EDGE_JUNK)
    if not value:
        return ""
    head = value.split("(", 1)[0].strip(_EDGE_JUNK)
    repeated = bool(head) and len(head) >= 8 and value.casefold().count(head.casefold()) > 1
    if not repeated and len(value) <= limit:
        return value
    zh = _cjk_run(value)
    if zh and head:
        return f"{head} ({zh})"[:limit]
    return (head or value)[:limit]


def normalize_clothing_parts(raw: Any) -> dict[str, str]:
    """Garment slots authored by the LLM (any language) as a clean slot→text map."""
    if not isinstance(raw, dict):
        return {}
    parts: dict[str, str] = {}
    for slot in CLOTHING_SLOTS:
        value = raw.get(slot)
        if isinstance(value, (list, tuple)):
            items = [" ".join(str(v).split()) for v in value]
            text = ", ".join(item for item in items if item)
        else:
            text = " ".join(str(value or "").split())
        # Slot values are joined with ";" / "=" in costume_lock.
        text = text.replace("=", " ").replace(";", ",").strip(" ,\uff0c;\uff1b.\u3002")
        if text:
            parts[slot] = condense_garment(text)
    return parts


def wearable_phrase(text: str, *, name: str = "", lang: str = "en") -> str:
    """Readable garment phrase from a costume_lock: no "Name:" prefix or slot keys."""
    from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
        clause_separator,
    )

    raw = " ".join(str(text or "").split()).strip()
    if name:
        head, sep, rest = raw.partition(":")
        if not sep:
            head, sep, rest = raw.partition("\uff1a")
        if sep and head.strip().casefold() == name.strip().casefold():
            raw = rest.strip()
    raw = _strip_slot_labels(raw)
    clauses = [part.strip() for part in raw.replace("\uff1b", ";").split(";") if part.strip()]
    raw = clause_separator(lang).join(clauses)
    return condense_garment(raw)



def extract_clothing_parts(text: str) -> dict[str, str]:
    """Fallback parse of English garment words when the LLM gave no clothing_parts."""
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


def _clamp_clause(text: str, limit: int) -> str:
    """Cut authored prose at a clause boundary instead of mid-word."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    cut = value[:limit]
    for separator in ("\uff1b", ";", "\uff0c", ", "):
        at = cut.rfind(separator)
        if at >= limit // 2:
            return cut[:at].strip(" ,\uff0c;\uff1b")
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit // 2 else cut).strip(" ,\uff0c;\uff1b")


def detailed_costume_lock_for_character(ch: dict[str, Any] | None) -> str:
    """Build a garment-level costume_lock string for one character."""
    if not isinstance(ch, dict):
        return ""
    name = str(ch.get("name") or ch.get("id") or "character").strip()
    # A costume read straight off the identity plate keeps the exact garments;
    # the slot extractor condenses them to "deep sweater" and would lose the
    # colour and fabric the plate just declared.
    clause = str(ch.get("costume_clause") or "").strip()
    if clause:
        return f"{name}: {_clamp_clause(clause, 480 - len(name) - 2)}"
    existing_parts = normalize_clothing_parts(ch.get("clothing_parts"))
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
        if "=" in raw and _has_clothing_slots(raw):
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
    parts = normalize_clothing_parts(ch.get("clothing_parts")) or extract_clothing_parts(blob)
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
    """Prompt block injected into clip specs and clips."""
    text = re.sub(r"\s+", " ", (costume_lock or "").strip())
    if not text:
        return ""
    where = "this clip (match Image 1 + character specs)" if for_clip else "this clip spec"
    return (
        f"CLOTHING LOCK ({where}): {text[:720]}\n"
        + clothing_hold_rule()
    )


_COSTUME_LABELS = ("costume", "wardrobe", "outfit", "clothing")
_COSTUME_STOPS = ("photoreal", "framing", "background", "purpose")

# A plate separates garments with ";" and may end the clause with a prop
# ("…; a brass lantern in his hand"), so a segment only belongs to the
# wardrobe when it names a garment.
_GARMENT_NOUNS = frozenset(
    """
    sweater sweaters pullover pullovers jumper jumpers cardigan cardigans shirt shirts blouse blouses
    tee tees tshirt tshirts top tops vest vests waistcoat waistcoats jacket jackets coat coats parka
    parkas anorak anoraks trench blazer blazers suit suits dress dresses gown gowns skirt skirts
    trousers pants jeans shorts leggings tights boot boots shoe shoes sneaker sneakers sandal sandals
    loafer loafers sock socks glove gloves scarf scarfs scarves shawl shawls hat hats cap caps beanie
    beanies helmet helmets mask masks apron aprons robe robes uniform uniforms belt belts tie ties
    cloak cloaks cape capes poncho ponchos overalls dungarees turban veil glasses sunglasses
    """.split()
)


def _words(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for char in str(text or "").casefold():
        if char.isalnum() or char in "'-":
            current.append(char)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _is_wardrobe_segment(text: str) -> bool:
    return any(word in _GARMENT_NOUNS for word in _words(text))


def costume_clause_from_spec_prompt(prompt: str) -> str:
    """Garment clause a character spec prompt declares, or "".

    A character identity plate carries the authoritative wardrobe as
    ``Costume (locked, exact): <garments>`` (also written ``COSTUME`` or
    ``Wardrobe``). When the user edits that plate's prompt, this is the text
    downstream nodes must wear.
    """
    text = " ".join(str(prompt or "").split())
    if not text:
        return ""
    low = text.casefold()
    body = ""
    for label in _COSTUME_LABELS:
        at = low.find(label)
        if at < 0:
            continue
        rest = text[at + len(label) :].lstrip(" \t")
        if rest.startswith("("):
            close = rest.find(")")
            rest = rest[close + 1 :].lstrip(" \t") if close >= 0 else rest
        if rest[:1] not in (":", "\uff1a"):
            continue
        body = rest[1:]
        break
    if not body:
        return ""
    stops = [len(body)]
    for marker in (". ", "\u3002"):
        at = body.find(marker)
        if at >= 0:
            stops.append(at)
    low_body = body.casefold()
    for marker in _COSTUME_STOPS:
        at = low_body.find(marker)
        if at >= 0:
            stops.append(at)
    body = body[: min(stops)]
    segments = [seg.strip(" .,;:\uff1b") for seg in body.replace("\uff1b", ";").split(";")]
    garments = [seg for seg in segments if seg and _is_wardrobe_segment(seg)]
    if not garments:
        garments = [segments[0]] if segments and segments[0] else []
    return " ".join("; ".join(garments).strip(" .,;:\uff1b").split())[:480]



def _spec_prompt_for(node: dict[str, Any]) -> str:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    for key in ("last_wan_prompt", "last_still_prompt", "prompt"):
        text = str(cfg.get(key) or "").strip()
        if text:
            return text
    gen = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    return str(gen.get("prompt") or "").strip()


def refresh_dependent_costumes(graph: dict[str, Any] | None) -> list[str]:
    """Rewrite a stale wardrobe wherever a dependent node already carries it.

    ``sync_costume_from_specs`` fixes the analysis cast, but a clip prompt that
    was authored (or edited) keeps the wardrobe text inline, and the prompt
    builder honours that authored text. Replace the old phrasing in the clip's
    prompt fields and costume lock so the next run wears what the plate shows.
    Returns short notes about what changed.
    """
    if not isinstance(graph, dict):
        return []
    notes: list[str] = []
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = analysis.get("characters") if isinstance(analysis.get("characters"), list) else []
    by_id = {
        str(ch.get("id")): ch
        for ch in characters
        if isinstance(ch, dict) and str(ch.get("id") or "").strip()
    }
    if not by_id:
        return []
    # character id -> (name, old wardrobe text, new clause)
    updates: dict[str, tuple[str, str, str]] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not _is_character_spec(cfg, node):
            continue
        clause = costume_clause_from_spec_prompt(_spec_prompt_for(node))
        if not clause:
            continue
        for cid in _character_ids_for_spec(cfg):
            ch = by_id.get(cid)
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or cid).strip()
            old = str(ch.get("costume_clause") or ch.get("costume_lock") or "").strip()
            if not old or old == clause:
                continue
            updates[cid] = (name, old, clause)
    if not updates:
        return []
    sync_costume_from_specs(graph)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        cids = _character_ids_for_spec(cfg)
        targets = [cid for cid in cids if cid in updates] or list(updates)
        changed = False
        for cid in targets:
            name, old, clause = updates[cid]
            old_phrase = wearable_phrase(old, name=name)
            new_phrase = wearable_phrase(clause, name=name)
            replacements = [(old, clause)]
            if old_phrase and old_phrase != old:
                replacements.append((old_phrase, new_phrase))
            # Structured locks ("top=cream cable-knit sweater") are quoted into
            # prompts by their slot values too, so refresh those as well; a prose
            # lock is quoted by its garment segments.
            stale_garments = _slot_values(old)
            if not stale_garments:
                stale_garments = [
                    part
                    for part in old.replace("\uff0c", ";").replace(",", ";").replace("\uff1b", ";").split(";")
                    if part.strip()
                ]
            for garment in stale_garments:
                garment = garment.strip()
                if len(garment) >= 8 and garment not in clause:
                    replacements.append((garment, new_phrase or clause))
            # Longest first, so a whole-clause match wins over a segment of it.
            replacements.sort(key=lambda pair: len(pair[0]), reverse=True)
            for key, value in list(cfg.items()):
                if not isinstance(value, str) or not value:
                    continue
                if key not in _WARDROBE_PROMPT_FIELDS and key != "costume_lock":
                    continue
                updated = value
                for stale, fresh in replacements:
                    if stale and stale in updated:
                        updated = updated.replace(stale, fresh)
                if updated != value:
                    cfg[key] = updated
                    changed = True
            prompt = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else None
            if isinstance(prompt, dict) and isinstance(prompt.get("prompt"), str):
                text = prompt["prompt"]
                updated = text
                for stale, fresh in replacements:
                    if stale and stale in updated:
                        updated = updated.replace(stale, fresh)
                if updated != text:
                    prompt = dict(prompt)
                    prompt["prompt"] = updated
                    cfg["generate"] = prompt
                    changed = True
        if changed:
            node["config"] = cfg
            notes.append(f"{node.get('id')}: wardrobe refreshed from the character spec")
    return notes


_WARDROBE_PROMPT_FIELDS = (
    "prompt",
    "last_wan_prompt",
    "last_still_prompt",
    "last_approved_prompt",
    "user_edit_prompt",
    "clip_prompt_preview",
    "director_task",
)

_SLOT_KEY_SET = frozenset(
    {"outfit", "top", "bottom", "outerwear", "outer wear", "footwear", "foot wear", "accessories"}
)


def _slot_values(lock: str) -> list[str]:
    """Values of a structured lock ("top=…; bottom=…"); a prose lock yields none."""
    values: list[str] = []
    for chunk in str(lock or "").replace("\uff1b", ";").replace("\n", ";").split(";"):
        head, sep, value = chunk.partition("=")
        if not sep or " ".join(head.split()).casefold() not in _SLOT_KEY_SET:
            continue
        value = value.strip()
        if value:
            values.append(value)
    return values


def sync_costume_from_specs(graph: dict[str, Any] | None) -> set[str]:
    """Copy each character spec node's current wardrobe into the analysis cast.

    The identity plate is the source of truth for costume. Its prompt can be
    edited and re-rendered on its own, while the stamped ``script_analysis``
    copy stayed behind, so dependent clips kept the wardrobe they were built
    with. Returns the character ids whose spec declares a costume.
    """
    if not isinstance(graph, dict):
        return set()
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = analysis.get("characters") if isinstance(analysis.get("characters"), list) else []
    if not characters:
        return set()
    by_id = {
        str(ch.get("id")): ch
        for ch in characters
        if isinstance(ch, dict) and str(ch.get("id") or "").strip()
    }
    synced: set[str] = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not _is_character_spec(cfg, node):
            continue
        clause = costume_clause_from_spec_prompt(_spec_prompt_for(node))
        if not clause:
            continue
        for cid in _character_ids_for_spec(cfg):
            ch = by_id.get(cid)
            if not isinstance(ch, dict):
                continue
            ch["costume_clause"] = clause
            # The lock keeps the cast name: a bare garment clause would be read
            # as the previous character's next slot by whoever joins these
            # blocks into one shared lock.
            lock = detailed_costume_lock_for_character(ch) or (
                f"{ch.get('name') or cid}: {_clamp_clause(clause, 400)}"
            )
            if str(ch.get("costume_lock") or "").strip() != lock:
                ch["costume_lock"] = lock
                ch["clothing_parts"] = extract_clothing_parts(clause)
                attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
                attrs = dict(attrs)
                attrs["wardrobe"] = clause[:280]
                ch["identity_attrs"] = attrs
            synced.add(cid)
    return synced


def _is_character_spec(cfg: dict[str, Any], node: dict[str, Any]) -> bool:
    pipeline = str(cfg.get("pipeline") or cfg.get("role") or "").strip().lower()
    if pipeline in {"character", "character_spec"}:
        return True
    return str(node.get("id") or "").startswith("n_character")


def _character_ids_for_spec(cfg: dict[str, Any]) -> list[str]:
    ids = [str(x).strip() for x in (cfg.get("character_ids") or []) if str(x).strip()]
    if ids:
        return ids
    single = str(cfg.get("character_id") or "").strip()
    return [single] if single else []


def spec_costume_by_character(graph: dict[str, Any] | None) -> dict[str, str]:
    """character id -> wardrobe clause declared by its identity plate node."""
    out: dict[str, str] = {}
    if not isinstance(graph, dict):
        return out
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not _is_character_spec(cfg, node):
            continue
        clause = costume_clause_from_spec_prompt(_spec_prompt_for(node))
        if not clause:
            continue
        for cid in _character_ids_for_spec(cfg):
            out[cid] = clause
    return out


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
        has_top_or_bottom = "top=" in existing or "bottom=" in existing
        if characters and cids and not has_top_or_bottom:
            upgraded = costume_lock_for_ids(characters, cids)
            if upgraded and _has_clothing_slots(upgraded):
                cfg["costume_lock"] = upgraded[:720]
                return upgraded[:720]
        return existing[:720]
    if detailed:
        cfg["costume_lock"] = detailed[:720]
        return detailed[:720]
    return ""
