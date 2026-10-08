# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Film-wide production lock bible — stamped into brief/storyboard and leaf prompts.

Domain-agnostic: style, landmarks, lighting, crowd, speech, axis, aspect.
Leaves MUST read this before calling image/video models. Director may rewrite
generate.prompt to stay faithful to brief + storyboard + user prompt.
"""

from __future__ import annotations

from typing import Any


def _style_block(style_lock: dict[str, Any] | None) -> str:
    s = style_lock if isinstance(style_lock, dict) else {}
    if not s:
        return "STYLE LOCK: match user brief medium; no mid-film restyle."
    bits = []
    for k in ("medium", "look", "lens", "palette", "forbid"):
        v = str(s.get(k) or "").strip()
        if v:
            bits.append(f"{k}={v}")
    return "STYLE LOCK: " + "; ".join(bits) if bits else "STYLE LOCK: coherent film medium."


def _spatial_block(spatial: dict[str, Any] | None) -> str:
    sp = spatial if isinstance(spatial, dict) else {}
    lines: list[str] = []
    for k in (
        "setting",
        "architecture",
        "static_rule",
        "crowd_rule",
        "light",
        "landmark_rule",
        "forbid_bleed",
    ):
        v = str(sp.get(k) or "").strip()
        if v:
            lines.append(f"- {k}: {v}")
    lms = sp.get("landmarks") or sp.get("landmark_placements") or []
    if isinstance(lms, list) and lms:
        if lms and isinstance(lms[0], dict):
            bits = [
                f"{x.get('name')}@{x.get('where') or x.get('zone') or 'fixed'}"
                for x in lms
                if isinstance(x, dict) and x.get("name")
            ]
            if bits:
                lines.append("- landmark_placements: " + "; ".join(bits[:8]))
        else:
            lines.append("- landmarks: " + ", ".join(str(x) for x in lms[:8] if str(x).strip()))
    lighting = sp.get("lighting_lock") if isinstance(sp.get("lighting_lock"), dict) else {}
    if lighting:
        lines.append(
            "- lighting_lock: "
            + "; ".join(f"{k}={v}" for k, v in lighting.items() if str(v).strip())
        )
    return "SPATIAL / LIGHTING / CROWD LOCKS:\n" + ("\n".join(lines) if lines else "- per brief")


def _speech_table(shots: list[dict[str, Any]], characters: list[dict[str, Any]]) -> str:
    by_id = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    rows = ["| Shot | On camera | Speech (exact) | Motion beat |", "| --- | --- | --- | --- |"]
    for sh in shots:
        if not isinstance(sh, dict):
            continue
        idx = int(sh.get("shot_index") or 0) or "?"
        on = [str(x) for x in (sh.get("on_camera_cast_ids") or sh.get("character_ids") or []) if str(x)]
        names = ", ".join(by_id.get(c, c) for c in on) or "—"
        speech = str(sh.get("speech_line") or "").strip() or "(silent)"
        motion = str(sh.get("motion_detail") or sh.get("action") or "")[:90]
        rows.append(f"| {idx} | {names} | {speech[:120]} | {motion} |")
    return "LANGUAGE / SPEECH LOCK (use these exact lines at leaf nodes):\n" + "\n".join(rows)


def build_production_bible(
    analysis: dict[str, Any] | None,
    *,
    user_prompt: str = "",
    max_chars: int = 4500,
) -> str:
    """Compact LOCK BIBLE for brief + storyboard + leaf context."""
    a = analysis if isinstance(analysis, dict) else {}
    style = a.get("style_lock") if isinstance(a.get("style_lock"), dict) else {}
    spatial = a.get("spatial_lock") if isinstance(a.get("spatial_lock"), dict) else {}
    axis = a.get("axis_lock") if isinstance(a.get("axis_lock"), dict) else {}
    aspect = a.get("aspect_lock") if isinstance(a.get("aspect_lock"), dict) else {}
    shots = [s for s in (a.get("shots") or []) if isinstance(s, dict)]
    characters = [c for c in (a.get("characters") or []) if isinstance(c, dict)]

    cast_lines: list[str] = []
    for ch in characters:
        cid = str(ch.get("id") or "")
        name = str(ch.get("name") or cid)
        desc = str(ch.get("description") or ch.get("costume_lock") or "")[:160]
        attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
        micro = ", ".join(
            f"{k}={v}"
            for k, v in attrs.items()
            if k in {"sex", "age_band", "facial_hair", "glasses", "wardrobe"} and str(v).strip()
        )
        cast_lines.append(f"- {cid}={name}: {desc}" + (f" [{micro}]" if micro else ""))

    intent = str(user_prompt or a.get("user_prompt") or "").strip()
    parts = [
        "=== PRODUCTION LOCK BIBLE (obey on EVERY leaf; do not invent overrides) ===",
        (
            "USER INTENT (lock cue — keep character / wardrobe / place / language / "
            "opening blocking; Wan still films only THIS storyboard window): "
            + (intent[:500] if intent else "see brief")
        ),
        "PLOT RULE: each clip's MOTION is only its storyboard row / timeline window. "
        "Copy ALL locks below (cast, clothing, first-clip positions, language, speech, "
        "style, landmarks) into every leaf prompt. Do not drop locks. Do not paste "
        "later shots or the full remaining plot into this Wan call.",
        _style_block(style),
        (
            f"ASPECT LOCK: ratio={aspect.get('ratio') or 'per brief'}; "
            f"image={aspect.get('image_size') or ''}; video={aspect.get('video_size') or ''}."
        ),
        (
            "AXIS LOCK: "
            + str(axis.get("rule") or "180-degree screen L/R stable across pans/cuts.")
            + (
                " canonical="
                + ", ".join(f"{k}@{v}" for k, v in (axis.get("canonical_screen_axis") or {}).items())
                if isinstance(axis.get("canonical_screen_axis"), dict)
                else ""
            )
        ),
        "CAST IDENTITY LOCKS (one body per id):",
        "\n".join(cast_lines) if cast_lines else "- per brief",
        _spatial_block(spatial),
        _speech_table(shots, characters),
        "LANDMARK RULE: named landmarks keep the SAME screen-side "
        "and place vs the scene specs across all same-setting keyframes and clips — "
        "never teleport a landmark mid-film.",
        "CROWD RULE: if extras exist, keep the SAME silhouette layout across "
        "same-setting shots (do not empty then reinvent a new crowd).",
        "EXIT RULE: once a character leaves a setting, omit them from later same-setting "
        "prompts until the storyboard returns them — never spawn cast from nowhere.",
        "LEAF PROTOCOL: (1) read_upstream brief + storyboard (2) copy locks above into "
        "your visual prompt (3) then call image/video tools — never tool-first. "
        "Clip video calls: positive story form only — no negatives, no examples.",
        "=== END LOCK BIBLE ===",
    ]
    text = "\n".join(parts)
    return text[: max(800, int(max_chars))]


def leaf_lock_packet(
    *,
    role: str,
    shot_index: int = 0,
    analysis: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-node lock packet injected into leaf agent JSON context."""
    a = analysis if isinstance(analysis, dict) else {}
    m = meta if isinstance(meta, dict) else {}
    style = (
        a.get("style_lock")
        if isinstance(a.get("style_lock"), dict)
        else m.get("style_lock") if isinstance(m.get("style_lock"), dict) else {}
    )
    spatial = (
        a.get("spatial_lock")
        if isinstance(a.get("spatial_lock"), dict)
        else m.get("spatial_lock") if isinstance(m.get("spatial_lock"), dict) else {}
    )
    shots = {
        int(s.get("shot_index") or 0): s
        for s in (a.get("shots") or [])
        if isinstance(s, dict)
    }
    shot = shots.get(int(shot_index or 0)) if shot_index else None
    shot = shot if isinstance(shot, dict) else {}
    bible = str(m.get("production_bible") or a.get("production_bible") or "").strip()
    if not bible:
        bible = build_production_bible(a, user_prompt=str(m.get("user_prompt") or ""))
    return {
        "must_read_before_tools": True,
        "role": str(role or ""),
        "shot_index": int(shot_index or 0) or None,
        "style_lock": style,
        "spatial_lock": {
            k: v
            for k, v in (spatial or {}).items()
            if k in {
                "setting",
                "architecture",
                "static_rule",
                "crowd_rule",
                "light",
                "lighting_lock",
                "landmarks",
                "landmark_placements",
                "landmark_rule",
                "forbid_bleed",
            }
        },
        "speech_line": str(shot.get("speech_line") or ""),
        "motion_detail": str(shot.get("motion_detail") or "")[:400],
        "wardrobe_lock": str(shot.get("wardrobe_lock") or ""),
        "camera_framing_rule": str(shot.get("camera_framing_rule") or ""),
        "on_camera_cast_ids": list(shot.get("on_camera_cast_ids") or shot.get("character_ids") or []),
        "off_camera_cast_ids": list(shot.get("off_camera_cast_ids") or []),
        "blocking": shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {},
        "production_bible_excerpt": bible[:2800],
    }


def append_bible_to_markdown(md: str, bible: str) -> str:
    body = (md or "").rstrip()
    b = (bible or "").strip()
    if not b:
        return body
    if "PRODUCTION LOCK BIBLE" in body:
        return body
    return (body + "\n\n## Production Lock Bible\n\n" + b + "\n")[:12000]


def enforce_locks_on_generate_prompt(
    prompt: str,
    *,
    style_lock: dict[str, Any] | None = None,
    spatial_lock: dict[str, Any] | None = None,
    speech_line: str = "",
    motion_detail: str = "",
    wardrobe_lock: str = "",
    framing_rule: str = "",
    landmark_clause: str = "",
    role: str = "",
    max_chars: int = 3200,
) -> str:
    """Director: fold missing locks into a leaf generate.prompt (deterministic)."""
    p = (prompt or "").strip()
    additions: list[str] = []

    style_bit = _style_block(style_lock)
    if style_bit and "STYLE LOCK" not in p and "STYLE HOLD" not in p:
        if str(role or "").lower() == "clip":
            medium = str((style_lock or {}).get("medium") or "")
            look = str((style_lock or {}).get("look") or "")[:120]
            additions.append(
                f"STYLE HOLD (non-negotiable): match Image 1 art medium exactly "
                f"({medium or look or 'brief style'}). FORBIDDEN: mid-clip restyle."
            )
        else:
            additions.append(style_bit)

    sp = spatial_lock if isinstance(spatial_lock, dict) else {}
    static = str(sp.get("static_rule") or "").strip()
    if static and "STATIC OBJECTS" not in p and "SPATIAL LOCK" not in p:
        additions.append(static[:280])
    crowd = str(sp.get("crowd_rule") or "").strip()
    if crowd and "CROWD" not in p.upper() and "congregation" not in p.lower():
        additions.append(f"CROWD/EXTRAS: {crowd[:220]}")
    lighting = sp.get("lighting_lock") if isinstance(sp.get("lighting_lock"), dict) else {}
    if lighting and "LIGHTING LOCK" not in p and "key_direction" not in p:
        additions.append(
            "LIGHTING LOCK: "
            + "; ".join(f"{k}={v}" for k, v in lighting.items() if str(v).strip())[:240]
        )
    light = str(sp.get("light") or "").strip()
    if light and "LIGHTING LOCK" not in " ".join(additions) and "light direction" not in p.lower():
        additions.append(f"LIGHT: {light[:160]}")

    if landmark_clause and landmark_clause[:40] not in p:
        additions.append(landmark_clause[:280])
    elif sp.get("landmarks") or sp.get("landmark_placements"):
        clause = "LANDMARK LOCK: keep named landmarks fixed vs scene specs (no teleport)."
        if "LANDMARK" not in p:
            additions.append(clause)

    if framing_rule and framing_rule[:36] not in p:
        additions.append(framing_rule[:280])
    if wardrobe_lock and "WARDROBE" not in p and "Costume lock" not in p:
        additions.append(f"WARDROBE/FACE: {wardrobe_lock[:240]}")
    if motion_detail and "MOTION:" not in p and str(role or "").lower() == "clip":
        additions.append(f"MOTION: {motion_detail[:240]}")
    if speech_line and "SPEECH:" not in p:
        additions.append(f"SPEECH (exact): {speech_line[:200]}")
    elif str(role or "").lower() in {"clip", "speech"} and not speech_line and "SPEECH:" not in p:
        additions.append("SPEECH: silent beat (do not invent dialogue).")

    if not additions:
        return p[:max_chars]
    return (p + "\n" + "\n".join(additions)).strip()[:max_chars]


def landmark_clause_from_analysis(
    analysis: dict[str, Any] | None, shot: dict[str, Any] | None = None
) -> str:
    a = analysis if isinstance(analysis, dict) else {}
    sp = a.get("spatial_lock") if isinstance(a.get("spatial_lock"), dict) else {}
    shot = shot if isinstance(shot, dict) else {}
    blk = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    primary = str(blk.get("landmark") or "").strip()
    places = sp.get("landmark_placements") if isinstance(sp.get("landmark_placements"), list) else []
    bits: list[str] = []
    if primary:
        bits.append(f"primary landmark='{primary}' stays fixed in place/screen-side")
    for item in places[:6]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        where = str(item.get("where") or item.get("zone") or "").strip()
        if name:
            bits.append(f"{name}@{where or 'fixed'}")
    lms = sp.get("landmarks") if isinstance(sp.get("landmarks"), list) else []
    for lm in lms[:4]:
        if str(lm).strip() and str(lm) not in " ".join(bits):
            bits.append(f"{lm}@locked")
    if not bits:
        return ""
    return (
        "LANDMARK LOCK: "
        + "; ".join(bits)
        + " — do not move/resize/teleport these across same-setting keyframes/clips."
    )
