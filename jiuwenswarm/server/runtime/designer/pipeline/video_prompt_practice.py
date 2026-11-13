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

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_KEY_INPUTS,
    PIPELINE_COMPOSE,
    PIPELINE_MUSIC,
    node_config,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
    current_content_language,
    dialogue_language_display,
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
        "while": "{first}, while {second}.",
        "says": '{who} says: "{line}".',
        "says_offscreen": '{who}, heard offscreen, says: "{line}".',
        "voice_says": 'A voice says: "{line}".',
        "audio": "Audio: {bits}.",
        "sound_diegetic_only": (
            "Sound: diegetic sound of this shot only — action sound and room tone; "
            "no music, no score, no underscore, no ambient music bed."
        ),
        "sound_cues_only": (
            "Sound: {cues} only; no music, no score, no underscore, no ambient music bed."
        ),
        "sound_silent": "Sound: silent — no dialogue, no effects, no music.",
        "sound_no_speech": "No one speaks: no dialogue, no monologue, no voiceover.",
        "dialogue_language": "Spoken dialogue is in {name}.",
        "tod_with_lighting": "It is {label}; {lighting}.",
        "tod": "It is {label}.",
        "continue": (
            "This shot opens already past earlier finished moments in the storyboard — "
            "same setting and faces continue from that authored start_state."
        ),
        "scene_as_in": "The scene is as in {scene_word}: {place}.",
    "scene_as_in_unnamed": "The scene is as shown in {scene_word}.",
        "scene_description": "Scene description: {desc}.",
        "props_in_view": "Props in view: {props}.",
        "headcount": "{count} people stay in frame together: {cast}.",
        "staging": "Staging, unchanged across the shots of this setting: {layout}.",
        "connected_still": "{word} is the connected still {label}, and that subject is visible in this shot.",
        "cast_references": "the cast references",
        "camera": "The camera {cam}.",
        "offscreen_heard": "{who}{wearing} is in the scene from {scene_word}, outside this camera frame, and is heard.",
        "offscreen_wearing": ", wearing {wardrobe},",
        "cast_from": "{who} from {image_word}",
        "cast_named": "{who}",
        "match_reference_look": "Faces, wardrobe and set match the attached reference images",
        "match_reference_look_with": (
            "Faces, wardrobe and set match the attached reference images — {look}"
        ),
        "cast_wearing": "wearing {wardrobe}",
        "group_variation": (
            "Within the group, each person differs in gesture, timing, and attention; "
            "no two share the same pose."
        ),
        "cast_seat": "{seat} in the scene from {scene_word}",
        "cast_in_scene": "in the scene from {scene_word} ({scene})",
        "cast_partial": "is in the scene from {scene_word}, partially visible",
        "cast_out_of_frame": "is in the scene from {scene_word}, outside this camera frame",
        "shot_brief": "Film shot {idx} only. Camera {camera}. Action: {action}.",
        "shot_brief_cast": "Focus cast on screen: {cast}.",
        "shot_brief_default_camera": "medium / eye-level",
    },
    "zh": {
        "while": "{first}，同时{second}。",
        "says": "{who}说：“{line}”。",
        "says_offscreen": "{who}在画外说：“{line}”。",
        "voice_says": "画外音说：“{line}”。",
        "audio": "音乐：{bits}。",
        "sound_diegetic_only": "声音：仅本镜头的现场音（动作声与环境底噪）；不要配乐、不要背景音乐、不要氛围音床。",
        "sound_cues_only": "声音：仅{cues}；不要配乐、不要背景音乐、不要氛围音床。",
        "sound_silent": "声音：静音，无对白、无音效、无音乐。",
        "sound_no_speech": "人物没有台词、对白或独白。",
        "dialogue_language": "对白使用{name}。",
        "tod_with_lighting": "时间是{label}；{lighting}。",
        "tod": "时间是{label}。",
        "continue": "本镜头开始时，分镜中此前已完成的情节不再重演，同一场景和人物从既定的起始状态继续。",
        "scene_as_in": "场景如 {scene_word} 所示：{place}。",
    "scene_as_in_unnamed": "场景如 {scene_word} 所示。",
        "scene_description": "场景描述：{desc}。",
        "props_in_view": "画面中的道具：{props}。",
        "headcount": "画面内共 {count} 人：{cast}。",
        "staging": "本场景的座位与站位（同一场景的各镜头保持一致）：{layout}。",
        "connected_still": "{word} 是相连的静帧「{label}」，该主体在本镜头中可见。",
        "cast_references": "角色参考图",
        "camera": "镜头：{cam}。",
        "offscreen_heard": "{who}{wearing}位于 {scene_word} 的场景中，在本镜头画框之外，只闻其声。",
        "offscreen_wearing": "（身穿{wardrobe}）",
        "cast_from": "{who}（{image_word}）",
        "cast_named": "{who}",
        "match_reference_look": "面部、服装与场景与所附参考图一致",
        "match_reference_look_with": "面部、服装与场景与所附参考图一致——{look}",
        "cast_wearing": "身穿{wardrobe}",
        "group_variation": "组内每个人的手势、节奏与注意力各不相同，没有两个人是同一姿势。",
        "cast_seat": "{seat}，位于 {scene_word} 的场景中",
        "cast_in_scene": "位于 {scene_word} 的场景中（{scene}）",
        "cast_partial": "位于 {scene_word} 的场景中，部分可见",
        "cast_out_of_frame": "位于 {scene_word} 的场景中，在本镜头画框之外",
        "shot_brief": "仅拍第{idx}镜。镜头：{camera}。动作：{action}。",
        "shot_brief_cast": "出镜角色：{cast}。",
        "shot_brief_default_camera": "中景平视",
    },
}

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


def shot_brief_prompt(
    idx: int,
    *,
    camera: str,
    action: str,
    cast_names: list[str] | None = None,
    lang: str | None = None,
) -> str:
    """User-visible clip prompt seeded from the storyboard row; locks stay in config."""
    lang = lang or current_content_language()
    body = phrase(
        _PHRASES,
        lang,
        "shot_brief",
        idx=idx,
        camera=strip_phrase(camera) or phrase(_PHRASES, lang, "shot_brief_default_camera"),
        action=strip_phrase(action),
    )
    cast = list_separator(lang).join(str(n).strip() for n in (cast_names or []) if str(n).strip())
    if cast:
        body = join_sentences([body, phrase(_PHRASES, lang, "shot_brief_cast", cast=cast)], lang)
    return body


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


def _scene_name(cfg: dict[str, Any], lang: str = "en") -> str:
    """Best available place label, or "" when the shot names no place.

    Never asserts a place the storyboard did not provide: the former fallback
    wrote "the empty room" onto outdoor and aerial shots. Omitting the noun is
    always safer than naming the wrong one.
    """
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    for key in ("scene_name", "place", "description"):
        place = str(bible.get(key) or "").strip()
        if not place:
            continue
        place = re.split(r"[.。]", place, maxsplit=1)[0].strip()[:80]
        if not place:
            continue
        # A short label is a place name; a long clause is prose, not a name.
        # CJK writes without spaces, so cap characters as well as words.
        if len(place.split()) <= 8 and len(place) <= 40:
            return place
    setting = str(cfg.get("setting_id") or "").strip()
    if setting and not setting.lower().startswith("set_"):
        return setting
    return ""


def _style_phrase(cfg: dict[str, Any]) -> str:
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or style.get("medium") or "").strip()
    look = re.split(r"\s+[—–-]\s+|\bnever\b|\bno style\b", look, maxsplit=1, flags=re.I)[0]
    look = look.strip(" .;")
    return look


# Serialization labels render_storyboard_table uses to append per-cast actions
# onto the storyboard row's action cell. The per-cast values already render as
# their own cast sentences, so the leading action sentence drops the appendix.
_DOING_BLOCK_LABELS = (" Doing: ", " 动作：")


def _storyboard_action_text(cfg: dict[str, Any], action: str = "") -> str:
    """The storyboard's own action text for this shot, verbatim (directives rejected)."""
    text = str(action or cfg.get("clip_action") or cfg.get("character_action") or "").strip()
    for label in _DOING_BLOCK_LABELS:
        idx = text.find(label)
        if idx > 0:
            text = text[:idx]
    text = text.strip()
    if not text or _BAD_DIRECTIVE.search(text):
        return ""
    return text


def _speech_lines(
    cfg: dict[str, Any],
    graph: dict[str, Any] | None,
    names: list[str],
    lang: str = "en",
) -> list[str]:
    spoken: list[str] = []
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    on_screen = {name.casefold() for name in names}
    if by_char:
        for key, value in by_char.items():
            line = str(value or "").strip().strip('"“”')
            if not line:
                continue
            who = _name_for_id(str(key), graph) or str(key)
            if who.casefold() in on_screen:
                spoken.append(phrase(_PHRASES, lang, "says", who=who, line=line))
            else:
                spoken.append(phrase(_PHRASES, lang, "says_offscreen", who=who, line=line))
        return spoken
    line = str(cfg.get("speech_line") or "").strip().strip('"“”')
    if not line:
        return []
    if names:
        return [phrase(_PHRASES, lang, "says", who=names[0], line=line)]
    return [phrase(_PHRASES, lang, "voice_says", line=line)]


def film_scored_by_music_node(graph: dict[str, Any] | None) -> bool:
    """A music node feeding compose owns the film score, so no clip may score it."""
    nodes = [n for n in ((graph or {}).get("nodes") or []) if isinstance(n, dict)]
    music = {str(n.get("id") or "") for n in nodes if node_pipeline(n) == PIPELINE_MUSIC}
    music.discard("")
    if not music:
        return False
    compose = {str(n.get("id") or "") for n in nodes if node_pipeline(n) == PIPELINE_COMPOSE}
    for edge in (graph or {}).get("edges") or []:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source") or "") in music and str(edge.get("target") or "") in compose:
            return True
    return any(
        music.intersection(str(item) for item in (node_config(n).get(CONFIG_KEY_INPUTS) or []))
        for n in nodes
        if node_pipeline(n) == PIPELINE_COMPOSE
    )


def _music_line(cfg: dict[str, Any], lang: str = "en", graph: dict[str, Any] | None = None) -> str:
    """A clip never describes a score.

    The film's music is generated once by the music node and mixed in at compose,
    so a clip that asked for music made the cut jump and doubled the score. The
    phrase table and the ``clip_scores_film_music`` validator stay in place to
    catch a legacy prompt that still carries an in-clip score.
    """
    return ""


def _has_authored_speech(cfg: dict[str, Any]) -> bool:
    """True when this shot carries words someone is meant to say."""
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    if any(str(value or "").strip().strip('"“”') for value in by_char.values()):
        return True
    return bool(str(cfg.get("speech_line") or "").strip().strip('"“”'))


def _sound_line(cfg: dict[str, Any], lang: str = "en") -> str:
    """The one audio direction every clip gets: diegetic sound, never a score.

    A native-audio video model fills an unspecified track on its own, and it
    reaches for a music bed first — so a clip prompt that says nothing about
    sound ends up scoring itself. When the film's music node then fails, that
    improvised bed is the only music in the cut and reads as BGM that leaked
    into the clips. Naming the negative explicitly keeps the track diegetic.
    The same model also invents dialogue when a shot has no authored lines, so
    a non-silent shot with no speech says so outright.
    """
    if str(cfg.get("audio_policy") or "") == "silent":
        return phrase(_PHRASES, lang, "sound_silent")
    cues = [
        str(c).strip()
        for c in (cfg.get("sfx") if isinstance(cfg.get("sfx"), list) else [])
        if str(c).strip() and not _BAD_DIRECTIVE.search(str(c))
    ][:3]
    if cues:
        line = phrase(_PHRASES, lang, "sound_cues_only", cues=list_separator(lang).join(cues))
    else:
        line = phrase(_PHRASES, lang, "sound_diegetic_only")
    if not _has_authored_speech(cfg):
        line = f"{line} {phrase(_PHRASES, lang, 'sound_no_speech')}"
    return line


def exited_ids(cfg: dict[str, Any] | None) -> list[str]:
    """Cast that left this setting and stays gone until storyboard returns them.

    Does NOT include ordinary offscreen (still in-setting) — only exit marks.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    raw = (
        list(cfg.get("exited_ids") or [])
        + list(cfg.get("exiting_character_ids") or [])
        + list(cfg.get("exiting") or [])
        + list(occ.get("exited") or [])
    )
    return list(dict.fromkeys([str(x).strip() for x in raw if str(x).strip()]))


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


def _speaker_names(cfg: dict[str, Any], graph: dict[str, Any] | None) -> set[str]:
    by_character = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    return {_name_for_id(str(key), graph).casefold() for key in by_character if str(key).strip()}


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
        if not label:
            continue
        folded = label.casefold()
        if any(folded in names for names in (on, gone, gone_names, {x.casefold() for x in out})):
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


def _language_sentence(cfg: dict[str, Any], content_lang: str = "en") -> str:
    """Positive dialogue-language cue — only when this clip has speech."""
    if not _has_spoken_dialogue(cfg):
        return ""
    lang = str(cfg.get("language_lock") or "").strip()
    if not lang:
        return ""
    name = _language_display(lang)
    if not name or _BAD_DIRECTIVE.search(name):
        return ""
    name = dialogue_language_display(lang, name, content_lang)
    return phrase(_PHRASES, content_lang, "dialogue_language", name=name)


_PLANNER_NOTE_MARKERS = (
    "do not",
    "don't",
    "never",
    "forbid",
    "across same-setting shots",
    "across same setting shots",
    "no relight mid-scene",
)


def _visual_only(text: str) -> str:
    """Drop planner instructions from a lighting phrase; keep the light an image can show."""
    kept: list[str] = []
    for clause in str(text or "").replace("；", ";").split(";"):
        low = clause.casefold()
        cuts = [low.find(marker) for marker in _PLANNER_NOTE_MARKERS if low.find(marker) >= 0]
        if cuts:
            head = clause[: min(cuts)].strip(" ,.")
            if head:
                kept.append(head)
            continue
        kept.append(clause.strip())
    return "; ".join(part for part in kept if part).strip(" ,;.—-")


def _time_of_day_payload(cfg: dict[str, Any]) -> dict[str, str]:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = _visual_only(
        _scrub_lock_phrase(str((tod or {}).get("lighting") or bible.get("lighting") or ""))
    )
    return {"time_of_day": label, "lighting": lighting}


def _time_of_day_sentence(cfg: dict[str, Any], lang: str = "en") -> str:
    """Positive time-of-day / lighting continuity — no LOCK banners."""
    payload = _time_of_day_payload(cfg)
    raw_label = payload.get("time_of_day") or ""
    lighting = strip_phrase(localized_lighting(payload.get("lighting") or "", lang))
    if raw_label and raw_label.lower() != "unspecified":
        label = localized_time_of_day(raw_label, lang)
        if lighting:
            if label.lower() in lighting.lower():
                return sentence(lighting, lang)
            return phrase(_PHRASES, lang, "tod_with_lighting", label=label, lighting=lighting)
        return phrase(_PHRASES, lang, "tod", label=label)
    if lighting:
        return sentence(lighting, lang)
    return ""


def _tod_covered_in_text(text: str, cfg: dict[str, Any], lang: str = "en") -> bool:
    pl = str(text or "").lower()
    payload = _time_of_day_payload(cfg)
    label = (payload.get("time_of_day") or "").lower()
    if label and label != "unspecified":
        if label in pl:
            return True
        shown = localized_time_of_day(label, lang).lower()
        if shown and shown in pl:
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
    The full cast bible stays on config + stills, not in Wan prose.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    content_lang = current_content_language()
    text = str(prompt or "").strip()
    pl = text.lower()
    notes: list[str] = []
    additions: list[str] = []

    lang_sent = _language_sentence(cfg, content_lang)
    if lang_sent:
        lang = str(cfg.get("language_lock") or "").strip()
        display = _language_display(lang).lower()
        shown = dialogue_language_display(lang, display, content_lang)
        mentioned = "spoken dialogue" in pl or "对白" in pl or (
            display and re.search(rf"(?i)\b{re.escape(display)}\b", pl)
        ) or (shown != display and shown in pl)
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

    tod_sent = _time_of_day_sentence(cfg, content_lang)
    if tod_sent and not _tod_covered_in_text(text, cfg, content_lang):
        additions.append(tod_sent)
        notes.append("story_cover_time_of_day")

    look = _style_phrase(cfg)
    if look and not _BAD_DIRECTIVE.search(look):
        look_l = look.lower()
        # Soft style already at end of compose — only fill if wholly missing.
        if look_l not in pl and not any(
            token in pl for token in look_l.replace("-", " ").split() if len(token) > 4
        ):
            additions.append(sentence(look, content_lang))
            notes.append("story_cover_style")

    # Crowd disposition as positive prose (never CROWD LOCK banners).
    # The agent's own hold text is authoritative; we only avoid repeating the
    # exact same sentence, so nothing here classifies prose by keyword.
    crowd = _ensure_crowd_state(cfg)
    hold = _scrub_lock_phrase(str(crowd.get("hold") or ""))
    if hold and hold.lower() not in pl and not _BAD_DIRECTIVE.search(hold):
        additions.append(sentence(hold, content_lang))
        notes.append("story_cover_crowd_state")

    # Opening holds when prior shots exist but prose omitted them.
    for hold_line in (cfg.get("pose_holds") or [])[:3]:
        text_h = str(hold_line or "").strip()
        if not text_h or _BAD_DIRECTIVE.search(text_h):
            continue
        key = text_h.lower()[:40]
        if key and key not in pl:
            additions.append(sentence(text_h, content_lang))
            notes.append("story_cover_pose_hold")
            break

    if not additions:
        return text[:max_chars], notes
    merged = join_sentences([text.rstrip(), join_sentences(additions, content_lang)], content_lang).strip()
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


def _continue_line(cfg: dict[str, Any], lang: str = "en") -> str:
    """Positive continue from THIS row's start_state — not prior Wan text."""
    start = cfg.get("start_state") if isinstance(cfg.get("start_state"), dict) else {}
    if start.get("pose") or start.get("facing") or start.get("seats"):
        return ""  # start_state lines cover opening; no generic prior-clip sentence
    if cfg.get("pose_holds") or cfg.get("beat_done") or cfg.get("already_done"):
        return phrase(_PHRASES, lang, "continue")
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


def _continuity_story_lines(cfg: dict[str, Any], lang: str = "en") -> list[str]:
    """Opening holds from storyboard start_state + crowd + continue cue."""
    lines: list[str] = []
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
            start_end_story_lines,
        )

        for line in start_end_story_lines(cfg, lang):
            text = sentence(line, lang)
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
            holds = pose_holds_from_events(events)
            if holds:
                cfg["pose_holds"] = holds
        except Exception:  # noqa: BLE001
            holds = []
    prior_action = str(cfg.get("previous_clip_action") or "")
    this_action = str(cfg.get("clip_action") or cfg.get("action") or "")
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
            _is_action_restatement,
        )
    except Exception:  # noqa: BLE001
        _is_action_restatement = None  # type: ignore[assignment]
    start = cfg.get("start_state") if isinstance(cfg.get("start_state"), dict) else {}
    start_pose = strip_phrase(str(start.get("pose") or ""))
    for hold in holds[:4]:
        text = sentence(hold, lang)
        if _is_action_restatement and _is_action_restatement(text, prior_action, this_action):
            continue
        if start_pose and start_pose in text and any(start_pose in line for line in lines):
            continue
        if text and not _BAD_DIRECTIVE.search(text) and text not in lines:
            lines.append(text)

    crowd = _ensure_crowd_state(cfg)
    hold = _scrub_lock_phrase(str(crowd.get("hold") or ""))
    if hold and not _BAD_DIRECTIVE.search(hold):
        lines.append(sentence(hold, lang))

    cont = _continue_line(cfg, lang)
    if cont:
        lines.append(cont)
    return lines


def _staging_lines(
    cfg: dict[str, Any],
    graph: dict[str, Any] | None,
    tokens: list[str],
    lang: str = "en",
) -> list[str]:
    """Head count and setting geometry every shot in the setting has to reuse.

    The storyboard states who sits/stands where once per setting; a shot that got
    no structured blocking would otherwise let the video model re-invent the
    arrangement, moving people between shots of the same scene.
    """
    if len(tokens) < 2:
        return []
    names = [_name_for_id(token, graph) or str(token) for token in tokens]
    if any(_cast_form_for(token, name, graph) == "group" for token, name in zip(tokens, names)):
        # A group reference renders a varying crowd: a fixed head count would fight it.
        return []
    out = [phrase(_PHRASES, lang, "headcount", count=len(names), cast=list_separator(lang).join(names))]
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    if seats:
        # Per-cast seat clauses already carry the placement.
        return out
    from jiuwenswarm.server.runtime.designer.pipeline.positioning_lock import (
        stamp_lock,
    )

    token_names = {
        token: (_name_for_id(token, graph) or str(token)) for token in tokens
    }
    lock = str(cfg.get("positioning_lock_text") or "").strip() or stamp_lock(
        cfg, names=token_names, lang=lang, visible=list(tokens)
    )
    if lock and not _BAD_DIRECTIVE.search(lock):
        held = strip_phrase(_scrub_lock_phrase(lock)).rstrip(".;\u3002\uff1b")
        out.append(held + ("." if lang == "en" else "\u3002"))
        return out
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    layout = strip_phrase(_scrub_lock_phrase(str(spatial.get("layout") or "").strip()))
    if layout and not _BAD_DIRECTIVE.search(layout):
        out.append(phrase(_PHRASES, lang, "staging", layout=layout))
    return out


def _costume_for_setting(token: str, name: str, cfg: dict[str, Any]) -> str:
    """Per-scene wardrobe so a character can change clothes between settings."""
    table = cfg.get("costume_by_setting")
    if not isinstance(table, dict) or not table:
        return ""
    setting = str(cfg.get("scene_node_id") or cfg.get("setting_id") or "").strip()
    if not setting:
        return ""
    entry = table.get(setting)
    if not isinstance(entry, dict):
        return ""
    return str(entry.get(token) or entry.get(name) or "").strip()


_SLOT_HEADS = frozenset(
    {"top", "bottom", "outfit", "footwear", "foot wear", "outerwear", "outer wear", "underwear", "under wear", "accessory", "accessories"}
)


def _is_slot_chunk(chunk: str) -> bool:
    """True for a garment slot chunk such as ``bottom=navy trousers``."""
    head, sep, _ = str(chunk or "").partition("=")
    return bool(sep) and " ".join(head.split()).casefold() in _SLOT_HEADS


def _costume_blocks(lock: str) -> list[tuple[str, str]]:
    """Split "Name: top=…; bottom=…; Other: …" into [(name, "top=…; bottom=…")].

    Garment slots are separated by the same semicolon that separates cast
    members, so a naive split hands a character a single garment. A chunk that
    names no character is only the previous character's next garment slot when
    it is labelled (``bottom=…``); any other stray garment belongs to nobody and
    is kept under the empty name so it can never be read as their wardrobe.
    """
    blocks: list[tuple[str, list[str]]] = []
    chunks = str(lock or "").replace("\uff1b", ";").replace("\n", ";").split(";")
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        colons = [at for at in (chunk.find(":"), chunk.find("\uff1a")) if at >= 0]
        colon = min(colons) if colons else -1
        equals = chunk.find("=")
        names_a_character = 0 < colon <= 40 and (equals < 0 or colon < equals)
        if names_a_character:
            blocks.append((chunk[:colon].strip(), [chunk[colon + 1 :].strip()]))
        elif blocks and _is_slot_chunk(chunk):
            blocks[-1][1].append(chunk)
        elif blocks:
            blocks.append(("", [chunk]))
        else:
            return []
    return [(name, "; ".join(part for part in parts if part)) for name, parts in blocks]


def _id_for_name(name: str, graph: dict[str, Any] | None) -> str:
    """Character id that owns a cast name (the composer may carry names only)."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return ""
    for char in _analysis(graph).get("characters") or []:
        if not isinstance(char, dict):
            continue
        cname = str(char.get("name") or "").strip().casefold()
        if cname and cname == wanted:
            return str(char.get("id") or "").strip()
    return ""


def _own_slots(token: str, name: str, graph: dict[str, Any] | None) -> str:
    """This character's own garment slots as ``top=…; bottom=…`` (never another's)."""
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        format_clothing_slots,
        normalize_clothing_parts,
    )

    wanted_id = str(token or "").strip()
    wanted = str(name or "").strip().casefold()
    for char in _analysis(graph).get("characters") or []:
        if not isinstance(char, dict):
            continue
        cid = str(char.get("id") or "").strip()
        cname = str(char.get("name") or "").strip().casefold()
        if not (cid == wanted_id or (cname and cname == wanted)):
            continue
        parts = normalize_clothing_parts(char.get("clothing_parts"))
        if parts:
            return format_clothing_slots(parts, fallback="")
        return ""
    return ""


def _wardrobe_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None, lang: str = "en") -> str:
    """Short wearable phrase for one cast member — from costume_lock / analysis."""
    name = _name_for_id(token, graph) or str(token or "").strip()
    if not name:
        return ""
    raw = _costume_for_setting(token, name, cfg)
    if not raw:
        # Per-id map if present. Tokens are usually cast names, while the map is
        # keyed by character id, so bridge both ways before giving up on it.
        by_id = cfg.get("costume_by_id") if isinstance(cfg.get("costume_by_id"), dict) else {}
        cid = str(token or "").strip() if str(token or "").startswith("char_") else _id_for_name(name, graph)
        raw = str(by_id.get(token) or by_id.get(name) or by_id.get(cid) or "").strip()
    if not raw:
        # The identity plate is the source of truth: a costume edited on the
        # character node outranks the lock stamped when the graph was authored.
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            spec_costume_by_character,
        )

        by_plate = spec_costume_by_character(graph)
        raw = str(
            by_plate.get(token)
            or by_plate.get(name)
            or by_plate.get(_id_for_name(name, graph))
            or ""
        ).strip()
    if not raw:
        # This character's own garment slots. Reading them keeps one cast
        # member's clothes out of another's sentence when the shared lock is
        # malformed or already stale.
        raw = _own_slots(token, name, graph)
    if not raw:
        lock = str(cfg.get("costume_lock") or "").strip()
        # "Mother: top=…; bottom=…; Young Child: pale yellow…"
        blocks = _costume_blocks(lock)
        for who, block in blocks:
            if who.casefold() == name.casefold() or who == token:
                raw = block
                break
        on_screen = on_screen_names(cfg, graph)
        if not raw and len(on_screen) == 1 and on_screen[0].casefold() == name.casefold():
            # The lock can speak for the one face on screen — but only for that
            # face, and never through a garment chunk that lost its name.
            named = [block for who, block in blocks if who]
            raw = "; ".join(named) if named else ""
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
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import wearable_phrase

    return _fit_wardrobe(wearable_phrase(raw, name=name, lang=lang))


_WARDROBE_LIMIT = 220


def _fit_wardrobe(text: str, limit: int = _WARDROBE_LIMIT) -> str:
    """Trim a garment phrase at a clause boundary instead of mid-word."""
    value = ", ".join(part.strip() for part in str(text or "").split(",") if part.strip())
    value = value.strip(" ,\uff0c;\uff1b")
    if len(value) <= limit:
        return value
    cut = value[:limit]
    for separator in (", ", "; ", "，", "；"):
        at = cut.rfind(separator)
        if at >= limit // 2:
            return cut[:at].strip(" ,，;；")
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit // 2 else cut).strip(" ,，;；")


_SEAT_ZONES: dict[str, dict[str, str]] = {
    "en": {
        "center": "center frame",
        "left": "frame left",
        "right": "frame right",
        "foreground": "the foreground",
        "background": "the background",
    },
    "zh": {
        "center": "画面中央",
        "left": "画面左侧",
        "right": "画面右侧",
        "foreground": "前景",
        "background": "背景",
    },
}
_SNAKE_TOKEN = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")


def _seat_text(value: Any) -> str:
    """Authored seat prose; snake_case placeholders and key=value lock syntax stay in config."""
    text = _scrub_lock_phrase(value) if isinstance(value, str) else ""
    if not text or "=" in text or _SNAKE_TOKEN.fullmatch(text):
        return ""
    return text


def _seat_anchor_for(token: str, name: str, cfg: dict[str, Any], graph: dict[str, Any] | None) -> dict[str, Any]:
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    anchor = seats.get(token) if isinstance(seats.get(token), dict) else None
    if anchor is None:
        for key, value in seats.items():
            if isinstance(value, dict) and _name_for_id(str(key), graph).casefold() == name.casefold():
                return value
    if isinstance(anchor, dict):
        return anchor
    blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
    for pos in blocking.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        who = str(pos.get("name") or _name_for_id(str(pos.get("character_id") or ""), graph) or "")
        if str(pos.get("character_id") or "") == token or who.casefold() == name.casefold():
            # Blocking pose mirrors cast_actions, which the cast sentence already states.
            return {k: v for k, v in pos.items() if k != "pose"}
    return {}


def _seat_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None, lang: str = "en") -> str:
    """Where one cast member stands, rendered from structured seats; the raw positioning lock stays in config."""
    name = _name_for_id(token, graph) or str(token or "").strip()
    anchor = _seat_anchor_for(token, name, cfg, graph)
    if not anchor:
        return ""
    zh = lang == "zh"
    bits: list[str] = []
    place = _seat_text(anchor.get("pose")) or _seat_text(anchor.get("place")) or _seat_text(anchor.get("seat"))
    zone_key = str(anchor.get("zone") or "").strip().lower()
    zone = _SEAT_ZONES[lang].get(zone_key) or _seat_text(anchor.get("zone"))
    if place:
        bits.append(place)
    elif zone:
        bits.append(f"在{zone}" if zh else f"at {zone}")
    landmark = _seat_text(anchor.get("landmark"))
    if not landmark:
        blocking = cfg.get("blocking") if isinstance(cfg.get("blocking"), dict) else {}
        landmark = _seat_text(blocking.get("landmark"))
    if landmark:
        if zh:
            bits.append(f"靠近{landmark}")
        else:
            bits.append(f"near {landmark}" if landmark.lower().startswith("the ") else f"near the {landmark}")
    screen = _seat_text(anchor.get("screen"))
    if screen:
        bits.append(screen)

    def _ref_name(value: Any) -> str:
        """Resolve a declared cast ref (id or name) to a display name."""
        raw = str(value or "").strip()
        if not raw:
            return ""
        for char in _analysis(graph).get("characters") or []:
            if not isinstance(char, dict):
                continue
            if str(char.get("id") or "") == raw or str(char.get("name") or "") == raw:
                return str(char.get("name") or "").strip()
        # Unresolvable snake_case lock tokens are config, not prose.
        return "" if _SNAKE_TOKEN.fullmatch(raw) else raw

    gaze = _ref_name(anchor.get("looks_at"))
    if gaze and gaze.casefold() != name.casefold():
        bits.append(f"看着{gaze}" if zh else f"looking at {gaze}")
    addressing = _ref_name(anchor.get("talks_to"))
    if addressing and addressing.casefold() != name.casefold():
        bits.append(f"正对{addressing}说话" if zh else f"addressing {addressing}")
    return ("，" if zh else ", ").join(bits)


def _action_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None, fallback: str) -> str:
    """This cast member's authored action clause, verbatim.

    Empty when the storyboard action text already covers it (the action
    sentence leads the cast lines). The clause is never re-conjugated:
    gluing "is"/"正在" onto an unknown verb form produced "is sits" and
    "正在seated" macaronics.
    """
    actions = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}
    name = _name_for_id(token, graph) or str(token or "").strip()
    beat = _storyboard_action_text(cfg, fallback).rstrip(".")

    def _raw_doing(line: str) -> str:
        text = str(line or "").strip().rstrip(".")
        if not text or _BAD_DIRECTIVE.search(text):
            return ""
        if beat and text.casefold() == beat.casefold():
            return ""
        return text[:240]

    for key, value in actions.items():
        if str(key) == token or _name_for_id(str(key), graph).casefold() == name.casefold():
            return _raw_doing(str(value or ""))
    # No own entry: prefer this character's clause inside a shared "A ... while B ..." beat.
    if beat and name and " while " in beat.lower():
        for part in re.split(r"(?i)\s+while\s+", beat):
            if name.casefold() in part.casefold():
                cleaned = _raw_doing(part)
                if cleaned:
                    return cleaned
    return ""


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


def _cast_form_for(token: str, name: str, graph: dict[str, Any] | None) -> str:
    """The character's declared cast_form (``group`` / ``single``), else ''."""
    for char in _analysis(graph).get("characters") or []:
        if not isinstance(char, dict):
            continue
        cid = str(char.get("id") or "")
        cname = str(char.get("name") or "")
        name_matches = bool(name) and cname.casefold() == name.casefold()
        if cid == token or cname == token or name_matches:
            return str(char.get("cast_form") or "").strip().lower()
    return ""


def _look_sentence(cfg: dict[str, Any], lang: str, *, has_refs: bool) -> str:
    """Style sentence: anchored on the attached refs when present, else the style words."""
    look = _style_phrase(cfg)
    if has_refs:
        if look and len(look) <= 90 and not _BAD_DIRECTIVE.search(look):
            return phrase(_PHRASES, lang, "match_reference_look_with", look=look)
        return sentence(phrase(_PHRASES, lang, "match_reference_look"), lang)
    if look and not _BAD_DIRECTIVE.search(look):
        return sentence(look, lang)
    return ""


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
    lang: str = "en",
    group: bool = False,
) -> str:
    """One cast member in positive story form (no negatives, no genre examples).

    The authored action clause follows verbatim as its own sentence — never
    glued onto an auxiliary verb whose tense would corrupt it.
    """
    zh = lang == "zh"
    if image_word:
        bits = [phrase(_PHRASES, lang, "cast_from", who=who, image_word=image_word)]
    else:
        # No plate attached to this call: name the face, never invent an Image N.
        bits = [phrase(_PHRASES, lang, "cast_named", who=who)]
    if wardrobe:
        bits.append(phrase(_PHRASES, lang, "cast_wearing", wardrobe=wardrobe))
    if visibility == "full":
        if seat:
            bits.append(phrase(_PHRASES, lang, "cast_seat", seat=seat, scene_word=scene_word))
        else:
            bits.append(phrase(_PHRASES, lang, "cast_in_scene", scene_word=scene_word, scene=scene))
    elif visibility == "partial":
        bits.append(phrase(_PHRASES, lang, "cast_partial", scene_word=scene_word))
    else:
        # In-setting but out of this frame (not exited). Positive camera framing only.
        bits.append(phrase(_PHRASES, lang, "cast_out_of_frame", scene_word=scene_word))
    text = ("，" if zh else ", ").join(bits)
    identity = text if text.endswith((".", "。")) else sentence(text, lang)
    out = identity
    if doing:
        if who and who.casefold() not in doing.casefold():
            # Name binds the clause; a CJK clause takes no space, Latin does.
            sep = "" if doing[:1] and "一" <= doing[0] <= "鿿" else " "
            doing = f"{who}{sep}{doing}"
        out = identity + ("" if zh else " ") + sentence(doing, lang)
    if group and visibility == "full":
        # A group plate holds distinct people; the shot must not read as one
        # synchronized body, so the variation clause rides with the group.
        out += ("" if zh else " ") + sentence(phrase(_PHRASES, lang, "group_variation"), lang)
    return out


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
    lang = current_content_language()
    zh = lang == "zh"
    place = _scene_name(cfg, lang)
    extra_labels = [str(item).strip() for item in (extra_image_labels or []) if str(item).strip()]
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        resolve_reference_slots,
    )

    binding = cfg.get("reference_binding") if isinstance(cfg.get("reference_binding"), dict) else {}
    if binding:
        cast_slots = binding.get("cast") if isinstance(binding.get("cast"), dict) else {}
        unbound = {str(x) for x in (binding.get("unbound") or [])}
        extra_slots = [s for s in (binding.get("extra") or []) if isinstance(s, int) and s > 0]
        if len(extra_slots) != len(extra_labels):
            # No provenance for the stills: keep the positional fallback.
            extra_slots = [len(tokens) + 1 + i for i in range(len(extra_labels))]
        scene_slot = binding.get("scene") if isinstance(binding.get("scene"), int) else 0
    else:
        # No attached-reference report (prompt built before the clips run): predict
        # the slots from the same cap the call applies, never from raw counts.
        slots = resolve_reference_slots(len(tokens), len(extra_labels))
        cast_slots = {str(token): pos for token, pos in zip(tokens, slots["cast"])}
        unbound = {str(token) for token in tokens if str(token) not in cast_slots}
        extra_slots = list(slots["extra"])
        scene_slot = int(slots["scene"])
    has_refs = bool(cast_slots or extra_slots)
    scene_index = scene_slot or 1
    scene_word = _image_word(scene_index, family)
    sentences: list[str] = []

    if place:
        sentences.append(phrase(_PHRASES, lang, "scene_as_in", scene_word=scene_word, place=strip_phrase(place)))
    else:
        # No place name in the storyboard: point at the image without inventing one.
        sentences.append(phrase(_PHRASES, lang, "scene_as_in_unnamed", scene_word=scene_word))
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    objects = bible.get("objects") if isinstance(bible.get("objects"), list) else []
    props = [
        _scrub_lock_phrase(str(x))
        for x in objects[:4]
        if str(x).strip() and not _BAD_DIRECTIVE.search(str(x))
    ]
    props = [p for p in props if p]
    tod_sent = _time_of_day_sentence(cfg, lang)
    if tod_sent:
        sentences.append(tod_sent)
    else:
        lighting = _scrub_lock_phrase(str(bible.get("lighting") or ""))
        lighting = strip_phrase(localized_lighting(lighting, lang))
        desc_bits = [b for b in (lighting, list_separator(lang).join(props) if props else "") if b]
        if desc_bits:
            sentences.append(
                phrase(_PHRASES, lang, "scene_description", desc=("；" if zh else "; ").join(desc_bits))
            )
    if props and tod_sent:
        sentences.append(phrase(_PHRASES, lang, "props_in_view", props=list_separator(lang).join(props)))

    for line in _continuity_story_lines(cfg, lang):
        if line and line not in sentences:
            sentences.append(line)

    for line in _staging_lines(cfg, graph, tokens, lang):
        if line and line not in sentences and not _BAD_DIRECTIVE.search(line):
            sentences.append(line)

    # The storyboard's own action text always leads the cast lines: it carries
    # the causal order and any enumerated content (props, groups, icons).
    # Per-cast clauses below only anchor it to reference images — they are a
    # complement, never a replacement, so this sentence is never dropped.
    action_text = _storyboard_action_text(cfg, action)
    if action_text:
        action_sentence = sentence(action_text, lang)
        if action_sentence not in sentences:
            sentences.append(action_sentence)

    for index, token in enumerate(tokens, start=1):
        who = _name_for_id(token, graph) or str(token)
        if who.casefold() in gone_names:
            continue
        slot = cast_slots.get(str(token))
        if not isinstance(slot, int) or slot <= 0:
            slot = 0 if str(token) in unbound else index
        sentences.append(
            _story_cast_sentence(
                who=who,
                image_word=_image_word(slot, family) if slot else "",
                wardrobe=_wardrobe_for(token, cfg, graph, lang),
                seat=_seat_for(token, cfg, graph, lang),
                doing=_action_for(token, cfg, graph, action),
                scene_word=scene_word,
                scene=place,
                visibility="full",
                lang=lang,
                group=_cast_form_for(token, who, graph) == "group",
            )
        )

    for label, slot in zip(extra_labels, extra_slots):
        word = _image_word(slot, family)
        sentences.append(phrase(_PHRASES, lang, "connected_still", word=word, label=label))

    for who in _partial_names(cfg, graph):
        if who.casefold() in gone_names:
            continue
        sentences.append(
            _story_cast_sentence(
                who=who,
                image_word=phrase(_PHRASES, lang, "cast_references"),
                wardrobe=_wardrobe_for(who, cfg, graph, lang),
                seat=_seat_for(who, cfg, graph, lang),
                doing=_action_for(who, cfg, graph, ""),
                scene_word=scene_word,
                scene=place,
                visibility="partial",
                lang=lang,
            )
        )

    # Still-in-setting offscreen: only if they speak this shot (heard). Never name exited.
    speakers = _speaker_names(cfg, graph)
    for who in offscreen_names(cfg, graph):
        if who.casefold() in gone_names or who.casefold() in {n.casefold() for n in names}:
            continue
        if who.casefold() not in speakers:
            # Omitted from the call — occupancy lock stays on the node.
            continue
        wardrobe = _wardrobe_for(who, cfg, graph, lang)
        wearing = phrase(_PHRASES, lang, "offscreen_wearing", wardrobe=wardrobe) if wardrobe else ""
        sentences.append(
            phrase(_PHRASES, lang, "offscreen_heard", who=who, wearing=wearing, scene_word=scene_word)
        )

    cam = strip_phrase(camera or cfg.get("camera") or "")
    if cam and not _BAD_DIRECTIVE.search(cam):
        if zh:
            sentences.append(phrase(_PHRASES, lang, "camera", cam=cam))
        elif cam.lower().startswith("the camera"):
            sentences.append(cam + ".")
        else:
            sentences.append(
                f"The camera {cam[0].lower() + cam[1:] if cam[:1].isupper() else cam}."
            )

    sentences.extend(_speech_lines(cfg, graph, names, lang))
    lang_sent = _language_sentence(cfg, lang)
    if lang_sent:
        sentences.append(lang_sent)
    music = _music_line(cfg, lang, graph)
    if music:
        sentences.append(music)
    sound = _sound_line(cfg, lang)
    if sound:
        sentences.append(sound)
    look = _look_sentence(cfg, lang, has_refs=has_refs)
    if look and not _BAD_DIRECTIVE.search(look):
        sentences.append(look)

    text = join_sentences([s for s in sentences if s and not _BAD_DIRECTIVE.search(s)], lang)
    text = re.sub(r"[ \s]+", " ", text)
    text = re.sub(r"\s+\.", ".", text)
    # Final safety: strip any exited names that leaked via speech/continue text.
    for gone_name in exited_names(cfg, graph):
        if gone_name and gone_name.casefold() not in {n.casefold() for n in names}:
            if gone_name.isascii():
                text = re.sub(rf"(?i)\b{re.escape(gone_name)}\b[^.]*\.?", "", text)
            else:
                text = re.sub(rf"{re.escape(gone_name)}[^.。]*[.。]?", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text, _cov_notes = ensure_story_lock_coverage(text, cfg, graph=graph)
    return text


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


def _prompt_origin(cfg: dict[str, Any] | None) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    return str(generate.get("prompt_origin") or "").strip()


def resolve_user_origin_prompt(cfg: dict[str, Any] | None, fallback: str = "") -> str:
    """Resolve toolbar/user prompt authority for media calls.

    Beat authority for user edits is the durable user surface — never the last
    stamped API body (``last_wan_prompt`` / ``last_approved_prompt``). Prefer
    ``user_edit_prompt`` and regenerate_packet (what the toolbar wrote) over
    ``generate.prompt``, which is often a prior practice rewrite and would
    silently drop Film-shot facts (e.g. \"blue car\").

    A non-empty ``user_edit_prompt`` wins even if ``prompt_origin`` lagged behind
    the toolbar write (image + video regenerate).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    edit = str(cfg.get("user_edit_prompt") or "").strip()
    if edit:
        return edit
    if _prompt_origin(cfg) != "user":
        return ""
    generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    packet = cfg.get("regenerate_packet") if isinstance(cfg.get("regenerate_packet"), dict) else {}
    for candidate in (
        packet.get("prompt"),
        cfg.get("prompt"),
        generate.get("prompt"),
        fallback,
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def resolve_still_call_prompt(cfg: dict[str, Any] | None, fallback: str = "") -> str:
    """Still/image call authority: durable user text, else the LLM-supplied prompt.

    No hard-coded practice rewrite. Never prefer ``last_wan`` / ``last_approved``.
    """
    user = resolve_user_origin_prompt(cfg, "")
    if user:
        return user
    return str(fallback or "").strip()


_LOCK_BANNER_LINE = re.compile(
    r"(?i)^\s*(?:style\s*lock|costume\s*lock|clothing\s*lock|wardrobe\s*lock|"
    r"positioning\s*lock|scene\s*specs|forbid|forbidden|already-?done|on\s*screen)\b"
)
# Keep enough Film-shot narrative for prop/color edits (e.g. blue car) to
# survive rewrite into Image-N practice form.
_SEED_CAP = 1200


def narrative_seed_from_user_prompt(prompt: str) -> str:
    """Positive story lines from a toolbar prompt; drop lock-essay / forbid noise."""
    text = str(prompt or "").strip()
    if not text:
        return ""
    if not _BAD_DIRECTIVE.search(text) and not _LOCK_BANNER_LINE.search(text):
        return text[:_SEED_CAP]
    chunks: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text):
        line = part.strip().lstrip("-").strip()
        if not line:
            continue
        if _BAD_DIRECTIVE.search(line) or _LOCK_BANNER_LINE.search(line):
            continue
        # Inline lock essays (same paragraph as Action:) — cut from the banner on.
        cut = re.split(
            r"(?i)\b(?:style\s*lock|costume\s*lock|clothing\s*lock|wardrobe\s*lock|"
            r"positioning\s*lock|scene\s*specs|forbid|forbidden|already-?done|"
            r"identity\s*sheets|strategy\s*=)\b",
            line,
            maxsplit=1,
        )[0].strip(" ,;.—-")
        if not cut or _BAD_DIRECTIVE.search(cut):
            continue
        chunks.append(cut.rstrip("."))
    return ". ".join(chunks).strip()[:_SEED_CAP]


def _compose_cfg_for_user_origin(cfg: dict[str, Any], *, camera: str) -> dict[str, Any]:
    """Compose without re-seeding from stale storyboard beats/camera/cast_actions."""
    out = dict(cfg)
    out["clip_action"] = ""
    out["character_action"] = ""
    out["cast_actions"] = {}
    out["camera"] = str(camera or "")
    return out


def prompt_respects_practice(
    prompt: str,
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    honor_user_origin: bool = False,
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
    if film_scored_by_music_node(graph):
        # A clip never writes a score any more, so a hit here is a stale prompt:
        # match the score label itself rather than a phrase this build emits.
        labels = [
            str(_PHRASES[lang].get("audio") or "").split("{bits}")[0].strip().casefold()
            for lang in _PHRASES
        ]
        if any(label and label in text.casefold() for label in labels):
            reasons.append("clip_scores_film_music")
    # Toolbar user edits are authoritative for beat fidelity; do not force stale clip_action.
    if not honor_user_origin:
        action = str(cfg.get("clip_action") or "").strip()
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


_ACTION_LABELS = ("primary action", "character action", "storyboard shot", "storyboard beat", "action")


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
    clip_index: int = 0,
    model: str | None = None,
    action: str = "",
    camera: str = "",
    extra_image_labels: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Keep a concise faithful story-form prompt; otherwise rewrite locks into narrative."""
    del clip_index
    cfg = cfg if isinstance(cfg, dict) else {}
    user_text = resolve_user_origin_prompt(cfg, prompt)
    honor_user = bool(user_text)
    if honor_user:
        prompt = user_text
    if str(cfg.get("reference_call_mode") or "").strip():
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            compose_reference_clip_prompt,
            reference_prompt_issues,
        )

        raw = str(prompt or "").strip()
        reasons = reference_prompt_issues(raw, cfg)
        if not reasons:
            note = "kept_user_prompt" if honor_user else "kept_reference_led_prompt"
            return raw, [note]
        return compose_reference_clip_prompt(cfg), ["rewritten_reference_led", *reasons]
    reasons = prompt_respects_practice(
        prompt, cfg=cfg, graph=graph, honor_user_origin=honor_user,
    )
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.shot_consistency_contract import (
            prompt_violates_consistency,
        )

        for r in prompt_violates_consistency(prompt, cfg=cfg):
            if r not in reasons:
                reasons.append(r)
    except Exception:  # noqa: BLE001
        pass
    # Lock essays / labeled banners are not story form.
    raw = str(prompt or "").strip()
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
        note = "kept_user_prompt" if honor_user else "kept_agent_prompt"
        return raw, [note]
    # User-origin rewrites seed action/camera from the user surface, not stale beats.
    if honor_user:
        mined_action = (
            narrative_seed_from_user_prompt(action)
            or narrative_seed_from_user_prompt(user_text)
            or _pull_labeled(
                user_text,
                ("primary action", "character action", "storyboard shot", "storyboard beat", "action"),
            )
        )
        move = _pull_labeled(user_text, ("camera move",))
        angle = camera or _pull_labeled(user_text, ("camera for shot", "camera"))
    else:
        mined_action = (
            action
            or str(cfg.get("clip_action") or "")
            or _pull_labeled(prompt, _ACTION_LABELS)
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
    compose_cfg = (
        _compose_cfg_for_user_origin(cfg, camera=mined_camera) if honor_user else cfg
    )
    composed = compose_practice_prompt(
        cfg=compose_cfg,
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
    clip_index: int = 0,
    model: str | None = None,
    action: str = "",
    camera: str = "",
    extra_image_labels: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Director gate: concise story-form that still enforces wardrobe/seat/visibility locks.

    Approves a leaf rewrite when it is narrative, faithful, and short. Otherwise edits
    via compose_practice_prompt into the story-form Image-N binding.

    When generate.prompt_origin == user and a non-empty user prompt exists, that text
    is authoritative for beat fidelity; storyboard clip_action/camera are not forced.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    user_text = resolve_user_origin_prompt(cfg, prompt)
    honor_user = bool(user_text)
    if honor_user:
        prompt = user_text
        beat = (
            narrative_seed_from_user_prompt(action)
            or narrative_seed_from_user_prompt(user_text)
            or user_text
        )
        cam = (
            camera
            or _pull_labeled(user_text, ("camera move", "camera for shot", "camera"))
        )
    else:
        beat = (
            action
            or str(cfg.get("clip_action") or "")
            or _pull_labeled(prompt, _ACTION_LABELS)
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
        clip_index=clip_index,
        model=model,
        action=beat,
        camera=cam,
        extra_image_labels=extra_image_labels,
    )
    reasons = list(notes)
    # Storyboard beat force-rewrite stays for non-user origins only.
    if not honor_user and beat and not _covers_beat(approved, beat):
        approved = compose_practice_prompt(
            cfg=cfg,
            graph=graph,
            action=beat,
            camera=cam,
            model=model,
            extra_image_labels=extra_image_labels,
        )
        reasons = ["director_rewrote_for_storyboard", *reasons]
    elif not honor_user and beat and not _story_leads_prompt(approved, beat):
        approved = compose_practice_prompt(
            cfg=cfg,
            graph=graph,
            action=beat,
            camera=cam,
            model=model,
            extra_image_labels=extra_image_labels,
        )
        reasons = ["director_rewrote_story_first", *reasons]
    elif "director_rewrote" not in notes and (
        "kept_agent_prompt" in notes or "kept_user_prompt" in notes
    ):
        tag = "director_approved_user" if honor_user else "director_approved"
        reasons = [tag, *reasons]
    else:
        reasons = ["director_checked", *reasons]
    return approved.strip(), reasons
