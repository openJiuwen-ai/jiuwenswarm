# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Still-image locks, and the short prompt sent to the video model.

Keyframe stills still receive style and wardrobe locks. Clip calls are
rewritten into a concise story-form prompt (wardrobe/seats/visibility
folded into narrative); those locks stay on the node for the agent.
"""

from __future__ import annotations

import re
from typing import Any

_SEED_CONTENT_WORD = re.compile(r"[A-Za-z\u4e00-\u9fff]{3,}")
_SEED_STOP = frozenset(
    {
        "the",
        "and",
        "with",
        "from",
        "into",
        "onto",
        "that",
        "this",
        "only",
        "shot",
        "film",
        "camera",
        "action",
        "focus",
        "cast",
        "screen",
        "wide",
        "medium",
        "tracking",
        "gentle",
        "family",
        "must",
        "differ",
        "sibling",
        "shots",
    }
)


def _merge_user_seed_facts(approved: str, seed: str) -> str:
    """If compose dropped distinctive user nouns, append a short seed clause."""
    text = str(approved or "").strip()
    beat = str(seed or "").strip()
    if not text or not beat:
        return text
    hay = text.casefold()
    words = [
        w
        for w in _SEED_CONTENT_WORD.findall(beat)
        if w.casefold() not in _SEED_STOP and not w.isdigit()
    ]
    # Need several content hits; if under ~40% of distinctive words appear, fold seed in.
    if not words:
        return text
    hit = sum(1 for w in words if w.casefold() in hay)
    if hit >= max(2, int(len(words) * 0.4)):
        return text
    clause = beat if len(beat) <= 280 else beat[:277].rstrip() + "..."
    merged = f"{text.rstrip('. ')}. Also: {clause}."
    return merged[:2200]


def _style_from(cfg: dict[str, Any], graph: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    for src in (
        cfg.get("style_lock"),
        (graph.get("metadata") or {}).get("style_lock") if isinstance(graph.get("metadata"), dict) else None,
        analysis.get("style_lock"),
    ):
        if isinstance(src, dict) and src:
            return src
    return {}


def _aspect_from(cfg: dict[str, Any], graph: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    for src in (
        cfg.get("aspect_lock"),
        (graph.get("metadata") or {}).get("aspect_lock") if isinstance(graph.get("metadata"), dict) else None,
        analysis.get("aspect_lock"),
    ):
        if isinstance(src, dict) and src:
            return src
    return {}


def set_orientation_lock_clause(*, scene_card: bool = False) -> str:
    """Keep first-frame room orientation stable (fixes spin/flip drift)."""
    subject = "Scene card / Image 1" if scene_card else "Image 1"
    return (
        f"SET/ORIENTATION LOCK: keep {subject}'s exact room geometry, wall/window sides, "
        "furniture layout, and camera roll/horizon — FORBIDDEN: spin or rotate the set, "
        "mirror/flip architecture left-right, tilt the world, or rebuild a different room. "
        "Camera may push-in/pan slightly only if it does not reorient the space."
    )


def apply_keyframe_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> str:
    """Resolve still/image call text without hard-coded practice rewrites.

    User toolbar text (``user_edit_prompt`` / packet) wins; otherwise the
    LLM-authored ``prompt`` argument is sent as-is. Structured locks stay on
    cfg for the leaf agent — they are not appended or rewritten here.
    """
    del graph  # locks remain structured on cfg; not stamped into the API body
    cfg_map = cfg if isinstance(cfg, dict) else {}
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        resolve_user_origin_prompt,
    )

    user_text = resolve_user_origin_prompt(cfg_map, "")
    text = (user_text or str(prompt or "").strip())[:6000]
    if text:
        # Stamp last_* with what we send; keep durable user surfaces intact.
        _stamp_sent_prompt(
            cfg_map,
            text,
            user_origin=bool(user_text),
            user_edit=user_text,
        )
        cfg_map["director_still_prompt_notes"] = (
            ["still_user_authority"] if user_text else ["still_llm_authority"]
        )
        cfg_map["director_still_prompt_approved"] = True
    return text


def _stamp_sent_prompt(
    cfg: dict[str, Any],
    approved: str,
    *,
    user_origin: bool,
    user_edit: str = "",
) -> None:
    """Keep saved surfaces honest with the text actually sent to the video API.

    When origin is user, durable beat authority (``user_edit_prompt`` + packet)
    stays the pre-rewrite toolbar text so the next regenerate cannot treat a
    stamped practice body as the user's intent.
    """
    text = str(approved or "").strip()
    if not text:
        return
    edit = str(user_edit or cfg.get("user_edit_prompt") or "").strip()
    if user_origin and edit:
        cfg["user_edit_prompt"] = edit[:4000]
        packet = (
            dict(cfg["regenerate_packet"])
            if isinstance(cfg.get("regenerate_packet"), dict)
            else {}
        )
        packet["prompt"] = edit[:4000]
        cfg["regenerate_packet"] = packet
    cfg["prompt"] = text
    generate = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
    generate["prompt"] = text
    if user_origin:
        generate["prompt_origin"] = "user"
    cfg["generate"] = generate
    cfg["last_wan_prompt"] = text[:4000]
    cfg["last_approved_prompt"] = text[:4000]


def apply_wan_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
    shot_index: int = 0,
) -> str:
    """Rewrite the video call into a short image-binding prompt.

    Style, wardrobe, and storyboard locks stay on the node. The video model
    receives who each image is, where they are, what they do, and what they say.

    When generate.prompt_origin == user with a non-empty user prompt, that text
    seeds action/camera instead of stale shot_action/camera beats.
    """
    # Stamp/notes must mutate the caller's cfg. ensure_prior_clip_story_on_cfg
    # returns a shallow copy — use that copy only as the director working view.
    cfg_map = cfg if isinstance(cfg, dict) else {}
    director_cfg = cfg_map
    if isinstance(graph, dict):
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
                ensure_prior_clip_story_on_cfg,
            )

            director_cfg = ensure_prior_clip_story_on_cfg(cfg_map, graph)
        except Exception:  # noqa: BLE001
            director_cfg = cfg_map
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _pull_labeled,
        director_approve_video_prompt,
        narrative_seed_from_user_prompt,
        resolve_user_origin_prompt,
    )

    user_text = resolve_user_origin_prompt(cfg_map, prompt)
    if user_text:
        candidate = user_text
        action = narrative_seed_from_user_prompt(user_text) or user_text
        camera = _pull_labeled(user_text, ("camera move", "camera for shot", "camera"))
        if not camera:
            # Film-shot essays often put Camera on the same line as "Film shot N".
            m = re.search(
                r"(?i)\bcamera\b\s*[:.]?\s*(.+?)(?:\.\s*action\b|\.\s*focus\b|$)",
                user_text,
            )
            if m:
                camera = m.group(1).strip()[:240]
    else:
        candidate = prompt
        action = str(director_cfg.get("shot_action") or "")
        camera = str(director_cfg.get("camera") or "")

    approved, notes = director_approve_video_prompt(
        candidate,
        cfg=director_cfg,
        graph=graph if isinstance(graph, dict) else {},
        shot_index=shot_index,
        action=action,
        camera=camera,
    )
    # If compose dropped user nouns (prop/color), fold a short seed clause in.
    if user_text and action:
        approved = _merge_user_seed_facts(approved, action)
    cfg_map["director_video_prompt_notes"] = list(notes)
    cfg_map["director_video_prompt_approved"] = True
    _stamp_sent_prompt(
        cfg_map,
        approved,
        user_origin=bool(user_text),
        user_edit=user_text,
    )
    return approved
