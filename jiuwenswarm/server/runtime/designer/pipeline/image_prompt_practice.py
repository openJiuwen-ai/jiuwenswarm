# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Positive still prompts for Qwen / DashScope / MiniMax image backends.

Locks live on node config. The image API body is a concise positive description —
no LOCK banners, forbid lists, or worked examples. Domain-agnostic.
"""

from __future__ import annotations

import re
from typing import Any

_BAD = re.compile(
    r"(?i)(\bforbid\b|\bforbidden\b|\bdo not\b|\bdon't\b|\bnever\b|"
    r"\bmust not\b|\bno people\b|\bno faces\b|\bno bodies\b|"
    r"\bstyle lock\b|\bspatial lock\b|\baspect lock\b|\btime of day lock\b|"
    r"\bclothing lock\b|\bstaging lock\b|\bfor example\b|\be\.g\.\b)"
)
_SETTING_ID = re.compile(r"(?i)^(set|scene|setting)[_\s-]?\d+$")
_INSTRUCTION_PLACE = re.compile(
    r"(?i)("
    r"keep one coherent|one coherent interior|coherent scene|primary setting|"
    r"^setting$|static objects locked|no scene specs|never invent|as appropriate"
    r")"
)
_PLACEHOLDER_PROP = re.compile(
    r"(?i)("
    r"primary landmark|architecture massing|secondary props|floor/ground plane|"
    r"background depth cues|that define the scene|locked landmarks|as appropriate"
    r")"
)
_EXTERIOR = (
    "rooftop", "terrace", "balcony", "street", "outdoor", "exterior", "sky",
    "garden", "courtyard", "beach", "park", "天台", "阳台", "户外", "室外",
    "街道", "院子", "天空",
)
_INTERIOR = (
    "room", "kitchen", "office", "interior", "bedroom", "hallway", "hall",
    "室内", "房间", "厨房", "客厅", "办公室",
)


def _cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return cfg if isinstance(cfg, dict) else {}


def _style_look(cfg: dict[str, Any]) -> str:
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or style.get("medium") or "").strip()
    look = re.split(r"\s+[—–-]\s+|\bnever\b|\bno style\b", look, maxsplit=1, flags=re.I)[0]
    return look.strip(" .;")


def _style_is_covered(text: str, look: str) -> bool:
    body = str(text or "").lower()
    style = str(look or "").lower()
    if not style:
        return True
    if style in body:
        return True
    cues = {
        token
        for token in re.findall(r"[\w-]+", style)
        if len(token) > 4
        and token
        not in {"across", "every", "feature", "coherent", "forms", "shapes", "rendering"}
    }
    return bool(cues and any(token in body for token in cues))


def _aspect_phrase(cfg: dict[str, Any], graph: dict[str, Any] | None) -> str:
    aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
    if not aspect and isinstance(graph, dict):
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
    ratio = str((aspect or {}).get("ratio") or "").strip()
    size = str(cfg.get("image_size") or (aspect or {}).get("image_size") or "").strip()
    bits = []
    if ratio:
        bits.append(f"{ratio} framing")
    if size:
        bits.append(f"size {size}")
    return ", ".join(bits)


def _visual_lighting(text: str) -> str:
    """Drop planner clauses. Keep only the light the image can show."""
    body = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", str(text or ""), maxsplit=1)[0]
    body = re.sub(
        r"(?i)\s*;?\s*keep\s+[\w /+-]+\s+across\s+same-setting\s+shots",
        "",
        body,
    )
    body = re.sub(r"(?i)\s*;?\s*no relight mid-scene", "", body)
    return body.strip(" ,;.—-")


def visual_place_name(text: Any) -> str:
    """A place the image model can draw. Setting ids and lock sentences are not places."""
    raw = str(text or "").strip()
    raw = re.sub(r"(?i)^(set|scene|setting)[_\s-]?\d+\s*[:.\-]\s*", "", raw).strip()
    if not raw or _SETTING_ID.match(raw) or _INSTRUCTION_PLACE.search(raw):
        return ""
    if _PLACEHOLDER_PROP.search(raw) or _BAD.search(raw):
        return ""
    return raw.split(".")[0].strip()[:160]


def visual_prop_list(raw: Any) -> list[str]:
    """Concrete props only. Planner placeholders are not objects in the frame."""
    if isinstance(raw, str):
        parts = re.split(r"[,;\n，、]+", raw)
    elif isinstance(raw, list):
        parts = list(raw)
    else:
        parts = []
    out: list[str] = []
    for item in parts:
        text = str(item or "").strip()
        if (
            not text
            or _BAD.search(text)
            or _PLACEHOLDER_PROP.search(text)
            or _INSTRUCTION_PLACE.search(text)
        ):
            continue
        if text not in out:
            out.append(text[:160])
    return out[:6]


def _enclosure_phrase(place: str) -> str:
    text = str(place or "")
    low = text.lower()
    if any(word in text or word in low for word in _EXTERIOR):
        return "Exterior, with the ground and the open background."
    if any(word in text or word in low for word in _INTERIOR):
        return "Interior of this place."
    return ""


def _tod_phrase(cfg: dict[str, Any]) -> str:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = _visual_lighting(str((tod or {}).get("lighting") or bible.get("lighting") or ""))
    if label and label.lower() != "unspecified":
        if lighting and label.lower() in lighting.lower():
            return lighting.rstrip(".") + "."
        if lighting:
            return f"It is {label}; {lighting.rstrip('.')}."
        return f"It is {label}."
    if lighting:
        return lighting.rstrip(".") + "."
    return ""


def _scene_phrase(cfg: dict[str, Any]) -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    for raw in (
        bible.get("description"),
        bible.get("scene_name"),
        bible.get("place"),
        bible.get("architecture"),
        (spatial or {}).get("architecture"),
    ):
        place = visual_place_name(raw)
        if place:
            return place
    return ""


def _props_phrase(cfg: dict[str, Any]) -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    return ", ".join(visual_prop_list(bible.get("objects")))


def looks_like_lock_essay(prompt: str) -> bool:
    text = str(prompt or "")
    if not text.strip():
        return True
    if _BAD.search(text):
        return True
    if re.search(r"(?i)\b(?:STYLE|SPATIAL|ASPECT|TIME OF DAY|CLOTHING|STAGING)\s+LOCK\b", text):
        return True
    return False


def compose_scene_specs_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    seed: str = "",
) -> str:
    """Positive empty-environment plate for the image model."""
    cfg = _cfg(cfg)
    place = _scene_phrase(cfg)
    tod = _tod_phrase(cfg)
    props = _props_phrase(cfg)
    look = _style_look(cfg)
    aspect = _aspect_phrase(cfg, graph)
    bits = [f"{place}." if place else "One empty setting."]
    enclosure = _enclosure_phrase(place)
    if enclosure:
        bits.append(enclosure)
    bits.append("The setting is empty.")
    if tod:
        bits.append(tod if tod.endswith(".") else tod + ".")
    if props:
        bits.append(f"Visible props: {props}.")
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    arch = visual_place_name((spatial or {}).get("architecture"))
    if arch and arch.casefold() not in place.casefold():
        bits.append(arch.rstrip(".") + ".")
    if look and not _BAD.search(look):
        bits.append(f"{look}.")
    if aspect:
        bits.append(f"Framing: {aspect}.")
    bits.append("One clear image.")
    # Optional seed: only keep positive fragments that add place detail.
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 400:
        bits.insert(1, seed_text.rstrip(".") + ".")
    text = " ".join(b for b in bits if b)
    return re.sub(r"\s+", " ", text).strip()[:2200]


def compose_character_sheet_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    seed: str = "",
) -> str:
    """Positive one-person studio sheet for the image model."""
    del graph
    cfg = _cfg(cfg)
    name = str(
        cfg.get("character_name")
        or (cfg.get("character_names") or [None])[0]
        or cfg.get("character_id")
        or "the character"
    ).strip()
    costume = str(cfg.get("costume_lock") or "").strip()
    costume = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", costume, maxsplit=1)[0]
    costume = costume.strip(" ,;.—-")[:200]
    look = _style_look(cfg)
    aspect = _aspect_phrase(cfg, None)
    bits = [
        f"One person only: {name}, full or three-quarter body on a plain empty studio backdrop.",
        "Solid neutral background, identity and costume only.",
    ]
    if costume and not _BAD.search(costume):
        bits.append(f"Wearing {costume}.")
    if look and not _BAD.search(look):
        bits.append(f"{look}.")
    if aspect:
        bits.append(f"Framing: {aspect}.")
    bits.append("One clear image.")
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 300:
        bits.insert(1, seed_text.rstrip(".") + ".")
    text = " ".join(b for b in bits if b)
    return re.sub(r"\s+", " ", text).strip()[:2200]


def ensure_still_tool_prompt(
    prompt: str,
    *,
    role: str,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Legacy helper: rewrite lock essays into positive still prompts.

    Call paths (``call_image_model`` / handlers / director gate) no longer invoke
    this — durable user text and the leaf LLM prompt are authority. Kept for
    unit tests and optional offline cleanup.
    """
    cfg = _cfg(cfg)
    # Honor durable toolbar text if present (same authority as video).
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
            resolve_user_origin_prompt,
        )

        user = resolve_user_origin_prompt(cfg, "")
        if user:
            return user[:2200], ["still_user_authority"]
    except Exception:  # noqa: BLE001
        pass
    role_l = str(role or cfg.get("role") or "").lower()
    notes: list[str] = []
    text = str(prompt or "").strip()
    if role_l in {"scene"} or "scene" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_scene_specs_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_scene_plate")
        else:
            # Soft fill missing ToD / style as positive prose.
            pl = text.lower()
            tod = _tod_phrase(cfg)
            if tod and not any(w in pl for w in ("night", "dawn", "dusk", "morning", "evening", "daylight", "it is ")):
                if tod.lower() not in pl:
                    text = (text.rstrip(".") + ". " + tod).strip()
                    notes.append("still_cover_tod")
            look = _style_look(cfg)
            if look and not _style_is_covered(text, look):
                text = (text.rstrip(".") + f". {look}.").strip()
                notes.append("still_cover_style")
    elif role_l in {"character", "character_design"} or "character" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_character_sheet_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_character_sheet")
        else:
            look = _style_look(cfg)
            if look and not _style_is_covered(text, look):
                text = (text.rstrip(".") + f". {look}.").strip()
                notes.append("still_cover_style")
    return text[:2200], notes
