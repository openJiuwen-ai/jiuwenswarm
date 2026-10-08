# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Wan 3.0 + Qwen-Image multi-angle playbooks for Director / leaf agents.

Encode prompt and tool guidance so storyboards specify objects + camera facing,
and media tools keep identity / style / positioning consistent.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Camera diversity (Qwen-Image-Edit Multiple-Angles style controls)
# ---------------------------------------------------------------------------

# azimuth: 0=front, 90=right, 180=back, 270=left
# elevation: -30 low … 0 eye … 60 high
# distance: 0.6 close · 1.0 medium · 1.4 wide
ANGLE_PRESETS: list[dict[str, Any]] = [
    {
        "label": "wide / front establishing",
        "azimuth": 0,
        "elevation": 5,
        "distance": 1.4,
        "facing": "front of subject / space",
        "move": "static",
    },
    {
        "label": "medium / three-quarter right",
        "azimuth": 45,
        "elevation": 0,
        "distance": 1.0,
        "facing": "front-right of subject",
        "move": "subtle dolly-in",
    },
    {
        "label": "close-up / front",
        "azimuth": 0,
        "elevation": 5,
        "distance": 0.6,
        "facing": "front of subject",
        "move": "hold",
    },
    {
        "label": "medium / left profile (side)",
        "azimuth": 270,
        "elevation": 0,
        "distance": 1.0,
        "facing": "left side / profile",
        "move": "static",
    },
    {
        "label": "over-the-shoulder / reverse toward partner",
        "azimuth": 160,
        "elevation": 0,
        "distance": 1.0,
        "facing": "OTS behind foreground character looking at partner",
        "move": "static",
    },
    {
        "label": "wide / back reverse angle",
        "azimuth": 180,
        "elevation": 8,
        "distance": 1.4,
        "facing": "back of subject / reverse of shot 1",
        "move": "static",
    },
]

STYLE_LOCK_DEFAULT = {
    "look": (
        "cartoonish animated feature look, flat shapes, soft rendering, rounded forms, "
        "coherent across all sheets/frames/clips"
    ),
    "lens": "animated cinematic framing; soft readable shapes; no comic/grid UI",
    "palette": "match the brief's palette; keep colors and line weight stable across shots",
    "medium": "stylized_animation",
    "forbid": "no style drift, no outfit redesign, no new architecture, no subtitles/watermarks",
}

QWEN_IMAGE_PLAYBOOK = """
## Configured image model (call_image_model)
Use for solo character sheets and scene specs / stills.
- PROMPT LENGTH: obey the IMAGE PROMPT LIMIT stamped from the configured VISUAL_GEN
  backend (Director / leaf context). Prefer dense shot-ready prose; do not pad.
- COST: Prefer ~1K resolution (size 1K / 1024x1024 or aspect-matched ~1K). Do not request 2K/4K.
- EVERY on-screen character gets an identity sheet before any keyframe, including an unnamed group that shares one look.
- First KF of a setting_id: compose_from_solo_refs — GENERATE the setting AND place ONLY
  storyboard on_screen cast with cast_actions. Author a detailed SCENE SPECS (objects,
  lighting, crowd, hierarchical views: front/left/right/side/top/bottom).
- Later KF same setting_id: ALSO compose_from_solo_refs from character solos, but reuse the
  SCENE PROMPT HANDOFF / scene specs so architecture stays deterministic. Change only view +
  on_screen/doing. Never edit a prior keyframe image as the primary ref.
- Multi-ref prompting: name each slot ("Image 1 is Pastor… Image 2 is Young Man…").
  State placements (screen-left/right), gaze targets, and what must NOT change.
- CROWD LOCK: if the first KF shows a congregation/extras, keep them in every later KF
  unless storyboard exits them — never pop a new crowd mid-scene.
""".strip()

WAN3_VIDEO_PLAYBOOK = """
## Configured video model (call_video_model) — PRIMARY film path
Every clip: R2V from the scene specs + on-screen solos.
Same-setting later clips keep prior continuity in agent context (not last-frame pixels).
Do not attach the peopled master as last environment (clones / people who left).
Prompt formula: Character + Action + Lines + Scene.
  Name character1… in attach order; speech_line exact or silent.
Always lead with positive story-form binding (no LOCK banners on the tool body):
  SAME-SCENE GATE: agree with THIS storyboard row; continue from previous clip;
  do not repeat finished actions unless this row / user prompt asks to repeat
Keep clips short (~5s); one primary camera move; obey VIDEO PROMPT LIMIT from the
configured VIDEO_GEN backend.
""".strip()

STORYBOARD_DETAIL_RULES = """
## Storyboard detail (Director authors — hierarchical by SCENE then keyframes)
Group shots by setting_id / place. For EACH scene block author a SCENE SPECS:
objects + locations, lighting, crowd size/positions, and hierarchical views
(front/left/right/side/top/bottom) so coverage is coherent and things do not appear
from nowhere. For EACH scene:
1. First keyframe = compose_from_solo_refs: Qwen generates the setting AND places cast
   from solo identity sheets (NO scene specs node). Include scene specs in the brief.
2. Later keyframes in the SAME scene = compose_from_solo_refs again with SCENE PROMPT
   HANDOFF from the master (reuse architecture; change view/cast only).
3. New setting_id → new scene specs + compose master, then handoffs again.
Per shot also list: timeline, camera/view_key, placements, motion, speech_line,
character_ids, continuity_lock, style_lock, shot_relation, keyframe_strategy,
keyframe_prompt. Diversify angles within a locked geography/style.
Node labels should read like: character: [Name]; scene [n]: keyframe [n]; scene [n]: clip [n].
""".strip()

SUPERVISOR_CORRECTION_HINTS = """
When correcting leaf agents / drafting node prompts:
- Character solos = identity only. Every KF composes cast INTO the locked scene specs
  (style_lock + setting text + hierarchical views). Same-setting later KFs: reuse master
  scene prompt handoff — do not edit prior keyframe images as the primary ref.
- Clip: DETAILED Wan R2V prompt with the brief/storyboard STYLE LOCK + CONTACT lock
  + set/orientation + cast/prop locks (not STYLE HOLD alone).
- NON-NEGOTIABLE film-wide locks on EVERY keyframe AND clip: aspect_lock (one ratio;
  stills at the chosen image size; video at the user or director resolution),
  style_lock, spatial_lock, costume/identity, occupancy, screen axis.
- If agents ignore storyboard facing/objects/speech OR drop any lock, rewrite
  director_task / generate.prompt once (no loops).
""".strip()

MANAGER_CORRECTION_HINTS = """
Director one-pass corrections (locks bind ALL agents — leaf rewrites cannot drop them):
- Reject / rewrite shots that all share the same facing or that omit placement/motion/speech/objects.
- Require camera_rig + screen_positions + speech_line; stamp setting_id + keyframe_strategy.
- BEFORE media calls: gate every frame/keyframe/clip prompt for aspect_lock, style_lock,
  spatial_lock, costume/identity, occupancy, and prior continuity; re-inject missing locks.
- Stamp image_size from aspect_lock on stills and the user or director video_size/video_resolution on clips.
- Graph = brief→storyboard→solo cast→scene specs→R2V shots→compose.
- Prune only true orphans; every kept node must reach compose.
""".strip()


def playbook_for_role(role: str) -> str:
    role_l = str(role or "").lower()
    chunks = [STORYBOARD_DETAIL_RULES]
    if role_l in {"storyboard", "brief", "director"}:
        chunks.extend([QWEN_IMAGE_PLAYBOOK, WAN3_VIDEO_PLAYBOOK])
    if role_l in {"frame", "keyframe", "scene", "character", "character_design", "image"}:
        chunks.append(QWEN_IMAGE_PLAYBOOK)
    if role_l in {"clip", "video"}:
        chunks.append(WAN3_VIDEO_PLAYBOOK)
    if role_l in {"director", "brief", "storyboard"}:
        chunks.append(SUPERVISOR_CORRECTION_HINTS)
    if role_l in {"director"}:
        chunks.append(MANAGER_CORRECTION_HINTS)
    return "\n\n".join(chunks)


def default_style_lock(prompt: str = "", scene_desc: str = "") -> dict[str, str]:
    """Infer the brief's film-wide style; unspecified briefs default to cartoonish."""
    lock = dict(STYLE_LOCK_DEFAULT)
    blob = f"{prompt} {scene_desc}".lower()
    # Explicit artistic media must survive instead of being collapsed into either
    # animation or photography.
    artistic_media: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (
            ("watercolor", "watercolour", "水彩"),
            "watercolor illustration with soft washes and visible paper texture",
            "watercolor_illustration",
        ),
        (
            ("oil painting", "oil-painted", "油画"),
            "oil-painted artwork with visible brush texture and painterly lighting",
            "oil_painting",
        ),
        (
            ("ink wash", "ink painting", "水墨", "国画"),
            "ink-wash illustration with expressive lines and controlled tonal washes",
            "ink_wash",
        ),
        (
            ("pixel art", "像素风", "像素画"),
            "pixel-art animation with deliberate pixel shapes and a stable limited palette",
            "pixel_art",
        ),
        (
            ("claymation", "clay animation", "黏土动画", "粘土动画"),
            "clay-animation look with tactile rounded models and soft handcrafted lighting",
            "clay_animation",
        ),
        (
            ("paper cut", "papercut", "剪纸"),
            "paper-cut illustration with layered shapes and handcrafted edges",
            "paper_cut",
        ),
    )
    for cues, look, medium in artistic_media:
        if any(cue in blob for cue in cues):
            lock["look"] = look
            lock["lens"] = "cinematic framing appropriate to the brief's chosen art medium"
            lock["palette"] = "preserve the brief's specified palette and material treatment"
            lock["medium"] = medium
            lock["forbid"] = (
                "no style drift between shots, no outfit redesign, no new architecture, "
                "no subtitles/watermarks, no switch to photography or another art medium"
            )
            return {k: str(v)[:280] for k, v in lock.items()}

    # Stylization must win over incidental camera language such as "cinematic".
    if any(
        k in blob
        for k in (
            "cartoon",
            "cartoonish",
            "pixar",
            "anime",
            "cel-shaded",
            "cel shaded",
            "stylized",
            "2d animated",
            "3d animated",
            "toon",
            "卡通",
            "动画",
            "动漫",
            "二次元",
            "赛璐璐",
            "扁平",
            "圆润造型",
            "柔和渲染",
        )
    ):
        lock["look"] = (
            "cartoonish animated feature look (卡通画风), flat shapes, soft rendering, "
            "rounded forms, coherent across every sheet, keyframe, and clip"
        )
        lock["lens"] = "animated cinematic framing; soft readable shapes; no comic grid UI"
        lock["palette"] = "match the brief's cartoon palette; keep dyes locked across shots"
        lock["medium"] = "stylized_animation"
        lock["forbid"] = (
            "no style drift between shots, no outfit redesign, no new architecture, "
            "no subtitles/watermarks, no labeled infographic character specs, "
            "no medium switch (cartoon<->photoreal or 3D<->2D) mid-film"
        )
    elif any(
        k in blob
        for k in (
            "photoreal",
            "photorealistic",
            "live-action",
            "live action",
            "documentary",
            "写实",
            "逼真",
            "真人",
            "实拍",
            "纪录片",
        )
    ):
        lock["look"] = "photoreal cinematic — continuous color grade; never cartoon restyle"
        lock["lens"] = "35mm cinematic, soft background when close; no comic/grid UI"
        lock["palette"] = "match scene color temperature and wardrobe dyes across shots"
        lock["medium"] = "photoreal_cinematic"
    return {k: str(v)[:280] for k, v in lock.items()}


_VISUAL_STYLE_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:visual\s+style|art\s+style|style|视觉风格|画风)"
    r"(?:\*\*)?\s*[:：]\s*(.+?)\s*$"
)
_VISUAL_STYLE_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*(?:visual\s+style|art\s+style|style|视觉风格|画风)"
    r"\s*$\s*^\s*(?:[-*]\s*)?(.+?)\s*$"
)


def style_lock_from_brief(
    text: str,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Read the authored Brief/Storyboard style without inventing a leaf default."""
    body = str(text or "").strip()
    match = _VISUAL_STYLE_LINE_RE.search(body) or _VISUAL_STYLE_HEADING_RE.search(body)
    if match:
        authored = match.group(1).strip().strip("*").strip()
        inferred = default_style_lock(authored)
        # Preserve uncommon authored media that are more specific than our small
        # canonical vocabulary.
        if inferred.get("look") == STYLE_LOCK_DEFAULT.get("look") and not any(
            cue in authored.lower()
            for cue in ("cartoon", "animation", "animated", "卡通", "动画", "扁平", "圆润")
        ):
            inferred["look"] = authored[:280]
            inferred["medium"] = "brief_specified"
        return inferred
    if fallback:
        return {str(k): str(v)[:280] for k, v in fallback.items() if str(v).strip()}
    return default_style_lock(body)


def ensure_visual_style_statement(
    text: str,
    style_lock: dict[str, Any] | None,
) -> str:
    """Ensure an authored artifact visibly states its style authority."""
    body = str(text or "").rstrip()
    if _VISUAL_STYLE_LINE_RE.search(body) or _VISUAL_STYLE_HEADING_RE.search(body):
        return body
    style = style_lock if isinstance(style_lock, dict) else {}
    look = str(style.get("look") or style.get("medium") or "").strip()
    if not look:
        return body
    return (body + f"\n\n## Visual Style\n\n{look}\n").strip()


def synchronize_graph_style_from_brief(
    graph: dict[str, Any],
    text: str,
) -> dict[str, str]:
    """Make authored Brief/Storyboard style the single authority for media leaves."""
    meta = dict(graph.get("metadata") or {})
    analysis = (
        dict(meta.get("script_analysis") or {})
        if isinstance(meta.get("script_analysis"), dict)
        else {}
    )
    existing = (
        analysis.get("style_lock")
        if isinstance(analysis.get("style_lock"), dict)
        else meta.get("style_lock")
        if isinstance(meta.get("style_lock"), dict)
        else {}
    )
    user_style = default_style_lock(
        str(graph.get("description") or meta.get("user_prompt") or "")
    )
    # Never let a later authored artifact contradict an explicit user medium.
    # For prompts without a recognized style cue, Brief/Storyboard remains the
    # authority and may carry a richer Director inference.
    if user_style.get("look") != STYLE_LOCK_DEFAULT.get("look"):
        style = user_style
    else:
        style = style_lock_from_brief(text, fallback=existing)
    analysis["style_lock"] = dict(style)
    meta["script_analysis"] = analysis
    meta["style_lock"] = dict(style)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
            build_production_bible,
        )

        bible = build_production_bible(
            analysis,
            user_prompt=str(graph.get("description") or meta.get("user_prompt") or ""),
        )
        analysis["production_bible"] = bible
        meta["production_bible"] = bible
    except Exception:  # noqa: BLE001
        pass
    graph["metadata"] = meta
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or cfg.get("pipeline") or "").lower()
        if role in {"character", "character_design", "scene", "frame", "keyframe", "clip"}:
            cfg["style_lock"] = dict(style)
            node["config"] = cfg
    return style


def camera_rig_for_index(index: int, camera_hint: str = "") -> dict[str, Any]:
    """Pick a diverse angle; honor explicit front/side/back keywords in camera_hint."""
    hint = str(camera_hint or "").lower()
    preset = ANGLE_PRESETS[(max(1, int(index)) - 1) % len(ANGLE_PRESETS)]
    chosen = dict(preset)
    if "back" in hint or "reverse" in hint or "behind" in hint:
        chosen = dict(ANGLE_PRESETS[5])
    elif "ots" in hint or "over-the-shoulder" in hint or "over the shoulder" in hint:
        chosen = dict(ANGLE_PRESETS[4])
    elif "profile" in hint or "side" in hint:
        chosen = dict(ANGLE_PRESETS[3])
    elif "close" in hint:
        chosen = dict(ANGLE_PRESETS[2])
    elif "wide" in hint or "establish" in hint:
        chosen = dict(ANGLE_PRESETS[0])
    elif "low" in hint:
        chosen = dict(preset)
        chosen["elevation"] = -20
        chosen["label"] = f"{chosen['label']} (low angle)"
    elif "high" in hint or "bird" in hint:
        chosen = dict(preset)
        chosen["elevation"] = 45
        chosen["label"] = f"{chosen['label']} (high angle)"
    if hint and not any(
        k in hint for k in ("front", "back", "side", "ots", "wide", "close", "profile", "reverse")
    ):
        # Still diversify by index when hint is generic "medium".
        chosen = dict(ANGLE_PRESETS[(max(1, int(index)) - 1) % len(ANGLE_PRESETS)])
    return chosen


def enrich_shot_for_storyboard(
    shot: dict[str, Any],
    *,
    index: int,
    style_lock: dict[str, str] | None = None,
    scene_inventory: str = "",
) -> dict[str, Any]:
    """Stamp camera_rig / facing / scene_objects / style_lock onto a shot dict."""
    out = dict(shot)
    idx = int(out.get("shot_index") or index or 1)
    out["shot_index"] = idx
    camera = str(out.get("camera") or "").strip()
    rig = out.get("camera_rig") if isinstance(out.get("camera_rig"), dict) else None
    if not rig:
        rig = camera_rig_for_index(idx, camera)
        out["camera_rig"] = {
            "azimuth": int(rig.get("azimuth") or 0),
            "elevation": int(rig.get("elevation") or 0),
            "distance": float(rig.get("distance") or 1.0),
            "facing": str(rig.get("facing") or ""),
            "label": str(rig.get("label") or ""),
            "move": str(rig.get("move") or "static"),
        }
    if not camera or camera.lower() in {"medium", "medium / eye-level", "eye-level"}:
        out["camera"] = str(rig.get("label") or camera or "medium / eye-level")[:120]
    out.setdefault("facing", str((out.get("camera_rig") or {}).get("facing") or ""))
    if not str(out.get("scene_objects") or "").strip():
        out["scene_objects"] = (
            str(scene_inventory or "").strip()
            or "List visible locked landmarks for THIS framing from the master plate"
        )[:280]
    if style_lock:
        out["style_lock"] = {str(k): str(v)[:200] for k, v in style_lock.items() if str(v).strip()}
    return enrich_shot_wan_fields(out)


def qwen_angle_clause(shot: dict[str, Any] | None) -> str:
    if not isinstance(shot, dict):
        return ""
    rig = shot.get("camera_rig") if isinstance(shot.get("camera_rig"), dict) else {}
    if not rig:
        return ""
    return (
        f" QWEN CAMERA RIG: azimuth={rig.get('azimuth')}° "
        f"(0=front,90=right,180=back,270=left), elevation={rig.get('elevation')}°, "
        f"distance={rig.get('distance')} (0.6=CU,1.0=MS,1.4=wide); "
        f"facing={rig.get('facing') or shot.get('facing') or ''}."
    )


def wan3_clip_prompt_prefix(
    *,
    cast_names: list[str],
    camera: str,
    action: str,
    style_lock: dict[str, Any] | None = None,
    left_right: str = "",
    scene_ref_index: int | None = None,
    camera_move: str = "",
    speech_line: str = "",
    object_placements: str = "",
    motion_detail: str = "",
    locks_line: str = "",
    user_intent: str = "",
    brief_locks: str = "",
    ref_bindings: list[dict[str, Any]] | None = None,
) -> str:
    """Build Wan Omni prompt; scene specs is the last reference image when scene_ref_index set."""
    names = [str(n).strip() for n in cast_names if str(n).strip()]
    if ref_bindings:
        ref_line = format_image_ref_bindings(ref_bindings)
    else:
        refs = []
        for i, name in enumerate(names[:5], start=1):
            refs.append(f"Image {i} is the sole identity reference for {name}.")
        if not refs:
            refs.append("Image 1 is the sole identity reference for the on-screen subject.")
        scene_i = int(scene_ref_index) if scene_ref_index else (len(names[:5]) + 1)
        refs.append(
            f"Image {scene_i} is the SINGLE environment/scene specs — lock geography, props, and lighting."
        )
        ref_line = f"[References]: {' '.join(refs)}"
    style = style_lock if isinstance(style_lock, dict) else {}
    style_line = "; ".join(f"{k}={v}" for k, v in list(style.items())[:4]) or (
        "match the visual style specified by the brief and storyboard"
    )
    positions = left_right or (
        "Maintain fixed screen positions per storyboard; do not swap left/right."
    )
    move = camera_move or "static / slow pan / subtle dolly-in (no orbiting track that warps the set)"
    motion = motion_detail or action
    objects = object_placements or "Keep storyboard object placements; do not invent new landmarks."
    speech = speech_line or "Match ambient/speech intent for this shot when audio is enabled."
    layer = ""
    if user_intent or brief_locks:
        layer = (
            f"[Three-layer intent]: user={str(user_intent)[:220]}; "
            f"brief_locks={str(brief_locks)[:220]}.\n"
        )
    return (
        f"{layer}"
        f"[Composition]: {camera or 'cinematic shot'}; move={move}. "
        "If the camera moves, keep landmark L/R anchors stable every beat.\n"
        f"{ref_line}\n"
        f"[Positioning]: {positions}. Objects: {objects}. 180-degree rule; Fixed L/R anchors.\n"
        f"[Motion]: {motion}\n"
        f"[Speech/Audio]: {speech}\n"
        f"[Action beat]: {action}\n"
        + (f"[Locks]: {locks_line}\n" if locks_line else "")
        + f"[Style]: {style_line}."
    )


def enrich_shot_wan_fields(shot: dict[str, Any]) -> dict[str, Any]:
    """Ensure placement / motion / speech fields exist for Wan-direct clips."""
    out = dict(shot)
    action = str(out.get("action") or out.get("keyframe_prompt") or "").strip()
    out.setdefault("screen_positions", str(out.get("screen_positions") or out.get("facing") or "")[:160])
    out.setdefault(
        "object_placements",
        str(out.get("object_placements") or out.get("scene_objects") or "")[:220],
    )
    out.setdefault("motion_detail", str(out.get("motion_detail") or action)[:280])
    out.setdefault(
        "speech_line",
        str(out.get("speech_line") or out.get("dialogue") or out.get("speech") or "")[:280],
    )
    rig = out.get("camera_rig") if isinstance(out.get("camera_rig"), dict) else {}
    out.setdefault("camera_move", str(out.get("camera_move") or out.get("move") or rig.get("move") or "static")[:80])
    return out


def style_lock_clause(style_lock: dict[str, Any] | None) -> str:
    if not isinstance(style_lock, dict) or not style_lock:
        return ""
    medium = str(style_lock.get("medium") or "").strip()
    look = str(style_lock.get("look") or "").strip()
    forbid = str(style_lock.get("forbid") or "").strip()
    lens = str(style_lock.get("lens") or "").strip()
    palette = str(style_lock.get("palette") or "").strip()
    head = "STYLE LOCK (film-wide, non-negotiable): "
    if medium:
        head += f"medium={medium}. "
    if look:
        head += f"look={look}. "
    bits = []
    if lens:
        bits.append(f"lens={lens}")
    if palette:
        bits.append(f"palette={palette}")
    if forbid:
        bits.append(f"forbid={forbid}")
    # Explicit anti-drift for stylized / cartoon briefs.
    look_l = look.lower()
    if medium == "stylized_animation" or any(
        k in look_l for k in ("cartoon", "pixar", "animat", "stylized", "toon", "anime")
    ):
        bits.append(
            "FORBIDDEN mid-film: live-action photoreal, painterly restyle, "
            "or flattening soft-3D into flat 2D cel"
        )
    elif medium == "photoreal_cinematic" or any(
        k in look_l for k in ("photoreal", "live-action", "documentary")
    ):
        bits.append("FORBIDDEN mid-film: cartoon/anime/toon restyle")
    tail = ("; ".join(bits) + ".") if bits else ""
    return f" {head}{tail}"


def format_image_ref_bindings(bindings: list[dict[str, Any]] | None) -> str:
    """Explicit Image N → role map so multimodal models know which ref is which.

    ``bindings`` items: {index:int, role:str, name?:str}
    roles: first_frame | prior_keyframe | identity | environment
    """
    if not bindings:
        return ""
    lines: list[str] = []
    for raw in bindings:
        if not isinstance(raw, dict):
            continue
        try:
            idx = int(raw.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if idx < 1:
            continue
        role = str(raw.get("role") or "").strip().lower()
        name = str(raw.get("name") or "").strip()
        if role == "first_frame":
            lines.append(
                f"Image {idx} is the FIRST-FRAME keyframe — animate THIS exact image; "
                "preserve its art style, faces, wardrobe, architecture, and lighting."
            )
        elif role == "prior_keyframe":
            lines.append(
                f"Image {idx} is the PRIOR keyframe to EDIT — keep the same room geometry "
                "and style; only update pose/action/blocking for this shot."
            )
        elif role == "identity":
            who = name or "this cast member"
            lines.append(
                f"Image {idx} is the sole identity/costume reference for {who} "
                "(use face/hair/body/wardrobe only; ignore any backdrop in that sheet)."
            )
        elif role == "environment":
            lines.append(
                f"Image {idx} is the environment/scene specs — lock geography, furniture, "
                "windows/walls, props, and lighting."
            )
        else:
            label = name or role or "reference"
            lines.append(f"Image {idx} is the reference for {label}.")
    if not lines:
        return ""
    return "[References]: " + " ".join(lines)
