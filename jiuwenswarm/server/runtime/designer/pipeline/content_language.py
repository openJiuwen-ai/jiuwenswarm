# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authoring language for user-editable Designer text.

The content language follows the user's ``preferred_language`` setting. Briefs,
storyboard fields, character cards, and node prompts that the user can read and
edit are all authored in it. Spoken dialogue is a separate film lock
(``language_lock``) and may differ, e.g. a Chinese session for an English film.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.config import get_config

_PHRASE_PUNCT = " .。;；,，"


def current_content_language() -> str:
    """``en`` when preferred_language is English, otherwise ``zh`` (the product default)."""
    lang = str(get_config().get("preferred_language") or "zh").strip().lower()
    return "en" if lang == "en" else "zh"


def authoring_language_rule(lang: str) -> str:
    """System-prompt clause that keeps user-editable text in the content language."""
    name = "Chinese (中文)" if lang == "zh" else "English"
    return (
        f"OUTPUT LANGUAGE: content_language is {name}. Write every human-readable value "
        f"in {name} — brief prose, character names and descriptions, clothing_parts, "
        "style_lock look/medium, scene and place descriptions, props, lighting, camera, "
        "action, cast_actions, clip_prompt, start/end states, continuity notes, notes, "
        "issues, and every image or video prompt — even where these instructions, "
        "examples, upstream locks, or the approved brief use another language. Keep "
        "proper names exactly as the user wrote them. Only ids, enum values (such as "
        "clip_strategy, emotion, presence), JSON keys, ratios such as 16:9, timelines, "
        "and model reference tokens such as Image 1 or character1 stay as-is. Spoken "
        "lines follow language_lock, which may differ from content_language. "
    )


POSITIVE_VISUAL_RULE = (
    "Visual fields (character description, clothing_parts, scene/place, props, lighting, "
    "style look, camera, action, cast_actions, clip_prompt) describe only what is visible, "
    "in positive wording — no prohibitions, negations, or rule banners inside them. Put "
    "every prohibition in continuity_lock.forbid instead. "
)


_NODE_TASKS: dict[str, dict[str, str]] = {
    "en": {
        "compose": (
            "Concatenate the clips in shot order into one real, non-empty .mp4 with the "
            "ffmpeg_compose tool. Output the video file, not markdown."
        ),
        "execute": "Execute node {label}.",
    },
    "zh": {
        "compose": "使用 ffmpeg_compose 工具，按镜头顺序把各片段拼接成一个真实、非空的 .mp4 文件。输出视频文件，而不是 markdown。",
        "execute": "执行节点「{label}」。",
    },
}


def node_task(key: str, **values: Any) -> str:
    """Code-authored node task in the content language; the toolbar shows it when a node has no prompt."""
    return phrase(_NODE_TASKS, current_content_language(), key, **values)


def phrase(table: dict[str, dict[str, str]], lang: str, key: str, **values: Any) -> str:
    """Pick a template string for the content language and fill it."""
    entry = table[lang][key]
    return entry.format(**values) if values else entry


def strip_phrase(text: Any) -> str:
    """Trim surrounding whitespace and trailing sentence punctuation in either language."""
    return str(text or "").strip().rstrip(_PHRASE_PUNCT).lstrip()


def sentence(text: Any, lang: str) -> str:
    """Close a phrase with the content language's full stop."""
    body = strip_phrase(text)
    if not body:
        return ""
    return body + ("。" if lang == "zh" else ".")


def join_sentences(parts: list[str], lang: str) -> str:
    sep = "" if lang == "zh" else " "
    return sep.join(p for p in parts if p)


def list_separator(lang: str) -> str:
    return "、" if lang == "zh" else ", "


def clause_separator(lang: str) -> str:
    return "，" if lang == "zh" else ", "


_TIME_OF_DAY_ZH = {
    "night": "夜晚",
    "dusk": "黄昏",
    "dawn": "黎明",
    "morning": "早晨",
    "day": "白天",
    "evening": "傍晚",
}

# Lighting strings written by axis_locks.infer_time_of_day_lock (code, not the user).
_INFERRED_LIGHTING_ZH = (
    ("night lighting", "夜间光线，冷暗主光配合场景内光源"),
    ("dusk / golden-hour", "黄昏金色暖光，影子拉长"),
    ("dawn light", "黎明光线，地平线冷暖交织的微光"),
    ("morning daylight", "清晨日光，柔和清亮的主光"),
    ("daytime daylight", "白天日光，阳光方向稳定"),
    ("evening interior/exterior practicals", "傍晚的室内外实景光源"),
    ("motivated key light", "有明确光源依据、方向稳定的主光"),
)


def localized_time_of_day(label: str, lang: str) -> str:
    text = str(label or "").strip()
    if lang == "zh":
        return _TIME_OF_DAY_ZH.get(text.lower(), text)
    return text


def localized_lighting(lighting: str, lang: str) -> str:
    text = str(lighting or "").strip()
    if lang != "zh":
        return text
    low = text.lower()
    for prefix, zh in _INFERRED_LIGHTING_ZH:
        if low.startswith(prefix):
            return zh
    return text


_DIALOGUE_LANGUAGE_ZH = {
    "en": "英语",
    "zh": "中文",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "es": "西班牙语",
    "de": "德语",
    "ar": "阿拉伯语",
    "hi": "印地语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "it": "意大利语",
}


def dialogue_language_display(code: str, english_name: str, lang: str) -> str:
    """Spoken-language name rendered in the content language."""
    if lang != "zh":
        return english_name
    key = str(code or "").strip().lower().replace("_", "-").split("-")[0]
    return _DIALOGUE_LANGUAGE_ZH.get(key, english_name)
