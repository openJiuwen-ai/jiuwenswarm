# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic movie continuity guide for Plan A (safe for any brief).

Not scene-specific: no church/dinner/valentine rules. Stamped into brief,
storyboard, keyframe, and clip prompts so every leaf shares one "production bible".
"""

from __future__ import annotations

from typing import Any


def build_movie_continuity_guide(
    *,
    style_lock: dict[str, Any] | None = None,
    spatial_lock: dict[str, Any] | None = None,
    cast_summary: str = "",
) -> str:
    """Compact production bible injected into every creative/media prompt."""
    style = style_lock if isinstance(style_lock, dict) else {}
    spatial = spatial_lock if isinstance(spatial_lock, dict) else {}
    look = str(style.get("look") or "match the user brief's stated visual medium").strip()
    forbid = str(style.get("forbid") or "no mid-film style switch").strip()
    setting = str(spatial.get("setting") or spatial.get("setting_type") or "per brief").strip()
    static = str(
        spatial.get("static_rule")
        or "landmarks, furniture, windows/walls, and light direction stay fixed unless the shot changes location"
    ).strip()
    cast = cast_summary.strip() or "use storyboard cast ids only"

    return (
        "=== MOVIE CONTINUITY GUIDE (obey on EVERY sheet/frame/clip) ===\n"
        f"1) STYLE ANCHOR: look={look}. {forbid}. "
        "If Image 1 / prior keyframe is 3D CGI, stay 3D CGI; if 2D illustration, stay 2D — "
        "never flatten, never photorealize, never restyle mid-film.\n"
        f"2) CAST ANCHOR: {cast}. One body per id; no face clones; "
        "shot on_screen list is authoritative — do not feature a different cast member as hero.\n"
        f"3) SET ANCHOR: setting={setting}. {static}. "
        "Same-setting edits must not morph windows<->cabinets<->walls.\n"
        "4) BLOCKING ANCHOR: honor storyboard zones (left/center/right/fg/bg) and landmark. "
        "Featured subjects face the landmark / action focus unless the shot says otherwise.\n"
        "5) SCREEN AXIS (180-degree): people on screen_left stay screen_left across pans/cuts "
        "unless the shot says they cross. Camera move reframes — it does not flip L/R seats.\n"
        "6) SETTING MASTER: first keyframe of a setting_id places ALL named cast for that "
        "setting clearly visible (bake seats/crowd now). Later same-setting keyframes EDIT "
        "that master (reframe/zoom/pose/exit) — do not erase people still in must_appear. "
        "New setting_id → new master. Solos = identity only (few Image-N); no scene specs. "
        "Partial occlusion of a placed person still = SAME identity (sex/age/hair/wardrobe).\n"
        "7) LANDMARKS: furniture/landmarks keep place vs the setting master still; "
        "do not teleport a landmark to mid-room or invent chairs.\n"
        "8) ASPECT: one ratio for the whole film (see aspect_lock); never square-crop mid-film.\n"
        "9) REFERENCE BINDING: when images are attached, prompt MUST name Image 1, Image 2, … "
        "matching attach order (identity sheets → environment).\n"
        "10) CLIP RULE: R2V animates from attached refs — motion/camera change, not redesign.\n"
        "11) LOCATION CHANGE: only when storyboard setting_id changes; then new compose from solos+scene, "
        "not a morph of the previous room.\n"
        "=== END GUIDE ==="
    )


def continuity_guide_clause(
    analysis: dict[str, Any] | None,
    *,
    max_chars: int = 1200,
) -> str:
    if not isinstance(analysis, dict):
        return ""
    chars = analysis.get("characters") or []
    bits: list[str] = []
    for ch in chars:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        name = str(ch.get("name") or cid).strip()
        if cid:
            bits.append(f"{cid}={name}")
    guide = build_movie_continuity_guide(
        style_lock=analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else None,
        spatial_lock=analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else None,
        cast_summary="; ".join(bits[:8]),
    )
    return guide[: max(200, int(max_chars))]
