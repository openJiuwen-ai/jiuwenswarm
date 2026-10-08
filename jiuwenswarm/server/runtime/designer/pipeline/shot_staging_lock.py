# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-shot staging locks: positioning, action, and cast relationships.

Clothing locks alone can dominate prompts and wash out where people stand, how they
pose, who they look at, and who talks to whom. These locks are shot-local (and
setting-aware) and must be injected into BOTH keyframes and clips at equal
priority with clothing — without changing soft clip concurrency or hard compose waits.
"""

from __future__ import annotations

import re
from typing import Any


def _name_map(characters: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for ch in characters or []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if cid:
            out[cid] = str(ch.get("name") or cid).strip() or cid
    return out


def _who(cid: str, names: dict[str, str]) -> str:
    return names.get(str(cid), str(cid))


def _shot_camera_bits(shot: dict[str, Any]) -> list[str]:
    bits: list[str] = []
    camera = str(shot.get("camera") or "").strip()
    if camera:
        bits.append(f"camera={camera[:160]}")
    view = str(shot.get("view_key") or "").strip()
    if view:
        bits.append(f"view={view}")
    framing = str(shot.get("camera_framing_rule") or "").strip()
    if framing:
        bits.append(f"framing={framing[:220]}")
    move = str(shot.get("move") or shot.get("camera_move") or "").strip()
    if move:
        bits.append(f"camera_move={move[:120]}")
    return bits


def build_positioning_lock(
    shot: dict[str, Any] | None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Where each on-screen person is, facing, and camera/view for THIS shot."""
    shot = shot if isinstance(shot, dict) else {}
    names = _name_map(characters)
    bits: list[str] = []
    setting = str(shot.get("setting_id") or "").strip()
    if setting:
        bits.append(f"setting={setting}")
    bits.extend(_shot_camera_bits(shot))

    blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    landmark = str(blocking.get("landmark") or "").strip()
    if landmark:
        bits.append(f"landmark={landmark[:120]}")
    positions = blocking.get("positions") if isinstance(blocking.get("positions"), list) else []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        cid = str(pos.get("character_id") or "").strip()
        who = str(pos.get("name") or _who(cid, names) or cid)
        zone = str(pos.get("zone") or "").strip() or "mid"
        facing = str(pos.get("facing") or "").strip() or "toward_action"
        pose = str(pos.get("pose") or "").strip()
        near = str(pos.get("near") or pos.get("beside") or "").strip()
        line = f"{who}@zone={zone} facing={facing}"
        if pose:
            line += f" pose={pose[:80]}"
        if near:
            line += f" near={near[:80]}"
        bits.append(line)

    screen = shot.get("screen_positions") or shot.get("screen_axis")
    if isinstance(screen, dict) and screen:
        bits.append(
            "screen="
            + "; ".join(f"{k}:{v}" for k, v in list(screen.items())[:8] if str(v).strip())[:220]
        )
    elif isinstance(screen, list):
        bits.append("screen=" + "; ".join(str(x)[:60] for x in screen[:6]))

    existing = str(shot.get("positioning_lock") or "").strip()
    if existing and len(bits) < 2:
        return existing[:720]
    if not bits:
        action = str(shot.get("action") or shot.get("shot_action") or "")[:160]
        return (
            f"place cast for this shot only"
            + (f" — {action}" if action else "")
        )[:480]
    return "; ".join(bits)[:720]


def build_action_lock(
    shot: dict[str, Any] | None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Posture / doing / motion for THIS shot — not clothing, not other shots."""
    shot = shot if isinstance(shot, dict) else {}
    names = _name_map(characters)
    bits: list[str] = []
    cast_actions = shot.get("cast_actions") if isinstance(shot.get("cast_actions"), dict) else {}
    occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
    if not cast_actions and isinstance(occ.get("cast_actions"), dict):
        cast_actions = occ["cast_actions"]
    on_screen = [
        str(x)
        for x in (
            shot.get("on_screen")
            or shot.get("character_ids")
            or occ.get("must_appear")
            or []
        )
        if str(x)
    ]
    for cid in on_screen or list(cast_actions.keys()):
        doing = str(cast_actions.get(cid) or "").strip()
        if doing:
            bits.append(f"{_who(cid, names)}: {doing[:180]}")
    beat = str(
        shot.get("shot_action")
        or shot.get("character_action")
        or shot.get("action")
        or ""
    ).strip()
    # Strip contaminated lock dumps from beat for a short action summary.
    for marker in ("CLOTHING LOCK", "POSITION:", "WARDROBE/FACE:", "PRIOR CLIP", "YOUR ASSIGNMENT"):
        if marker in beat.upper():
            beat = beat.split(marker, 1)[0].strip()
    if beat and not any(beat[:40] in b for b in bits):
        bits.append(f"beat={beat[:220]}")
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
            infer_pose_from_action,
        )

        pose_hint = infer_pose_from_action(
            " ".join(
                [
                    beat,
                    " ".join(str(cast_actions.get(c) or "") for c in (on_screen or [])),
                    str(shot.get("motion_detail") or ""),
                ]
            )
        )
        if pose_hint and pose_hint != "engaged_in_beat":
            bits.insert(0, f"beat_pose={pose_hint}")
    except Exception:  # noqa: BLE001
        pass
    motion = str(shot.get("motion_detail") or "").strip()
    if motion:
        bits.append(f"motion={motion[:200]}")
    posture = str(shot.get("posture") or "").strip()
    if posture:
        bits.append(f"posture={posture[:120]}")
    existing = str(shot.get("action_lock") or "").strip()
    if existing and not bits:
        return existing[:720]
    return "; ".join(bits)[:720] if bits else "hold storyboard shot posture for this shot only"


def build_relationship_lock(
    shot: dict[str, Any] | None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Who looks at whom, who is adjacent, who talks to whom — THIS shot only."""
    shot = shot if isinstance(shot, dict) else {}
    names = _name_map(characters)
    bits: list[str] = []

    rel = shot.get("relationship_lock")
    if isinstance(rel, str) and rel.strip():
        bits.append(rel.strip()[:400])
    elif isinstance(rel, dict):
        for k, v in rel.items():
            if str(v).strip():
                bits.append(f"{k}={str(v).strip()[:120]}")
    elif isinstance(rel, list):
        bits.extend(str(x)[:160] for x in rel if str(x).strip())

    looks = shot.get("looks_at") or shot.get("gaze") or shot.get("looking_at")
    if isinstance(looks, dict):
        for src, tgt in looks.items():
            bits.append(f"{_who(str(src), names)} looks_at={_who(str(tgt), names) if str(tgt) in names else str(tgt)[:80]}")
    elif isinstance(looks, list):
        for item in looks:
            if isinstance(item, dict):
                bits.append(
                    f"{_who(str(item.get('from') or item.get('who') or ''), names)} "
                    f"looks_at={item.get('to') or item.get('target') or item.get('at')}"
                )
            elif str(item).strip():
                bits.append(str(item)[:160])

    talking = shot.get("talking_to") or shot.get("dialogue_pairs")
    if isinstance(talking, dict):
        for src, tgt in talking.items():
            bits.append(
                f"{_who(str(src), names)} talks_to={_who(str(tgt), names) if str(tgt) in names else str(tgt)[:80]}"
            )
    speech = str(shot.get("speech_line") or "").strip()
    speaker = str(shot.get("speaker") or shot.get("speech_speaker") or "").strip()
    if speech:
        who = _who(speaker, names) if speaker else "speaker"
        bits.append(f"{who} speaks: {speech[:160]}")

    # Adjacency / facing from blocking.
    blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    positions = [p for p in (blocking.get("positions") or []) if isinstance(p, dict)]
    zone_people: dict[str, list[str]] = {}
    for pos in positions:
        cid = str(pos.get("character_id") or "").strip()
        who = str(pos.get("name") or _who(cid, names) or cid)
        zone = str(pos.get("zone") or "mid").strip().lower()
        facing = str(pos.get("facing") or "").strip()
        if facing:
            bits.append(f"{who} facing={facing[:100]}")
        zone_people.setdefault(zone, []).append(who)
        near = str(pos.get("near") or pos.get("beside") or "").strip()
        if near:
            bits.append(f"{who} next_to={near[:80]}")
    for zone, people in zone_people.items():
        if len(people) >= 2:
            bits.append(f"adjacent@{zone}: {' + '.join(people)}")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for b in bits:
        key = re.sub(r"\s+", " ", b).strip().lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(b)
    if not uniq:
        on_screen = [
            _who(str(x), names)
            for x in (shot.get("on_screen") or shot.get("character_ids") or [])
            if str(x)
        ]
        if len(on_screen) >= 2:
            return (
                f"keep spatial relations among {', '.join(on_screen)} for this shot; "
                "preserve who faces whom and who stands next to whom"
            )[:480]
        return "keep this shot's gaze/adjacency/dialogue relations"[:240]
    return "; ".join(uniq)[:720]


def enrich_shot_staging(
    shot: dict[str, Any],
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Stamp positioning/action/relationship locks onto the analysis shot."""
    if not isinstance(shot, dict):
        return {}
    positioning = build_positioning_lock(shot, characters)
    action = build_action_lock(shot, characters)
    relationship = build_relationship_lock(shot, characters)
    shot["positioning_lock"] = positioning
    shot["action_lock"] = action
    shot["relationship_lock"] = relationship
    return {
        "positioning_lock": positioning,
        "action_lock": action,
        "relationship_lock": relationship,
    }


def staging_priority_rule() -> str:
    return (
        "STAGING PRIORITY: positioning + action + relationship locks are EQUAL to clothing "
        "locks — FORBIDDEN: drop pose, placement, gaze, adjacency, or who-talks-to-whom "
        "to satisfy wardrobe; satisfy ALL locks for THIS shot only."
    )


def staging_lock_clause(
    *,
    positioning_lock: str = "",
    action_lock: str = "",
    relationship_lock: str = "",
    shot_index: int = 0,
    setting_id: str = "",
    for_clip: bool = False,
) -> str:
    """Prompt block for keyframes and clips."""
    where = "this clip (match storyboard contact poses — solos are identity only)" if for_clip else "this keyframe"
    scope = f"shot {shot_index}" if shot_index else "this shot"
    if setting_id:
        scope += f" / setting `{setting_id}`"
    lines = [f"STAGING LOCK ({where}, {scope}):"]
    if positioning_lock.strip():
        lines.append(f"- POSITIONING: {positioning_lock.strip()[:720]}")
    if action_lock.strip():
        lines.append(f"- ACTION/POSTURE: {action_lock.strip()[:720]}")
    if relationship_lock.strip():
        lines.append(f"- RELATIONSHIPS: {relationship_lock.strip()[:720]}")
    if len(lines) == 1:
        return ""
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
            contact_anti_penetration_clause,
        )

        lines.append(f"- {contact_anti_penetration_clause(for_clip=for_clip)}")
    except Exception:  # noqa: BLE001
        lines.append(
            "- CONTACT: bodies on support surfaces only — never intersect solid furniture/props."
        )
    lines.append(staging_priority_rule())
    return "\n".join(lines)


def ensure_cfg_staging_locks(
    cfg: dict[str, Any],
    *,
    shot: dict[str, Any] | None = None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Ensure node config carries per-shot staging locks; return combined clause."""
    if not isinstance(cfg, dict):
        return ""
    shot = shot if isinstance(shot, dict) else {}
    # Prefer shot enrichment, fall back to cfg fields.
    source = dict(shot) if shot else {}
    for key in (
        "blocking",
        "cast_actions",
        "occupancy",
        "camera",
        "view_key",
        "shot_action",
        "action",
        "speech_line",
        "speaker",
        "looks_at",
        "talking_to",
        "motion_detail",
        "setting_id",
        "on_screen",
        "character_ids",
        "positioning_lock",
        "action_lock",
        "relationship_lock",
        "camera_framing_rule",
        "screen_positions",
        "screen_axis",
    ):
        if key not in source and cfg.get(key) is not None:
            source[key] = cfg.get(key)
    locks = enrich_shot_staging(source, characters)
    cfg["positioning_lock"] = locks["positioning_lock"]
    cfg["action_lock"] = locks["action_lock"]
    cfg["relationship_lock"] = locks["relationship_lock"]
    if isinstance(source.get("blocking"), dict):
        cfg["blocking"] = source["blocking"]
    if isinstance(source.get("cast_actions"), dict):
        cfg["cast_actions"] = source["cast_actions"]
    return staging_lock_clause(
        positioning_lock=locks["positioning_lock"],
        action_lock=locks["action_lock"],
        relationship_lock=locks["relationship_lock"],
        shot_index=int(cfg.get("shot_index") or shot.get("shot_index") or 0),
        setting_id=str(cfg.get("setting_id") or shot.get("setting_id") or ""),
        for_clip=str(cfg.get("role") or "").lower() == "clip",
    )


def staging_locks_from_cfg(cfg: dict[str, Any] | None, *, for_clip: bool = False) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    return staging_lock_clause(
        positioning_lock=str(cfg.get("positioning_lock") or ""),
        action_lock=str(cfg.get("action_lock") or ""),
        relationship_lock=str(cfg.get("relationship_lock") or ""),
        shot_index=int(cfg.get("shot_index") or 0),
        setting_id=str(cfg.get("setting_id") or ""),
        for_clip=for_clip,
    )
