# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canvas node labels — story names from analysis / brief, never modality Image/Video N."""

from __future__ import annotations

import re
from typing import Any

_GENERIC_SHOT_TITLE = re.compile(
    r"(?i)^(shot|beat|clip|scene|keyframe|focus|untitled)\s*\d*$"
)
_LEADING_FILLER_WORDS = {
    # Structural request wrappers (any story) — not topic nouns.
    "a",
    "an",
    "the",
    "and",
    "then",
    "while",
    "as",
    "with",
    "from",
    "to",
    "of",
    "in",
    "on",
    "at",
    "i",
    "want",
    "video",
    "film",
    "clip",
    "make",
    "create",
    "please",
    "generate",
    "help",
    "me",
    "us",
    "my",
    "a",
    "short",
    "帮我",
    "请",
    "制作",
    "生成",
    "创建",
    "写一个",
    "做一个",
    "视频",
    "短片",
    "分镜",
}
_TRAILING_FILLER_WORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "with",
    "from",
    "as",
}


def _clean(text: object, *, limit: int = 72) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    raw = raw.strip(" []\"'")
    if not raw:
        return ""
    return raw[:limit].rstrip(" .,:;")


def _is_generic_shot_title(text: str) -> bool:
    return bool(_GENERIC_SHOT_TITLE.fullmatch((text or "").strip()))


def short_shot_phrase(text: object, *, max_words: int = 4) -> str:
    """Compress action/title to a 2–4 word canvas label (any language)."""
    cleaned = _clean(text, limit=80)
    if not cleaned or _is_generic_shot_title(cleaned):
        return ""
    # CJK / dense scripts: take a short character window.
    if re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", cleaned):
        return _clean(cleaned, limit=12)
    words = cleaned.split()
    while words and words[0].casefold().strip(".,") in _LEADING_FILLER_WORDS:
        words.pop(0)
    take = words[: max(2, min(max_words, len(words)))]
    if len(take) < 2 and words:
        take = words[: min(3, len(words))]
    while take and take[-1].casefold().strip(".,") in _TRAILING_FILLER_WORDS:
        take.pop()
    if len(take) < 2 and words:
        take = words[: min(3, len(words))]
    phrase = " ".join(take)
    if phrase.isascii() and phrase == phrase.lower():
        phrase = phrase.title()
    return _clean(phrase, limit=40)


def derive_story_name(
    *,
    analysis: dict[str, Any] | None = None,
    prompt: str = "",
    graph_title: str = "",
) -> str:
    """Pick a display story name from analysis / title / prompt (any language)."""
    meta = analysis if isinstance(analysis, dict) else {}
    for key in ("story_name", "film_title", "title", "project_title"):
        hit = _clean(meta.get(key), limit=64)
        if hit and hit.lower() not in {"untitled", "design", "video", "film"}:
            return hit
    title = _clean(graph_title, limit=64)
    if title and title.lower() not in {"untitled", "design"}:
        return title
    # First sentence / clause of the user prompt — no genre hardcodes.
    text = re.sub(r"\s+", " ", (prompt or "").strip())
    if not text:
        return "Untitled"
    # Drop leading "help me make a video" style wrappers when a later clause exists.
    parts = re.split(r"[。.!?\n]|[;；]", text)
    parts = [p.strip(" ,，") for p in parts if len(p.strip()) > 8]
    candidate = parts[0] if parts else text
    return _clean(candidate, limit=48) or "Untitled"


def derive_shot_name(shot: dict[str, Any] | None, *, fallback_index: int = 1) -> str:
    """2–4 word shot display name from title or action (any language)."""
    sh = shot if isinstance(shot, dict) else {}
    for key in ("title", "shot_title", "clip_name", "name"):
        hit = short_shot_phrase(sh.get(key), max_words=4)
        if hit:
            return hit
    action = short_shot_phrase(
        sh.get("action") or sh.get("keyframe_prompt") or sh.get("character_action"),
        max_words=4,
    )
    if action:
        return action
    return f"Shot {max(1, int(fallback_index or 1))}"


def label_brief(story_name: str) -> str:
    return f"Brief: {_clean(story_name, limit=64) or 'Untitled'}"


def label_storyboard(story_name: str) -> str:
    return f"Story Board: {_clean(story_name, limit=64) or 'Untitled'}"


def label_character(index: int, name: str) -> str:
    n = max(1, int(index or 1))
    return f"Character {n}: {_clean(name, limit=48) or f'Character {n}'}"


def label_scene(*, scene_number: int, scene_name: str) -> str:
    """Canvas label: Scene N: two-to-three-word scene name."""
    sn = max(1, int(scene_number or 1))
    desc = short_shot_phrase(scene_name, max_words=3) or _clean(scene_name, limit=40)
    if not desc or _is_generic_shot_title(desc):
        return f"Scene {sn}"
    return f"Scene {sn}: {desc}"


def label_shot(
    *,
    scene_number: int,
    shot_number: int,
    shot_name: str,
) -> str:
    sn = max(1, int(scene_number or 1))
    shn = max(1, int(shot_number or 1))
    desc = short_shot_phrase(shot_name, max_words=4) or _clean(shot_name, limit=40)
    if not desc or _is_generic_shot_title(desc):
        return f"Scene {sn}: Shot {shn}"
    return f"Scene {sn}: Shot {shn}: {desc}"


def label_clip(
    *,
    scene_number: int,
    clip_number: int,
    clip_name: str,
) -> str:
    """Shot label. The stored node id may still contain clip; the name is shot."""
    return label_shot(
        scene_number=scene_number,
        shot_number=clip_number,
        shot_name=clip_name,
    )


def label_compose(story_name: str) -> str:
    return f"Final Composed: {_clean(story_name, limit=64) or 'Untitled'}"


def is_semantic_canvas_label(label: str) -> bool:
    """True when label follows the Designer naming scheme (keep through normalize)."""
    text = (label or "").strip()
    if not text:
        return False
    prefixes = (
        "Brief:",
        "Story Board:",
        "Character ",
        "Scene ",
        "Final Composed:",
    )
    return any(text.startswith(p) for p in prefixes)
