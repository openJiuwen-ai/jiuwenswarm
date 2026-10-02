# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Load Designer scenario, director, agent, subject, and style skills."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import node_pipeline
from jiuwenswarm.server.runtime.designer.paths import skills_dir

logger = logging.getLogger(__name__)

_ROLE_ALIASES: dict[str, str] = {
    "brief": "brief",
    "character": "character",
    "character_design": "character",
    "scene": "scene",
    "storyboard": "storyboard",
    "frame": "frame",
    "keyframe": "frame",
    "clip": "clip",
    "compose": "compose",
    "final": "compose",
    "audio": "audio_bed",
    "audio_bed": "audio_bed",
    "music": "music",
    "speech": "speech_tts",
    "tts": "speech_tts",
    "speech_tts": "speech_tts",
    "mesh": "mesh",
    "3d": "mesh",
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("skill read failed %s: %s", path, exc)
        return ""


@lru_cache(maxsize=1)
def _skill_index() -> dict[str, Path]:
    root = skills_dir()
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for path in root.rglob("*.md"):
        key = path.stem.lower()
        index[key] = path
        # also index relative posix without suffix
        rel = path.relative_to(root).with_suffix("").as_posix().lower()
        index[rel] = path
    return index


def load_skill(*keys: str) -> str:
    """Return first matching skill markdown body (without YAML frontmatter if present)."""
    index = _skill_index()
    for key in keys:
        norm = str(key or "").strip().lower().replace("\\", "/")
        if not norm:
            continue
        path = index.get(norm) or index.get(norm.split("/")[-1])
        if path is None:
            continue
        text = _read_text(path)
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2].lstrip("\n")
        return text.strip()
    return ""


def load_scenario_skill(scenario: str) -> str:
    return load_skill(f"scenarios/{scenario}", scenario)


def load_orchestration_skill(role: str) -> str:
    return load_skill(f"orchestration/{role}", role)


def load_agent_skill(role_or_node: str) -> str:
    raw = str(role_or_node or "").strip().lower()
    # n_character / n_clip_1 → character / clip
    if raw.startswith("n_"):
        raw = raw[2:]
    raw = re.sub(r"_\d+$", "", raw)
    alias = _ROLE_ALIASES.get(raw, raw)
    return load_skill(f"agents/{alias}", alias)


def load_subject_skill(subject: str) -> str:
    return load_skill(f"subjects/{subject}", subject)


def load_style_skill(style_id: str) -> str:
    """Load a named video director style skill (skills/styles/<id>.md)."""
    return load_skill(f"styles/{style_id}", style_id)


def detect_subjects(prompt: str) -> list[str]:
    text = (prompt or "").lower()
    found: list[str] = []
    checks = [
        ("human", ("person", "people", "human", "man", "woman", "courier", "actor", "character", "人", "角色", "快递员")),
        ("vehicle", ("car", "truck", "bike", "motorcycle", "vehicle", "车", "汽车")),
        ("product", ("product", "bottle", "phone", "shoe", "packaging", "产品", "包装")),
        ("animal", ("dog", "cat", "bird", "animal", "宠物", "动物")),
        ("architecture", ("building", "interior", "room", "street", "alley", "建筑", "巷", "室内")),
        ("nature", ("forest", "ocean", "mountain", "sky", "nature", "自然", "风景")),
    ]
    for name, kws in checks:
        if any(kw in text for kw in kws):
            found.append(name)
    return found or ["human"]


def detect_audio_intent(prompt: str) -> dict[str, Any]:
    """Infer speech / music / silence policy from the user prompt."""
    from jiuwenswarm.server.runtime.designer.audio_locks import (
        infer_bgm_lock,
        infer_language_lock,
        prompt_requests_full_silence,
        user_declined_speech,
    )

    text = (prompt or "").lower()
    music_markers = (
        "music",
        "bgm",
        "soundtrack",
        "score",
        "song",
        "配乐",
        "音乐",
        "b gm",
    )
    language_lock = infer_language_lock(prompt or "")
    if prompt_requests_full_silence(prompt or ""):
        return {
            "policy": "silent",
            "include_speech": False,
            "include_music": False,
            "language_lock": language_lock,
            "bgm_lock": {},
            "notes": "User requested silence / no audio.",
        }
    # Speech is the default. Only an explicit mime / no-dialogue request turns it off.
    include_speech = not user_declined_speech(prompt or "")
    include_music = True
    if include_speech:
        policy = "speech_and_music"
    elif any(m in text for m in music_markers):
        policy = "music"
    else:
        policy = "optional_music"
    bgm_lock = infer_bgm_lock(prompt or "", {"policy": policy, "include_music": include_music})
    return {
        "policy": policy,
        "include_speech": include_speech,
        "include_music": include_music,
        "language_lock": language_lock,
        "bgm_lock": bgm_lock,
        "notes": f"Detected audio policy={policy}; language={language_lock}",
    }


def skill_bundle_for_graph(
    *,
    scenario: str,
    prompt: str,
    node_roles: list[str] | None = None,
    video_style: str | None = None,
) -> dict[str, Any]:
    subjects = detect_subjects(prompt)
    audio = detect_audio_intent(prompt)
    agent_skills = {
        role: load_agent_skill(role) for role in (node_roles or [])
        if load_agent_skill(role)
    }
    style_id = str(video_style or "").strip()
    style_skill = load_style_skill(style_id) if style_id else ""
    if not style_skill and style_id:
        try:
            from jiuwenswarm.server.runtime.designer.video_styles import (
                video_style_skill_excerpt,
            )

            style_skill = video_style_skill_excerpt(style_id)
        except Exception:  # noqa: BLE001
            style_skill = ""
    return {
        "scenario": scenario,
        "scenario_skill": load_scenario_skill(scenario),
        "director_skill": load_orchestration_skill("director"),
        "agent_skills": agent_skills,
        "subjects": subjects,
        "subject_skills": {s: load_subject_skill(s) for s in subjects},
        "video_style": style_id,
        "style_skill": style_skill,
        "audio": audio,
    }


def load_tool_skill(tool_key: str) -> str:
    return load_skill(f"tools/{tool_key}", tool_key)


def _first_tool_skill(*keys: str) -> str:
    for key in keys:
        text = load_tool_skill(key)
        if text:
            return text
    return ""


def _tool_skills_for_role(role: str) -> str:
    """Append media-tool playbooks used by this leaf."""
    role_l = str(role or "").strip().lower()
    chunks: list[str] = []
    if role_l in {
        "character",
        "character_design",
        "frame",
        "keyframe",
        "scene",
        "image",
    }:
        text = _first_tool_skill("image_gen", "qwen_image")
        if text:
            chunks.append(text)
    if role_l in {"clip", "video"}:
        text = _first_tool_skill("video_gen", "wan_video")
        if text:
            chunks.append(text)
    if role_l in {"compose", "final", "film"}:
        text = load_tool_skill("ffmpeg")
        if text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def attach_skills_metadata(graph: dict[str, Any], prompt: str | None = None) -> dict[str, Any]:
    """Attach task-specific agent skills to nodes; overall skills only on orchestration meta."""
    # Fresh disk index after skill file edits (dev restarts).
    try:
        _skill_index.cache_clear()
    except Exception:
        pass
    meta = dict(graph.get("metadata") or {})
    scenario = str(meta.get("scenario") or "video")
    text = prompt or str(graph.get("description") or "")
    try:
        from jiuwenswarm.server.runtime.designer.video_styles import (
            stamp_video_style_on_graph,
            video_style_skill_excerpt,
        )

        style_id = stamp_video_style_on_graph(graph, text)
        style_skill = load_style_skill(style_id) or video_style_skill_excerpt(style_id)
    except Exception:  # noqa: BLE001
        style_id = str(meta.get("video_style") or "")
        style_skill = load_style_skill(style_id) if style_id else ""
    meta = dict(graph.get("metadata") or {})
    roles: list[str] = []
    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(node_pipeline(node) or cfg.get("role") or cfg.get("agent_role") or node.get("id") or "")
        roles.append(role)
        # Leaf agents: role skill + matching tool playbook (image / video / ffmpeg).
        stamped_skill = str(cfg.get("skill_id") or "").strip()
        skill_text = (
            (load_agent_skill(stamped_skill) if stamped_skill else "")
            or load_agent_skill(role)
            or load_agent_skill(str(node.get("id") or ""))
        )
        tool_text = _tool_skills_for_role(role)
        # Clip / storyboard / brief also receive the active director style.
        role_l = _ROLE_ALIASES.get(role, role)
        style_bit = ""
        if style_skill and role_l in {
            "clip",
            "storyboard",
            "brief",
            "frame",
            "keyframe",
            "compose",
        }:
            style_bit = style_skill
            cfg["video_style"] = style_id
        merged = "\n\n".join(x for x in (skill_text, tool_text, style_bit) if x).strip()
        if merged:
            if not stamped_skill:
                cfg["skill_id"] = _ROLE_ALIASES.get(role, role)
            cfg["skill_excerpt"] = merged[:2800]
        else:
            cfg.pop("skill_excerpt", None)
        # Do not attach subject encyclopedia to every node here.
        cfg.pop("scenario_skill_excerpt", None)
        # Media playbook for frame/clip agents.
        try:
            from jiuwenswarm.server.runtime.designer.media_model_playbook import (
                playbook_for_role,
            )

            pb = playbook_for_role(role)
            excerpt = str(cfg.get("skill_excerpt") or "")
            if pb and "call_image_model" not in excerpt and "call_video_model" not in excerpt:
                cfg["skill_excerpt"] = (
                    str(cfg.get("skill_excerpt") or "") + "\n\n" + pb
                ).strip()[:2800]
        except Exception:
            pass
        node["config"] = cfg
    bundle = skill_bundle_for_graph(
        scenario=scenario,
        prompt=text,
        node_roles=roles,
        video_style=style_id,
    )
    # Overall skills live only on graph metadata for the director.
    meta["skills_package"] = "jiuwenswarm/server/runtime/designer/skills"
    meta["scenario_skill_excerpt"] = (bundle.get("scenario_skill") or "")[:4000]
    meta["director_skill_excerpt"] = (bundle.get("director_skill") or "")[:4000]
    if style_skill:
        # Prepend the active video style so the director sees it first.
        meta["director_skill_excerpt"] = (
            style_skill + "\n\n" + str(meta.get("director_skill_excerpt") or "")
        ).strip()[:5000]
        meta["style_skill_excerpt"] = style_skill[:4000]
    meta["subject_keys"] = bundle.get("subjects") or []
    meta["audio_intent"] = bundle.get("audio") or {}
    meta["skill_guided"] = True
    meta["skill_policy"] = (
        "Leaf nodes: agents/<skill_id>.md, plus a tool playbook when that file exists, "
        "and styles/<video_style>.md when a style is active. "
        "Director: orchestration/director.md plus the active video style."
    )
    graph["metadata"] = meta
    return graph
