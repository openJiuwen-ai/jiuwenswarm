# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prior-shot consistency as storyboard notes — never paste full Wan prompts.

Format injected into leaves:
  PREVIOUS CLIP HAD … (context only — do not film again)
  YOUR ASSIGNMENT (storyboard shot N — film ONLY this)
"""

from __future__ import annotations

import re
from typing import Any


def _clip_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        if str(cfg.get("role") or "") == "clip":
            nodes.append(n)
    return sorted(
        nodes,
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0) or 99,
    )


def wan_prompt_from_clip_node(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    for key in ("last_wan_prompt", "clip_prompt_preview", "agent_authored_prompt_text"):
        text = str(cfg.get(key) or "").strip()
        if text and key != "agent_authored_prompt_text":
            return text
    gen = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    return str(gen.get("prompt") or cfg.get("prompt") or "").strip()


def _short_action(text: str, *, limit: int = 220) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    for marker in (
        "PRIOR CLIP",
        "PRIOR KEYFRAME",
        "YOUR ASSIGNMENT",
        "PREVIOUS CLIP HAD",
        "LANGUAGE LOCK",
        "STYLE LOCK",
        "SCENE SPECS",
        "CHARACTER CONSISTENCY",
        "MASTER SCENE",
    ):
        if marker in raw.upper():
            raw = raw.split(marker, 1)[0].strip()
    for pat in (
        r"Primary action for shot \d+:\s*(.+?)(?:\.|$)",
        r"YOUR ASSIGNMENT[\s\S]*?Action:\s*(.+?)(?:\n|$)",
        r"Character action:\s*(.+?)(?:\n|$)",
        r"Action:\s*(.+?)(?:\n|$)",
    ):
        m = re.search(pat, raw, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            break
    return raw[:limit]


def _action_from_clip_node(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    for key in ("shot_action", "character_action", "action", "previous_clip_action"):
        text = str(cfg.get(key) or "").strip()
        if text and key != "previous_clip_action":
            return _short_action(text)
    return _short_action(wan_prompt_from_clip_node(node))


def collect_prior_clip_prompts(
    graph: dict[str, Any],
    *,
    shot_index: int,
    max_chars_each: int = 1600,
    max_clips: int = 6,
) -> list[dict[str, Any]]:
    """Earlier clips as short storyboard shots (oldest → newest)."""
    del max_chars_each
    out: list[dict[str, Any]] = []
    for node in _clip_nodes(graph):
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0 or idx >= int(shot_index or 0):
            continue
        action = _action_from_clip_node(node)
        prompt = wan_prompt_from_clip_node(node)
        if not action and not prompt:
            continue
        out.append(
            {
                "node_id": str(node.get("id") or ""),
                "shot_index": idx,
                "wan_prompt": (prompt or "")[:400],  # readiness/debug only
                "shot_action": (action or "")[:220],
                "speech_line": str(cfg.get("speech_line") or "")[:200],
                "camera": str(cfg.get("camera") or "")[:120],
            }
        )
    return out[-max(1, int(max_clips)) :]


def stamp_wan_prompt_handoff(
    graph: dict[str, Any],
    *,
    shot_index: int,
    prompt: str,
    node_id: str = "",
    shot_action: str = "",
    speech_line: str = "",
) -> list[str]:
    """Save Wan prompt on this clip only (debug/regenerate).

    Continuity is storyboard start/end — do NOT stamp prior Wan onto later clips.
    """
    notes: list[str] = []
    text = (prompt or "").strip()
    if not text and not shot_action:
        return notes
    meta = dict(graph.get("metadata") or {})
    log = dict(meta.get("clip_wan_prompt_log") or {})
    key = str(node_id or f"n_clip_{shot_index}")
    src = next((n for n in _clip_nodes(graph) if str(n.get("id") or "") == key), None)
    src_cfg = src.get("config") if isinstance(src, dict) else {}
    action = _short_action(shot_action or _action_from_clip_node(src) or text)
    speech = (speech_line or str((src_cfg or {}).get("speech_line") or "")).strip()
    log[key] = {
        "shot_index": int(shot_index or 0),
        "prompt": text[:4000],
        "shot_action": action[:220],
        "speech_line": speech[:200],
    }
    meta["clip_wan_prompt_log"] = log
    graph["metadata"] = meta

    for node in _clip_nodes(graph):
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("shot_index") or 0) or 0
        nid = str(node.get("id") or "")
        if idx == int(shot_index or 0) or nid == key:
            if text:
                cfg["last_wan_prompt"] = text[:4000]
                cfg["clip_prompt_preview"] = text[:1200]
            if action:
                cfg["shot_action"] = cfg.get("shot_action") or action[:300]
            cfg["handoff_artifact_ready"] = True
            # Drop legacy prior-clip schedule wiring if present on older graphs.
            cfg.pop("continuity_clip_node_id", None)
            node["config"] = cfg
            notes.append(f"{nid}: saved last_wan_prompt (no next-clip Wan stamp)")
    return notes


def same_scene_prompt_gate_clause(
    *,
    shot_index: int = 0,
    this_action: str = "",
    this_camera: str = "",
    this_speech: str = "",
    already_done: list[str] | None = None,
    has_prior: bool = False,
) -> str:
    """LLM instruction: agree with this storyboard row; continue; do not unasked-repeat."""
    lines = [
        "SAME-SCENE CONSISTENCY GATE (write the Wan prompt to this contract):",
        f"1) THIS storyboard shot {int(shot_index or 0) or 'N'} is the plot authority "
        f"— action: {_short_action(this_action) or '(this row)'}"
        + (f"; camera: {str(this_camera).strip()[:120]}" if str(this_camera or "").strip() else "")
        + (f"; speech: {str(this_speech).strip()[:160]}" if str(this_speech or "").strip() else "")
        + ".",
        "2) The Wan prompt MUST agree with that row (blocking, who is on screen, what "
        "happens in THIS window). Do not film another shot's beat.",
    ]
    if has_prior:
        lines.append(
            "3) Continue from the previous clip in this SAME setting: read "
            "already_done / pose_holds / seat_anchors / forbidden_speech / end_state "
            "as finished story STATE, then write a NEW motion prompt for THIS row only. "
            "Do not paste or restage prior Wan text."
        )
        lines.append(
            "4) Do NOT repeat actions / onsets / exits / walk-aways / dialogue / reseating "
            "that already finished unless THIS storyboard row or the user prompt "
            "explicitly asks to repeat them."
        )
    else:
        lines.append(
            "3) This is the first clip of the setting: animate the composed scene master; "
            "do not restart later plot."
        )
    done = [str(x) for x in (already_done or []) if str(x).strip()]
    if done:
        lines.append("ALREADY FINISHED (do not redo unless this row asks): " + "; ".join(done[:8]))
    lines.append(
        "Keep clothing, language, occupancy, and seat locks. Identity/place from attached media."
    )
    return "\n".join(lines)


_EVENT_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"walk", "walks", "walked", "walking", "away"}),
    frozenset({"leave", "leaves", "leaving", "left", "exit", "exits", "exited", "exiting", "depart", "gone"}),
    frozenset({"sit", "sits", "sat", "sitting", "seated", "reseated", "reseat"}),
    frozenset({"start", "starts", "started", "starting", "begin", "begins", "began", "onset"}),
    frozenset({"turn", "turns", "turned", "turning", "look", "looks", "looked", "looking", "gaze", "gazes", "glance"}),
    frozenset({"reach", "reaches", "reached", "check", "checks", "checked", "grab", "grabs", "pick", "open", "opens"}),
    frozenset({"crowd", "colleagues", "coworker", "coworkers", "extras", "bystanders", "staff"}),
)


def _stem_token(token: str) -> str:
    t = str(token or "").strip().lower()
    if len(t) <= 3:
        return t
    for suf in ("ing", "ied", "ies", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            stem = t[: -len(suf)]
            if stem.endswith("k") or len(stem) >= 3:
                return stem
    return t


def _content_stems(text: str) -> set[str]:
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            content_tokens,
        )
    except Exception:  # noqa: BLE001
        return { _stem_token(x) for x in re.findall(r"[A-Za-z]{2,}", str(text or "").lower()) }
    return { _stem_token(t) for t in content_tokens(text) }


def _families_in(text: str) -> set[int]:
    tokens = set(re.findall(r"[A-Za-z]{2,}", str(text or "").lower()))
    stems = { _stem_token(t) for t in tokens }
    bag = tokens | stems
    hit: set[int] = set()
    for i, fam in enumerate(_EVENT_FAMILIES):
        fam_stems = { _stem_token(x) for x in fam } | set(fam)
        if bag & fam_stems:
            hit.add(i)
    return hit


def agent_replays_finished_events(
    text: str,
    *,
    already_done: list[str] | None = None,
    this_action: str = "",
) -> bool:
    """True when the agent restages a finished event this shot did not ask to repeat."""
    body = str(text or "").strip()
    if not body:
        return False
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            overlap_ratio,
        )
    except Exception:  # noqa: BLE001
        overlap_ratio = None  # type: ignore[assignment]
    asked = _families_in(this_action)
    for note in already_done or []:
        note_s = str(note or "").strip()
        if len(note_s) < 12:
            continue
        if overlap_ratio is not None and overlap_ratio(note_s, this_action) >= 0.45:
            continue
        if overlap_ratio is not None and overlap_ratio(note_s, body) >= 0.45:
            # This row asked to repeat that finished beat — allow it.
            if overlap_ratio(note_s, this_action) >= 0.35:
                continue
            return True
        this_stems = _content_stems(this_action)
        note_stems = _content_stems(note_s) - this_stems
        body_stems = _content_stems(body) - this_stems
        shared = note_stems & body_stems
        denom = float(max(1, min(len(note_stems), len(body_stems))))
        if len(shared) >= 2 and (len(shared) / denom) >= 0.4:
            return True
        note_fams = _families_in(note_s) - asked
        if note_fams and note_fams & _families_in(body):
            return True
    return False


def _same_setting(src: dict[str, Any] | None, dst: dict[str, Any] | None) -> bool:
    """True only when both sides name the same setting_id.

    Missing IDs do not count as a match — that would leak continuity across rooms.
    """
    a = str((src or {}).get("setting_id") or "").strip()
    b = str((dst or {}).get("setting_id") or "").strip()
    if not a or not b:
        return False
    return a == b


def this_shot_assignment_clause(
    *,
    shot_index: int,
    action: str = "",
    camera: str = "",
    speech_line: str = "",
    already_done: list[str] | None = None,
) -> str:
    lines = [
        f"YOUR ASSIGNMENT (storyboard shot {int(shot_index or 0)} — film ONLY this part):",
        f"- Action: {_short_action(action) or '(follow this shot keyframe + storyboard shot)'}",
    ]
    if str(camera or "").strip():
        lines.append(f"- Camera: {str(camera).strip()[:160]}")
    if str(speech_line or "").strip():
        lines.append(f"- Speech this shot: {str(speech_line).strip()[:200]}")
    done = [str(x) for x in (already_done or []) if str(x).strip()]
    if done:
        lines.append("- Already finished earlier (do not redo): " + "; ".join(done[:8]))
    lines.append(
        "Use the storyboard shot assigned to you above. Do not invent another shot's action."
    )
    return "\n".join(lines)


def previous_clip_had_clause(prior: list[dict[str, Any]] | None) -> str:
    items = [p for p in (prior or []) if isinstance(p, dict)]
    if not items:
        return ""
    latest = items[-1]
    beat = _short_action(str(latest.get("shot_action") or latest.get("wan_prompt") or ""))
    lines = [
        "PREVIOUS CLIP HAD THE FOLLOWING (context only — do NOT film / redo this):",
        f"- Shot {latest.get('shot_index')} ({latest.get('node_id')}): {beat or '(prior beat)'}",
    ]
    if latest.get("speech_line"):
        lines.append(
            f"- Prior speech already delivered (do NOT restate): "
            f"\"{str(latest.get('speech_line'))[:160]}\""
        )
        lines.append(
            "Dialogue continuity: do not restart that line; only speak NEW words for This shot "
            "(or stay silent if this shot has no new speech_line)."
        )
    if len(items) > 1:
        earlier = "; ".join(
            f"shot {p.get('shot_index')}: {_short_action(str(p.get('shot_action') or ''), limit=80)}"
            for p in items[:-1]
            if p.get("shot_action") or p.get("shot_index")
        )
        if earlier:
            lines.append(f"- Earlier clips already covered: {earlier}")
    lines.append(
        "Do not restart finished onsets/exits/dialogue from the previous clip "
        "unless THIS storyboard row or the user prompt explicitly asks for a repeat."
    )
    lines.append(
        "Write a NEW motion prompt that AGREES with YOUR ASSIGNMENT (this storyboard "
        "shot) and CONTINUES from structured continuity state (already_done / holds / "
        "forbidden_speech). Never paste prior Wan prose into this clip's Wan call."
    )
    return "\n".join(lines)


def handoff_clause_for_prompt(
    prior: list[dict[str, Any]] | None,
    *,
    this_shot_index: int = 0,
    this_action: str = "",
    this_camera: str = "",
    this_speech: str = "",
    already_done: list[str] | None = None,
) -> str:
    """Previous-clip summary + this-shot assignment — never paste full prior Wan text."""
    parts: list[str] = []
    prev = previous_clip_had_clause(prior)
    if prev:
        parts.append(prev)
    if int(this_shot_index or 0) > 0 or str(this_action or "").strip():
        parts.append(
            this_shot_assignment_clause(
                shot_index=int(this_shot_index or 0),
                action=this_action,
                camera=this_camera,
                speech_line=this_speech,
                already_done=already_done,
            )
        )
    if prior:
        parts.append(
            same_scene_prompt_gate_clause(
                shot_index=int(this_shot_index or 0),
                this_action=this_action,
                this_camera=this_camera,
                this_speech=this_speech,
                already_done=already_done,
                has_prior=True,
            )
        )
    return "\n\n".join(parts)


def keyframe_continuity_note(
    *,
    shot_index: int,
    this_action: str = "",
    this_camera: str = "",
    prior_action: str = "",
    prior_shot_index: int | None = None,
) -> str:
    """Same pattern for keyframes — no full prior generate.prompt paste."""
    parts: list[str] = []
    if prior_action.strip():
        src = f"shot {prior_shot_index}" if prior_shot_index else "prior keyframe"
        parts.append(
            "PREVIOUS KEYFRAME HAD THE FOLLOWING (context only — do NOT redraw that beat):\n"
            f"- {src}: {_short_action(prior_action)}\n"
            "Advance time; do not restart finished onsets unless storyboard asks."
        )
    parts.append(
        this_shot_assignment_clause(
            shot_index=shot_index,
            action=this_action,
            camera=this_camera,
        ).replace("film ONLY", "draw ONLY").replace("Film ONLY", "Draw ONLY")
    )
    return "\n\n".join(parts)
