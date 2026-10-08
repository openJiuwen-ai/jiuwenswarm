# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Director shot-sheet helpers — second-by-second craft, domain-agnostic.

DeepSeek-Flash has no 3D spatial priors; sheets stay in natural language and
keyframe prompts must put REPOSE / camera / facing BEFORE identity locks.
"""

from __future__ import annotations

import re
from typing import Any

_SPEECH_QUOTE_RE = re.compile(
    r"""["“]([^"”]{3,160})["”]|'([^']{3,120})'|says?\s*[:,]?\s*["“]?([^"”.\n]{3,120})""",
    re.I,
)


def _guess_speech_line(shot: dict[str, Any]) -> str:
    existing = str(shot.get("speech_line") or shot.get("dialogue") or shot.get("speech") or "").strip()
    if existing:
        return existing[:280]
    blob = " ".join(
        str(shot.get(k) or "")
        for k in ("action", "keyframe_prompt", "title", "comment")
    )
    m = _SPEECH_QUOTE_RE.search(blob)
    if not m:
        return ""
    return next((g for g in m.groups() if g), "").strip()[:280]


def _timeline_seconds(timeline: str) -> tuple[float, float]:
    """Parse '0-5s' / '0:00-0:07' → (start, end) seconds; default 0–5."""
    t = (timeline or "").strip().lower()
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:s|sec)?\s*[-–—to]+\s*(\d+(?:\.\d+)?)\s*(?:s|sec)?",
        t,
    )
    if m:
        return float(m.group(1)), float(m.group(2))
    m2 = re.search(r"(\d+):(\d+)\s*[-–—]\s*(\d+):(\d+)", t)
    if m2:
        a = int(m2.group(1)) * 60 + int(m2.group(2))
        b = int(m2.group(3)) * 60 + int(m2.group(4))
        return float(a), float(b)
    return 0.0, 5.0


def stamp_director_shot_sheets(
    analysis: dict[str, Any],
    *,
    prompt: str = "",
) -> dict[str, Any]:
    """Ensure each shot has speech/motion/wardrobe/face/position craft fields."""
    out = analysis if isinstance(analysis, dict) else {}
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]

    for shot in shots:
        focus = [str(x) for x in (shot.get("character_ids") or shot.get("on_screen") or []) if str(x)]
        all_ids = [str(c.get("id")) for c in characters if c.get("id")]
        off = [c for c in all_ids if c not in set(focus)]
        # Same-setting others who exist in the world but should be off-camera this shot.
        shot["off_camera_cast_ids"] = off
        shot["on_camera_cast_ids"] = list(focus)

        # Wardrobe / face locks from character descriptions (director bible).
        look_bits: list[str] = []
        for cid in focus:
            ch = by_id.get(cid) or {}
            name = str(ch.get("name") or cid)
            desc = str(ch.get("description") or "").strip()
            attrs = ch.get("identity_attrs") if isinstance(ch.get("identity_attrs"), dict) else {}
            hair = str(attrs.get("facial_hair") or attrs.get("hair") or "").strip()
            glasses = str(attrs.get("glasses") or "").strip()
            sex = str(attrs.get("sex") or "").strip()
            age = str(attrs.get("age_band") or "").strip()
            wardrobe = str(
                attrs.get("wardrobe")
                or attrs.get("outfit")
                or attrs.get("dress_color")
                or ""
            ).strip()
            micro = ", ".join(x for x in (sex, age, hair, glasses, wardrobe) if x)
            bit = f"{name}: {desc[:140]}".strip()
            if micro:
                bit = f"{bit}; {micro}" if desc else f"{name}: {micro}"
            look_bits.append(bit[:220])
        if look_bits and not str(shot.get("wardrobe_lock") or "").strip():
            shot["wardrobe_lock"] = " | ".join(look_bits)[:520]

        action = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
        timeline = str(shot.get("timeline") or "0-5s")
        t0, t1 = _timeline_seconds(timeline)
        dur = max(1.0, t1 - t0)
        if not str(shot.get("motion_detail") or "").strip():
            shot["motion_detail"] = (
                f"Beat sheet ({timeline}, ~{dur:.0f}s): "
                f"0–{max(1, int(dur * 0.3))}s establish pose/gaze/weight; "
                f"mid play action — {action[:160]}; "
                f"final ~{max(1, int(dur * 0.2))}s hold for cut. "
                "Continuous body weight; no teleport; screen L/R seats stable."
            )[:480]

        speech = _guess_speech_line(shot)
        shot["speech_line"] = speech  # empty = intentional silence (no invented TTS)

        if not str(shot.get("camera_framing_rule") or "").strip():
            on_names = [
                str((by_id.get(c) or {}).get("name") or c) for c in focus
            ]
            off_names = [
                str((by_id.get(c) or {}).get("name") or c) for c in off
            ]
            is_master = (
                str(shot.get("keyframe_strategy") or "") == "master_still"
                or bool(shot.get("ensemble_master"))
            )
            if is_master:
                ens = [
                    str((by_id.get(c) or {}).get("name") or c)
                    for c in (shot.get("ensemble_cast_ids") or focus)
                    if str(c)
                ]
                shot["camera_framing_rule"] = (
                    f"SETTING MASTER FRAMING: show ALL of {', '.join(ens) or 'ensemble'} "
                    "clearly readable in this establishing still; bake seats/crowd now."
                )[:360]
            elif off_names and on_names:
                shot["camera_framing_rule"] = (
                    f"CAMERA FRAMING: frame so ONLY {', '.join(on_names)} are visible on camera. "
                    f"Keep {', '.join(off_names)} OFF-CAMERA (outside frame / behind camera / "
                    f"occluded by set) for this shot — they still exist in the same setting, "
                    f"do not delete them from the world, just do not show them."
                )[:420]
            else:
                shot["camera_framing_rule"] = (
                    f"CAMERA FRAMING: keep featured subjects ({', '.join(on_names) or 'on_screen'}) "
                    "clearly readable; do not invent extra named leads."
                )[:320]

        # Fold into action once for leaf agents (keep language lock).
        framing = str(shot.get("camera_framing_rule") or "")
        if framing and framing[:40] not in str(shot.get("action") or ""):
            shot["action"] = (str(shot.get("action") or "") + " " + framing)[:700]
        wardrobe = str(shot.get("wardrobe_lock") or "")
        if wardrobe and "WARDROBE/FACE:" not in str(shot.get("action") or ""):
            shot["action"] = (
                str(shot.get("action") or "") + f" WARDROBE/FACE: {wardrobe}"
            )[:750]
        motion = str(shot.get("motion_detail") or "")
        if motion and "MOTION:" not in str(shot.get("action") or ""):
            shot["action"] = (str(shot.get("action") or "") + f" MOTION: {motion}")[:800]
        if speech and "SPEECH:" not in str(shot.get("action") or ""):
            shot["action"] = (str(shot.get("action") or "") + f" SPEECH: {speech}")[:850]

        # Blocking zones for DeepSeek→Qwen (natural language, not fake 3D coords).
        blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
        positions = blocking.get("positions") if isinstance(blocking.get("positions"), list) else []
        if positions and "POSITION:" not in str(shot.get("action") or ""):
            pos_txt = "; ".join(
                f"{(p or {}).get('name') or (p or {}).get('character_id')}@"
                f"{(p or {}).get('zone') or 'mid'} facing={(p or {}).get('facing') or 'action'}"
                for p in positions
                if isinstance(p, dict)
            )[:200]
            if pos_txt:
                shot["action"] = (
                    str(shot.get("action") or "") + f" POSITION: {pos_txt}"
                )[:900]

        try:
            from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
                enrich_shot_staging,
            )

            enrich_shot_staging(shot, characters)
        except Exception:  # noqa: BLE001
            pass

    out["shots"] = shots
    out.setdefault("director_contract", {})
    if isinstance(out["director_contract"], dict):
        out["director_contract"]["shot_sheets"] = "speech/motion/wardrobe/framing.v2"
        out["director_contract"]["prompt"] = (prompt or "")[:200]
    return out


def repose_first_prefix(*, camera: str = "", facing: str = "", zone: str = "") -> str:
    """Qwen edit / DeepSeek-authored prompts: spatial instructions BEFORE identity."""
    bits = [
        "REPOSE FIRST (before identity):",
        f"camera/body orientation={camera or 'match shot camera'}",
    ]
    if facing:
        bits.append(f"facing={facing}")
    if zone:
        bits.append(f"place in zone={zone}")
    bits.append(
        "State body direction and screen placement, THEN preserve face/wardrobe from refs."
    )
    return " ".join(bits)


def format_clip_framing_clause(shot: dict[str, Any] | None, analysis: dict[str, Any] | None) -> str:
    shot = shot if isinstance(shot, dict) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    by_id = {
        str(c.get("id")): c
        for c in (analysis.get("characters") or [])
        if isinstance(c, dict) and c.get("id")
    }
    on = [str(x) for x in (shot.get("on_camera_cast_ids") or shot.get("character_ids") or []) if str(x)]
    off = [str(x) for x in (shot.get("off_camera_cast_ids") or []) if str(x)]
    on_n = [str((by_id.get(c) or {}).get("name") or c) for c in on]
    off_n = [str((by_id.get(c) or {}).get("name") or c) for c in off]
    parts = [
        str(shot.get("camera_framing_rule") or "").strip(),
        str(shot.get("motion_detail") or "").strip(),
        str(shot.get("wardrobe_lock") or "").strip(),
    ]
    if on_n:
        parts.append(f"ON CAMERA (must stay readable): {', '.join(on_n)}.")
    if off_n:
        parts.append(
            f"OFF CAMERA this shot (exist in set, not framed): {', '.join(off_n)} — "
            "do not invent them into frame; do not erase them from continuity. "
            "Name them only in text if useful; R2V does not attach their solos as refs."
        )
    speech = str(shot.get("speech_line") or "").strip()
    if speech:
        parts.append(f"SPEECH: {speech}")
    return " ".join(p for p in parts if p)[:900]
