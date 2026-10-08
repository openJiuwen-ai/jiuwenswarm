# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic Hollywood leaf-agent instructions (safe for any brief).

Used by Plan A leaf DeepAgents (deepseek-flash etc.) that may lack vision.
Agents must parse brief + storyboard text, optionally inspect upstream images,
then author a single production-ready visual-model prompt.
"""

from __future__ import annotations

from typing import Any


def _image_limit_clause() -> str:
    try:
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            image_prompt_limit_guidance,
        )

        return image_prompt_limit_guidance()
    except Exception:  # noqa: BLE001
        return (
            "IMAGE PROMPT LIMIT: follow the configured image backend's documented "
            "prompt length."
        )


def _video_limit_clause() -> str:
    try:
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            video_prompt_limit_guidance,
        )

        return video_prompt_limit_guidance()
    except Exception:  # noqa: BLE001
        return (
            "VIDEO PROMPT LIMIT: follow the configured video backend's documented "
            "prompt length."
        )


def hollywood_leaf_instructions(role: str) -> str:
    """Compact continuity bible for leaf agents — no scene-genre hardcodes."""
    r = str(role or "").strip().lower()
    shared = (
        "You are a leaf craft artist under one Director. "
        "The Director owns the brief, storyboard, shot prompts, gates, budgets, "
        "and identity/spatial consistency checks. "
        "You MUST read the STORYBOARD (and BRIEF only when you are the storyboard leaf) "
        "and obey the Director's on_screen cast + setting_id. "
        "If vision tools exist, inspect upstream sheets/scene/prior KF and reconcile conflicts "
        "toward the storyboard (never invent a new cast or set).\n"
        "Non-negotiables:\n"
        "1) STORYBOARD + PRODUCTION LOCK BIBLE first — call read_upstream on "
        "n_storyboard BEFORE any image/video tool. Clips and stills do not re-read the full "
        "brief; the storyboard already carries the shot. Obey style, landmarks, "
        "lighting, crowd, speech_line, on_screen cast, setting_id.\n"
        "2) CAST IDENTITY: one body per character id; match wardrobe/face from the FEW attached "
        "solo sheets only. Never swap heroes; never clone one face onto two bodies. "
        "Do not request extra solo sheets for background people.\n"
        "3) SET: same setting_id → same architecture, furniture, window/wall layout, light side, "
        "and landmark screen-side (never teleport landmarks).\n"
        "4) STYLE: obey style_lock / LOCK BIBLE for the entire film; "
        "copy the brief/storyboard medium; if they could not infer one, use their "
        "cartoonish default; "
        "no mid-film medium switch (3D<->2D<->photoreal).\n"
        "5) BLOCKING: honor zones + landmark + CONTACT (bodies on supports, never "
        "through furniture/props); featured subjects face the landmark/action focus. "
        "Pose/placement come from THIS storyboard row — never invent sit/stand defaults.\n"
        "6) SCREEN AXIS: keep L/R placement stable under pans (180-degree). Do not flip who is left/right.\n"
        "7) SETTING MASTER: scene specs + identity solos. First clip of the setting "
        "plays the storyboard open; later same-setting clips continue from structured "
        "continuity (already_done / pose_holds / seat_anchors / forbidden_speech / end_state) "
        "— never from raw prior Wan paragraphs. "
        "Only name on_screen / returned cast. If someone left the setting, omit them from every "
        "later same-setting prompt until the storyboard returns them. Never spawn unnamed extras.\n"
        "8) LANGUAGE: use storyboard speech_line exactly (empty = silent — do not invent lines).\n"
        "9) ASPECT: obey film aspect_lock on every still/clip.\n"
        "10) IMAGE-N: if references exist, name Image 1, Image 2… in attach order in your prompt.\n"
        "11) Finish with designer_node_complete(text=<FINAL visual-model prompt only>). "
        "The pipeline materializes media from that text — make it shot-ready, not a memo.\n"
        f"12) {_image_limit_clause()}\n"
        f"13) {_video_limit_clause()}\n"
    )
    if r in {"character", "character_design"}:
        return (
            shared
            + "ROLE=character sheet. Write ONE positive Qwen-ready studio portrait prompt "
            "from the locks (name, wardrobe, style, aspect). Plain empty backdrop, one person. "
            "Fold wardrobe into 'wearing …' prose — no LOCK banners, no forbid lists, no examples. "
            "Then call_image_model with that prompt only. Finish with designer_node_complete "
            "preferring the PNG uri (text notes are optional debug only)."
        )
    if r == "scene":
        return (
            shared
            + "ROLE=scene specs. Write ONE positive Qwen-ready environment prompt "
            "from the locks (place, lighting/time-of-day, style, aspect, props). "
            "Furniture, walls, windows, light, and props as a clear empty room. "
            "No LOCK banners, no forbid lists, no examples. "
            "Then call_image_model with that prompt only. Finish with designer_node_complete "
            "preferring the PNG uri (text notes are optional debug only)."
        )
    if r in {"frame", "keyframe"}:
        return (
            shared
            + "ROLE=keyframe still (legacy graphs only). "
            "Prefer scene-card + clip reference mode when available."
        )
    if r in {"storyboard", "brief"}:
        return (
            shared
            + "ROLE=storyboard/brief. The BRIEF is your source. Turn it into timed shots with "
            "setting_id, on_screen, offscreen, cast_actions, camera, and speech. "
            "Downstream clips read the storyboard — not the raw brief."
        )
    if r == "clip":
        return (
            shared
            +             "ROLE=clip. Write ONE concise positive story-form video prompt "
            "(Wan / Seedance / MiniMax-H3).\n"
            "FORM (locks folded into narrative — faithful, general, no examples):\n"
            "The scene is as in Image N: <place>. Scene description: <lighting/props from bible>.\n"
            "<Name> from Image k, wearing <wardrobe lock>, <placement from seat_anchors>, "
            "in the scene from Image N, is <cast_action / storyboard shot>. "
            "Name only people who are on_screen or partially visible this shot. "
            "Omit exited cast entirely until the storyboard returns them. "
            "The camera <one move>. <Speaker> says: \"<this shot's line>\".\n"
            "PRIORITY: (1) this storyboard shot + camera visibility from occupancy, "
            "(2) wardrobe + placement folded into those sentences, (3) same-setting continue "
            "from structured continuity fields only — never paste prior Wan text "
            "into call_video_model; never restage finished speech/action.\n"
            "OPENING HOLDS: if pose_holds / beat_done / crowd_state exist, open the prompt "
            "already past those beats (e.g. already facing the landmark; crowd already gone "
            "or still in background). Never restage a finished turn/exit/onset unless THIS "
            "storyboard row asks to repeat it.\n"
            "Clip API call rules: positive sentences only — no forbid, no 'do not', no already-done, "
            "no STYLE LOCK banners, no worked examples, no sit/stand defaults.\n"
            + _wan_leaf_extra()
        )
    return shared


def _wan_leaf_extra() -> str:
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
            wan_leaf_skill_block,
        )

        return wan_leaf_skill_block(role="clip")
    except Exception:  # noqa: BLE001
        return (
            "CONTACT: bodies never intersect furniture/props; when the shot uses a support "
            "surface, weight rests ON it. Solo sheets are identity — repose for the shot."
        )


def collect_production_context(
    *,
    brief_text: str = "",
    storyboard_text: str = "",
    continuity_guide: str = "",
    skill_excerpt: str = "",
    generate_prompt: str = "",
    max_chars: int = 4500,
) -> dict[str, str]:
    """Trim production texts for leaf agent JSON context."""

    def _trim(s: str, n: int) -> str:
        t = (s or "").strip()
        return t[:n] if t else ""

    return {
        "brief_excerpt": _trim(brief_text, 2800),
        "storyboard_excerpt": _trim(storyboard_text, 3200),
        "continuity_guide": _trim(continuity_guide, 1200),
        "skill_excerpt": _trim(skill_excerpt, 900),
        "current_generate_prompt": _trim(generate_prompt, 1200),
        "hollywood_instructions": hollywood_leaf_instructions(""),
    }


def merge_director_task(existing: str, planned: str) -> str:
    """Keep graph-built Hollywood tasks; append planner task if useful."""
    old = (existing or "").strip()
    new = (planned or "").strip()
    if not old:
        return new
    if not new:
        return old
    markers = (
        "MOVIE CONTINUITY",
        "Image 1",
        "STYLE LOCK",
        "environment plate",
        "solo sheet",
        "R2V",
        "keyframe",
    )
    if any(m.lower() in old.lower() for m in markers):
        if new and new not in old and "Execute " in new:
            return old
        if new and new not in old:
            return f"{old}\nPlanner note: {new}"[:2000]
        return old
    return new or old


def upstream_image_paths(
    ctx: Any,
    pred_ids: list[str],
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Resolve upstream image file paths for vision inspect / prompt binding."""
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

    run = getattr(ctx, "run", None) or {}
    states = run.get("node_states") or {}
    graph = getattr(ctx, "graph", None) or {}
    role_by_id = {
        str(n.get("id") or ""): str((n.get("config") or {}).get("role") or "")
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict)
    }
    out: list[dict[str, str]] = []
    for nid in pred_ids:
        st = states.get(nid) or {}
        refs: list[Any] = []
        if isinstance(st.get("output_ref"), dict):
            refs.append(st["output_ref"])
        for r in st.get("output_refs") or []:
            if isinstance(r, dict):
                refs.append(r)
        for ref in refs:
            p = path_from_uri(str(ref.get("uri") or ""))
            if p is None or not p.is_file():
                continue
            if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            out.append(
                {
                    "node_id": str(nid),
                    "role": role_by_id.get(str(nid), ""),
                    "path": str(p),
                }
            )
            break
        if len(out) >= limit:
            break
    return out
