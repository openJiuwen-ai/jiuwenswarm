# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Initial story-form drafts for Wan, Seedance, and MiniMax-H3.

Locks live on the node. The video call is a concise positive narrative only:
image refs, wardrobe, placement, visibility, action, speech — never forbid lists,
sit/stand examples, or scene-genre hardcodes. Exited cast is omitted until the
storyboard returns them to this setting. These templates seed new drafts;
authored tool-call prompts are preserved by video_call_contract.
"""

from __future__ import annotations

import os
import re
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CHARACTER,
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
from jiuwenswarm.server.runtime.designer.pipeline.reference_binding import (
    ReferenceSlot,
    image_word as reference_image_word,
    scene_slot,
)

_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "while": "{first}, while {second}.",
        "says": '{who} says: "{line}".',
        "says_offscreen": '{who}, heard offscreen, says: "{line}".',
        "voice_says": 'A voice says: "{line}".',
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
        "scene_description": "Scene description: {desc}.",
        "props_in_view": "Props in view: {props}.",
        "connected_still": "{word} is the connected still {label}, and that subject is visible in this shot.",
        "cast_references": "the cast references",
        "camera": "The camera {cam}.",
        "in_scene_ref": "in the scene from {scene_word}",
        "in_scene_plain": "in the scene",
        "offscreen_heard": "{who}{wearing} is {in_scene}, outside this camera frame, and is heard.",
        "offscreen_wearing": ", wearing {wardrobe},",
        "cast_from": "{who} from {image_word}",
        "cast_name": "{who}",
        "cast_wearing": "wearing {wardrobe}",
        "turnaround_refs": (
            "{images}: each is one person shown from the front, side, and back, with a head close-up; "
            "draw that person once in the shot."
        ),
        "group_variation": (
            "Within the group, each person differs in gesture, timing, and attention; "
            "no two share the same pose."
        ),
        "cast_seat": "{seat} {in_scene}",
        "cast_in_scene": "{in_scene}",
        "cast_partial": "is {in_scene}, partially visible",
        "cast_out_of_frame": "is {in_scene}, outside this camera frame",
        "shot_brief": "Film shot {idx} only. Camera {camera}. Action: {action}.",
        "shot_brief_scene": "Scene: {scene}.",
        "shot_brief_cast": "Focus cast on screen: {cast}.",
        "shot_brief_default_camera": "medium / eye-level",
    },
    "zh": {
        "while": "{first}，同时{second}。",
        "says": "{who}说：“{line}”。",
        "says_offscreen": "{who}在画外说：“{line}”。",
        "voice_says": "画外音说：“{line}”。",
        "sound_diegetic_only": "声音：仅本镜头的现场音（动作声与环境底噪）；不要配乐、不要背景音乐、不要氛围音床。",
        "sound_cues_only": "声音：仅{cues}；不要配乐、不要背景音乐、不要氛围音床。",
        "sound_silent": "声音：静音，无对白、无音效、无音乐。",
        "sound_no_speech": "人物没有台词、对白或独白。",
        "dialogue_language": "对白使用{name}。",
        "tod_with_lighting": "时间是{label}；{lighting}。",
        "tod": "时间是{label}。",
        "continue": "本镜头开始时，分镜中此前已完成的情节不再重演，同一场景和人物从既定的起始状态继续。",
        "scene_description": "场景描述：{desc}。",
        "props_in_view": "画面中的道具：{props}。",
        "connected_still": "{word} 是相连的静帧「{label}」，该主体在本镜头中可见。",
        "cast_references": "角色参考图",
        "camera": "镜头：{cam}。",
        "in_scene_ref": "位于 {scene_word} 的场景中",
        "in_scene_plain": "位于场景中",
        "offscreen_heard": "{who}{wearing}{in_scene}，在本镜头画框之外，只闻其声。",
        "offscreen_wearing": "（身穿{wardrobe}）",
        "cast_from": "{who}（{image_word}）",
        "cast_name": "{who}",
        "cast_wearing": "身穿{wardrobe}",
        "turnaround_refs": "{images} 各是一个人的正面、侧面、背面三视图加头部特写，镜头里每人只出现一次。",
        "group_variation": "组内每个人的手势、节奏与注意力各不相同，没有两个人是同一姿势。",
        "cast_seat": "{seat}，{in_scene}",
        "cast_in_scene": "{in_scene}",
        "cast_partial": "{in_scene}，部分可见",
        "cast_out_of_frame": "{in_scene}，在本镜头画框之外",
        "shot_brief": "仅拍第{idx}镜。镜头：{camera}。动作：{action}。",
        "shot_brief_scene": "场景：{scene}。",
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


def shot_brief_prompt(
    idx: int,
    *,
    camera: str,
    action: str,
    cast_names: list[str] | None = None,
    scene: str = "",
    lang: str | None = None,
) -> str:
    """User-visible clip prompt seeded from the storyboard row; locks stay in config.

    ``scene`` is the place the shot happens in. It is plain text the user may
    edit or delete; the image binding for the scene plate is added at call time.
    """
    lang = lang or current_content_language()
    body = phrase(
        _PHRASES,
        lang,
        "shot_brief",
        idx=idx,
        camera=strip_phrase(camera) or phrase(_PHRASES, lang, "shot_brief_default_camera"),
        action=strip_phrase(action),
    )
    place = strip_phrase(scene)
    if place:
        body = join_sentences([body, phrase(_PHRASES, lang, "shot_brief_scene", scene=place)], lang)
    cast = list_separator(lang).join(str(n).strip() for n in (cast_names or []) if str(n).strip())
    if cast:
        body = join_sentences([body, phrase(_PHRASES, lang, "shot_brief_cast", cast=cast)], lang)
    return body


def clip_scene_label(cfg: dict[str, Any] | None) -> str:
    """Place label of a clip's setting, from its scene specs; "" when none is known."""
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        visual_place_name,
    )

    cfg = cfg if isinstance(cfg, dict) else {}
    specs = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    return visual_place_name(specs.get("scene_name"))


def active_video_family(model: str | None = None) -> str:
    """wan | seedance | minimax, from the configured video model."""
    text = (model or os.getenv("VIDEO_GEN_MODEL_NAME") or "").strip().lower()
    if "seedance" in text or "doubao-seedance" in text:
        return "seedance"
    if "minimax" in text or "hailuo" in text:
        return "minimax"
    return "wan"


def _image_word(index: int, family: str) -> str:
    return reference_image_word(index, family)


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


def _music_line(cfg: dict[str, Any], lang: str = "en", graph: dict[str, Any] | None = None) -> str:
    """A clip never describes a score.

    The film's music is generated once by the music node and mixed in at compose.
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


def _time_of_day_payload(cfg: dict[str, Any]) -> dict[str, str]:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = _scrub_lock_phrase(
        str((tod or {}).get("lighting") or bible.get("lighting") or "")
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


def _wardrobe_for(token: str, cfg: dict[str, Any], graph: dict[str, Any] | None, lang: str = "en") -> str:
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
        for chunk in re.split(r"[;；\n]+", lock):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk or "：" in chunk:
                who, rest = re.split(r"[:：]", chunk, maxsplit=1)
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
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import wearable_phrase

    return wearable_phrase(raw, name=name, lang=lang)[:160]


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


def _turnaround_cast(graph: dict[str, Any] | None) -> set[str]:
    """Casefolded ids and names of cast whose spec sheet is a front/side/back turnaround."""
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        is_turnaround_sheet,
    )

    found: set[str] = set()
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != NODE_ROLE_CHARACTER:
            continue
        cfg = node_config(node)
        if not is_turnaround_sheet(cfg):
            continue
        tokens = [
            cfg.get("character_id"),
            cfg.get("character_name"),
            *(cfg.get("character_ids") or []),
            *(cfg.get("character_names") or []),
        ]
        found.update(str(t).strip().casefold() for t in tokens if str(t or "").strip())
    return found


def _story_cast_sentence(
    *,
    who: str,
    image_word: str,
    wardrobe: str,
    seat: str,
    doing: str,
    in_scene: str,
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
        bits = [phrase(_PHRASES, lang, "cast_name", who=who)]
    if wardrobe:
        bits.append(phrase(_PHRASES, lang, "cast_wearing", wardrobe=wardrobe))
    if visibility == "full":
        if seat:
            bits.append(phrase(_PHRASES, lang, "cast_seat", seat=seat, in_scene=in_scene))
        else:
            bits.append(phrase(_PHRASES, lang, "cast_in_scene", in_scene=in_scene))
    elif visibility == "partial":
        bits.append(phrase(_PHRASES, lang, "cast_partial", in_scene=in_scene))
    else:
        # In-setting but out of this frame (not exited). Positive camera framing only.
        bits.append(phrase(_PHRASES, lang, "cast_out_of_frame", in_scene=in_scene))
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
    slots: list[ReferenceSlot] | None = None,
) -> str:
    """Story-form video prompt: only present cast; omit exited until they return.

    With ``slots`` (the images attached to the call) every image number comes
    from the attach order, and a member or scene without an attached image is
    named without one. Without slots the prompt is a seed for a call whose
    files are not known yet, and cast/scene are numbered in Wan attach order.
    The place itself is named by the reference binding appended at call time.
    """
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
    if slots is None:
        scene_word = _image_word(len(tokens) + 1, family)
        extras: list[tuple[str, str]] = []
    else:
        scene = scene_slot(slots)
        scene_word = _image_word(scene.index, family) if scene is not None else ""
        extras = [
            (_image_word(slot.index, family), slot.label)
            for slot in slots
            if slot.role in {"upload", "other"}
        ]
    in_scene = (
        phrase(_PHRASES, lang, "in_scene_ref", scene_word=scene_word)
        if scene_word
        else phrase(_PHRASES, lang, "in_scene_plain")
    )

    def cast_word(position: int, who: str) -> str:
        if slots is None:
            return _image_word(position, family)
        for slot in slots:
            if slot.role == "character" and slot.label.casefold() == who.casefold():
                return _image_word(slot.index, family)
        return ""

    sentences: list[str] = []
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

    # The storyboard's own action text always leads the cast lines: it carries
    # the causal order and any enumerated content (props, groups, icons).
    # Per-cast clauses below only anchor it to reference images — they are a
    # complement, never a replacement, so this sentence is never dropped.
    action_text = _storyboard_action_text(cfg, action)
    if action_text:
        action_sentence = sentence(action_text, lang)
        if action_sentence not in sentences:
            sentences.append(action_sentence)

    turnaround = _turnaround_cast(graph)
    turnaround_words: list[str] = []
    for index, token in enumerate(tokens, start=1):
        who = _name_for_id(token, graph) or str(token)
        if who.casefold() in gone_names:
            continue
        image_word = cast_word(index, who)
        if image_word and (token.casefold() in turnaround or who.casefold() in turnaround):
            turnaround_words.append(image_word)
        sentences.append(
            _story_cast_sentence(
                who=who,
                image_word=image_word,
                wardrobe=_wardrobe_for(token, cfg, graph, lang),
                seat=_seat_for(token, cfg, graph, lang),
                doing=_action_for(token, cfg, graph, action),
                in_scene=in_scene,
                visibility="full",
                lang=lang,
                group=_cast_form_for(token, who, graph) == "group",
            )
        )
    if turnaround_words:
        sentences.append(
            phrase(
                _PHRASES,
                lang,
                "turnaround_refs",
                images=list_separator(lang).join(turnaround_words),
            )
        )

    for word, label in extras:
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
                in_scene=in_scene,
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
            phrase(_PHRASES, lang, "offscreen_heard", who=who, wearing=wearing, in_scene=in_scene)
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
    look = _style_phrase(cfg)
    if look and not _BAD_DIRECTIVE.search(look):
        sentences.append(sentence(look, lang))

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
