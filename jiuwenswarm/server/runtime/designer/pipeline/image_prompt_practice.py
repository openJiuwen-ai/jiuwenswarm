# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Positive still prompts for Qwen / DashScope / MiniMax image backends.

Locks live on node config. The image API body is a concise positive description —
no LOCK banners, forbid lists, or worked examples. Domain-agnostic.
"""

from __future__ import annotations

import re
from typing import Any

from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
    current_content_language,
    join_sentences,
    list_separator,
    localized_lighting,
    localized_time_of_day,
    phrase,
    sentence,
    strip_phrase,
)

_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "exterior": "Exterior, with the ground and the open background.",
        "interior": "Interior of this place.",
        "tod_with_lighting": "It is {label}; {lighting}.",
        "tod": "It is {label}.",
        "empty_setting": "One empty setting.",
        "setting_is_empty": "The setting is empty.",
        "visible_props": "Visible props: {props}.",
        "one_clear_image": "One clear image.",
        "one_person": "One person only: {name}, full or three-quarter body on a plain empty studio backdrop.",
        "neutral_backdrop": "Solid neutral background, identity and costume only.",
        "wearing": "Wearing {costume}.",
        "the_character": "the character",
    },
    "zh": {
        "exterior": "室外，可见地面与开阔的背景。",
        "interior": "该地点的室内。",
        "tod_with_lighting": "时间是{label}；{lighting}。",
        "tod": "时间是{label}。",
        "empty_setting": "一个空旷的场景。",
        "setting_is_empty": "场景中空无一人。",
        "visible_props": "可见道具：{props}。",
        "one_clear_image": "画面清晰，单张图像。",
        "one_person": "画面中仅一人：{name}，全身或七分身，站在纯净空白的影棚背景前。",
        "neutral_backdrop": "纯色中性背景，只呈现人物身份与服装。",
        "wearing": "身穿{costume}。",
        "the_character": "该角色",
    },
}

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
    filler = {"across", "every", "feature", "coherent", "forms", "shapes", "rendering"}
    cues = {token for token in re.findall(r"[\w-]+", style) if len(token) > 4 and token not in filler}
    return bool(cues and any(token in body for token in cues))


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
    return re.split(r"[.。]", raw, maxsplit=1)[0].strip()[:160]


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
        if not text or any(p.search(text) for p in (_BAD, _PLACEHOLDER_PROP, _INSTRUCTION_PLACE)):
            continue
        if text not in out:
            out.append(text[:160])
    return out[:6]


def _enclosure_phrase(place: str, lang: str = "en") -> str:
    text = str(place or "")
    low = text.lower()
    if any(word in text or word in low for word in _EXTERIOR):
        return phrase(_PHRASES, lang, "exterior")
    if any(word in text or word in low for word in _INTERIOR):
        return phrase(_PHRASES, lang, "interior")
    return ""


def _tod_phrase(cfg: dict[str, Any], lang: str = "en") -> str:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    raw_label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = _visual_lighting(str((tod or {}).get("lighting") or bible.get("lighting") or ""))
    lighting = strip_phrase(localized_lighting(lighting, lang))
    if raw_label and raw_label.lower() != "unspecified":
        label = localized_time_of_day(raw_label, lang)
        if lighting and label.lower() in lighting.lower():
            return sentence(lighting, lang)
        if lighting:
            return phrase(_PHRASES, lang, "tod_with_lighting", label=label, lighting=lighting)
        return phrase(_PHRASES, lang, "tod", label=label)
    if lighting:
        return sentence(lighting, lang)
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
        # Landmarks carry the authored setting description. At graph-build
        # time scene_name/architecture are often still empty, and this is the
        # only place text available for the pre-run scene plate prompt — it
        # must not be dropped (see the "One empty setting." stub).
        (spatial or {}).get("landmarks"),
    ):
        place = visual_place_name(raw)
        if place:
            place = re.sub(r"(?i)\bset[_\s-]?\d+\s*[:：]\s*", "", place)
            return place.strip().rstrip("。.;；")
    return ""


def _props_phrase(cfg: dict[str, Any], lang: str = "en") -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    return list_separator(lang).join(visual_prop_list(bible.get("objects")))


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
    lang = current_content_language()
    place = _scene_phrase(cfg)
    tod = _tod_phrase(cfg, lang)
    props = _props_phrase(cfg, lang)
    look = _style_look(cfg)
    bits = [sentence(place, lang) if place else phrase(_PHRASES, lang, "empty_setting")]
    enclosure = _enclosure_phrase(place, lang)
    if enclosure:
        bits.append(enclosure)
    bits.append(phrase(_PHRASES, lang, "setting_is_empty"))
    if tod:
        bits.append(sentence(tod, lang))
    if props:
        bits.append(phrase(_PHRASES, lang, "visible_props", props=props))
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    arch = visual_place_name((spatial or {}).get("architecture"))
    if arch and arch.casefold() not in place.casefold():
        bits.append(sentence(arch, lang))
    if look and not _BAD.search(look):
        bits.append(sentence(look, lang))
    bits.append(phrase(_PHRASES, lang, "one_clear_image"))
    # Optional seed: only keep positive fragments that add place detail.
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 400:
        bits.insert(1, sentence(seed_text, lang))
    text = join_sentences(bits, lang)
    return re.sub(r"\s+", " ", text).strip()


def compose_character_sheet_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    seed: str = "",
) -> str:
    """Positive one-person studio sheet for the image model."""
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import wearable_phrase

    cfg = _cfg(cfg)
    lang = current_content_language()
    name = str(
        cfg.get("character_name")
        or (cfg.get("character_names") or [None])[0]
        or cfg.get("character_id")
        or phrase(_PHRASES, lang, "the_character")
    ).strip()
    costume = str(cfg.get("costume_lock") or "").strip()
    costume = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", costume, maxsplit=1)[0]
    costume = wearable_phrase(costume, name=name, lang=lang).strip(" ,;.—-")[:200]
    look = _style_look(cfg)
    bits = [
        phrase(_PHRASES, lang, "one_person", name=name),
        phrase(_PHRASES, lang, "neutral_backdrop"),
    ]
    if costume and not _BAD.search(costume):
        bits.append(phrase(_PHRASES, lang, "wearing", costume=costume))
    if look and not _BAD.search(look):
        bits.append(sentence(look, lang))
    bits.append(phrase(_PHRASES, lang, "one_clear_image"))
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 300:
        bits.insert(1, sentence(seed_text, lang))
    text = join_sentences(bits, lang)
    return re.sub(r"\s+", " ", text).strip()


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
            return user, ["image_user_authority"]
    except Exception:  # noqa: BLE001
        pass
    role_l = str(role or cfg.get("role") or "").lower()
    notes: list[str] = []
    text = str(prompt or "").strip()
    if role_l in {"scene"} or "scene" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_scene_specs_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_scene_specs")
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
    elif role_l in {"character"} or "character" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_character_sheet_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_character_sheet")
        else:
            look = _style_look(cfg)
            if look and not _style_is_covered(text, look):
                text = (text.rstrip(".") + f". {look}.").strip()
                notes.append("still_cover_style")
    return text, notes
