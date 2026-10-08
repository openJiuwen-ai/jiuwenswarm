# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic shot consistency contract (storyboard + Director).

No scene/genre/prompt hardcodes. Continuity comes from:
  - prior same-setting storyboard rows (action / camera / speech)
  - compact structured end_state (not raw prior Wan paragraphs)

Leaf agents receive structured state only. Director hard-clears duplicate speech
and rejects prompts that restage finished beats.
"""

from __future__ import annotations

import re
from typing import Any


def _clip_nodes(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for n in (graph or {}).get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        if str(cfg.get("role") or "") == "clip":
            nodes.append(n)
    return sorted(
        nodes,
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0) or 99,
    )


def _same_setting(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    sa = str((a or {}).get("setting_id") or "").strip()
    sb = str((b or {}).get("setting_id") or "").strip()
    return bool(sa and sb and sa == sb)


def _short(text: str, *, limit: int = 180) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    return raw[:limit]


def collect_same_setting_prior_beats(
    graph: dict[str, Any] | None,
    *,
    shot_index: int,
    setting_id: str = "",
    this_cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Earlier clip rows in the same setting — storyboard fields only."""
    out: list[dict[str, Any]] = []
    sid = str(setting_id or (this_cfg or {}).get("setting_id") or "").strip()
    for node in _clip_nodes(graph):
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0 or idx >= int(shot_index or 0):
            continue
        if sid and str(cfg.get("setting_id") or "").strip() != sid:
            continue
        if this_cfg is not None and sid and not _same_setting(cfg, this_cfg):
            continue
        action = str(
            cfg.get("shot_action") or cfg.get("character_action") or cfg.get("action") or ""
        ).strip()
        speech = str(cfg.get("speech_line") or "").strip()
        by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
        if not speech and by_char:
            speech = " ".join(str(v).strip() for v in by_char.values() if str(v).strip())
        camera = str(cfg.get("camera") or "").strip()
        if not action and not speech and not camera:
            continue
        out.append(
            {
                "node_id": str(node.get("id") or ""),
                "shot_index": idx,
                "shot_action": _short(action, limit=220),
                "speech_line": _short(speech, limit=200),
                "camera": _short(camera, limit=120),
                "setting_id": str(cfg.get("setting_id") or "").strip(),
            }
        )
    return out


def storyboard_already_done_notes(priors: list[dict[str, Any]] | None) -> list[str]:
    """Compact already_done notes from prior storyboard shots (not Wan prose)."""
    notes: list[str] = []
    for p in priors or []:
        if not isinstance(p, dict):
            continue
        idx = int(p.get("shot_index") or 0) or "?"
        action = _short(str(p.get("shot_action") or ""), limit=120)
        speech = _short(str(p.get("speech_line") or ""), limit=100)
        if action:
            note = f"shot {idx} already filmed: {action}"
            if note not in notes:
                notes.append(note)
        if speech:
            note = f"shot {idx} dialogue already spoken: {speech}"
            if note not in notes:
                notes.append(note)
    return notes[:16]


def accumulate_forbidden_speech(
    cfg: dict[str, Any],
    priors: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Union prior same-setting speech into forbidden_speech (cfg-only)."""
    out = dict(cfg or {})
    forbid = [
        str(x).strip()
        for x in (out.get("forbidden_speech") or [])
        if str(x).strip()
    ]
    prior_one = str(out.get("previous_clip_speech") or "").strip()
    if prior_one and prior_one not in forbid:
        forbid.append(prior_one)
    for p in priors or []:
        line = str((p or {}).get("speech_line") or "").strip()
        if line and line not in forbid:
            forbid.append(line)
    out["forbidden_speech"] = forbid[:12]
    return out


def enforce_speech_uniqueness(
    cfg: dict[str, Any],
    *,
    graph: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Hard-clear this shot's speech when it duplicates any earlier same-setting line.

    Domain-agnostic: uses fuzzy speech match only — no content rules.
    """
    from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
        scrub_restated_speech,
        speech_already_delivered,
    )

    out = dict(cfg or {})
    notes: list[str] = []
    shot_index = int(out.get("shot_index") or 0) or 0
    priors = collect_same_setting_prior_beats(
        graph,
        shot_index=shot_index,
        setting_id=str(out.get("setting_id") or ""),
        this_cfg=out,
    )
    out = accumulate_forbidden_speech(out, priors)
    before = str(out.get("speech_line") or "").strip()
    out = scrub_restated_speech(out)
    after = str(out.get("speech_line") or "").strip()
    if before and not after:
        notes.append("cleared_duplicate_speech")
        out["include_speech"] = False
        out["speech_continuation_only"] = True

    # Also clear when this line matches any prior storyboard speech even if
    # previous_clip_speech was missing (first scrub path).
    this_line = str(out.get("speech_line") or "").strip()
    forbid = [str(x).strip() for x in (out.get("forbidden_speech") or []) if str(x).strip()]
    if this_line and any(speech_already_delivered(this_line, p) for p in forbid):
        out["speech_line"] = ""
        out["speech_by_character"] = {}
        out["include_speech"] = False
        out["speech_continuation_only"] = True
        if "cleared_duplicate_speech" not in notes:
            notes.append("cleared_duplicate_speech")

    by_char = (
        dict(out["speech_by_character"])
        if isinstance(out.get("speech_by_character"), dict)
        else {}
    )
    cleaned: dict[str, str] = {}
    for cid, line in by_char.items():
        text = str(line or "").strip()
        if text and any(speech_already_delivered(text, p) for p in forbid):
            if "cleared_duplicate_speech" not in notes:
                notes.append("cleared_duplicate_speech")
            continue
        if text:
            cleaned[str(cid)] = text
    if by_char and cleaned != by_char:
        out["speech_by_character"] = cleaned
        if not cleaned:
            out["speech_line"] = ""
            out["include_speech"] = False
    return out, notes


def merge_storyboard_continuity(
    cfg: dict[str, Any],
    *,
    graph: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Stamp already_done / forbidden_speech / end_state from prior storyboard rows."""
    out = dict(cfg or {})
    notes: list[str] = []
    shot_index = int(out.get("shot_index") or 0) or 0
    if shot_index <= 0:
        return out, notes
    priors = collect_same_setting_prior_beats(
        graph,
        shot_index=shot_index,
        setting_id=str(out.get("setting_id") or ""),
        this_cfg=out,
    )
    if not priors:
        out, speech_notes = enforce_speech_uniqueness(out, graph=graph)
        return out, speech_notes

    done = [str(x).strip() for x in (out.get("already_done") or []) if str(x).strip()]
    for note in storyboard_already_done_notes(priors):
        if note not in done:
            done.append(note)
            notes.append("storyboard_already_done")
    out["already_done"] = done[:16]

    # Compact beat_done / pose_holds from storyboard actions (positive holds).
    beat_done = [str(x).strip() for x in (out.get("beat_done") or []) if str(x).strip()]
    holds = [str(x).strip() for x in (out.get("pose_holds") or []) if str(x).strip()]
    for p in priors:
        action = _short(str(p.get("shot_action") or ""), limit=140)
        if not action:
            continue
        beat_note = f"shot {p.get('shot_index')}: {action}"
        if beat_note not in beat_done:
            beat_done.append(beat_note)
        # The previous action stays on already_done for the continuity check.
        # It is not an opening hold: quoting it makes the next clip replay that tail.
    out["beat_done"] = beat_done[:16]
    out["pose_holds"] = holds[:8]

    latest = priors[-1]
    out["previous_clip_action"] = str(
        out.get("previous_clip_action") or latest.get("shot_action") or ""
    )[:220]
    if latest.get("speech_line") and not str(out.get("previous_clip_speech") or "").strip():
        out["previous_clip_speech"] = str(latest.get("speech_line"))[:200]
    if not out.get("previous_clip_shot_index"):
        out["previous_clip_shot_index"] = int(latest.get("shot_index") or 0)
    out["previous_clip_handoff_ready"] = True

    # Structured end_state for leaf/Director — never the full prior Wan body.
    out["end_state"] = {
        "from_shot_index": int(latest.get("shot_index") or 0),
        "action_done": _short(str(latest.get("shot_action") or ""), limit=160),
        "speech_done": _short(str(latest.get("speech_line") or ""), limit=120),
        "camera_was": _short(str(latest.get("camera") or ""), limit=100),
        "already_done": list(out.get("already_done") or [])[:12],
        "pose_holds": list(out.get("pose_holds") or [])[:6],
        "seat_anchors": out.get("seat_anchors") if isinstance(out.get("seat_anchors"), dict) else {},
        "exited_ids": [
            str(x) for x in (out.get("exited_ids") or []) if str(x).strip()
        ][:12],
        "forbidden_speech": list(out.get("forbidden_speech") or [])[:8],
    }
    notes.append("stamped_end_state_from_storyboard")

    out, speech_notes = enforce_speech_uniqueness(out, graph=graph)
    notes.extend(speech_notes)
    return out, notes


def agent_structured_continuity_block(cfg: dict[str, Any] | None) -> str:
    """Leaf-facing continuity — structured fields only (no prior Wan dump)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lines: list[str] = []
    end = cfg.get("end_state") if isinstance(cfg.get("end_state"), dict) else {}
    idx = end.get("from_shot_index") or cfg.get("previous_clip_shot_index") or ""
    action_done = str(
        end.get("action_done") or cfg.get("previous_clip_action") or ""
    ).strip()
    if action_done or end or cfg.get("already_done") or cfg.get("pose_holds"):
        lines.append(
            f"CONTINUITY STATE (prior shot {idx} finished — structured only; "
            "write a NEW prompt for THIS storyboard row; never paste prior Wan text):"
        )
    if action_done:
        lines.append(
            "- Prior action is already finished. Open on the resulting still, then play only this shot."
        )
    speech_done = str(
        end.get("speech_done") or cfg.get("previous_clip_speech") or ""
    ).strip()
    if speech_done:
        lines.append(f"- Prior speech already delivered: \"{speech_done[:160]}\"")
    done = [str(x).strip() for x in (cfg.get("already_done") or end.get("already_done") or []) if str(x).strip()]
    if done:
        lines.append("- Already done: " + "; ".join(done[:8]))
    holds = [str(x).strip() for x in (cfg.get("pose_holds") or end.get("pose_holds") or []) if str(x).strip()]
    holds = [
        hold
        for hold in holds
        if "already past:" not in hold.lower()
        and "after this shot" not in hold.lower()
        and (not action_done or action_done.lower() not in hold.lower())
    ]
    if holds:
        lines.append("- Opening holds: " + "; ".join(holds[:6]))
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    if not seats and isinstance(end.get("seat_anchors"), dict):
        seats = end["seat_anchors"]
    if seats:
        bits = []
        for cid, anchor in list(seats.items())[:6]:
            if isinstance(anchor, dict):
                bits.append(f"{cid}@{anchor.get('zone') or anchor.get('place') or 'held'}")
            else:
                bits.append(f"{cid}@{anchor}")
        if bits:
            lines.append("- Seat holds: " + ", ".join(bits))
    exited = [
        str(x)
        for x in (cfg.get("exited_ids") or end.get("exited_ids") or [])
        if str(x).strip()
    ]
    if exited:
        lines.append("- Exited (omit until returned): " + ", ".join(exited[:8]))
    forbid = [
        str(x)
        for x in (cfg.get("forbidden_speech") or end.get("forbidden_speech") or [])
        if str(x).strip()
    ]
    if forbid:
        lines.append(
            "- Forbidden speech (do not restate): "
            + "; ".join(f'"{x[:80]}"' for x in forbid[:4])
        )
    this_action = str(cfg.get("shot_action") or "").strip()
    this_cam = str(cfg.get("camera") or "").strip()
    this_speech = str(cfg.get("speech_line") or "").strip()
    if this_action or this_cam or this_speech:
        assign = f"- THIS shot assignment: action={_short(this_action) or '(see storyboard)'}"
        if this_cam:
            assign += f"; camera={_short(this_cam, limit=80)}"
        if this_speech:
            assign += '; speech="' + _short(this_speech, limit=100) + '"'
        else:
            assign += "; speech=(silent)"
        lines.append(assign)
    return "\n".join(lines)


def prompt_restates_forbidden_speech(prompt: str, cfg: dict[str, Any] | None) -> bool:
    """True when free-form prompt embeds dialogue already marked forbidden."""
    from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
        speech_already_delivered,
    )

    body = str(prompt or "")
    if not body:
        return False
    cfg = cfg if isinstance(cfg, dict) else {}
    forbid = [
        str(x).strip()
        for x in (cfg.get("forbidden_speech") or [])
        if str(x).strip()
    ]
    prior = str(cfg.get("previous_clip_speech") or "").strip()
    if prior and prior not in forbid:
        forbid.append(prior)
    # Quoted spans + bare forbidden lines.
    quotes = re.findall(r'["“”]([^"“”]{4,200})["“”]', body)
    candidates = list(quotes)
    # Also check "says: …" clauses without quotes.
    for m in re.finditer(r"(?i)\bsays?\s*:\s*(.+?)(?:\.|$)", body):
        candidates.append(m.group(1).strip())
    for cand in candidates:
        if any(speech_already_delivered(cand, p) for p in forbid):
            return True
    # Whole-prompt containment for short forbidden lines.
    for p in forbid:
        if len(p) >= 12 and speech_already_delivered(body, p):
            return True
    return False


def prompt_violates_continuity(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Hard Director reasons to force rewrite from this storyboard row."""
    reasons: list[str] = []
    cfg = cfg if isinstance(cfg, dict) else {}
    body = str(prompt or "").strip()
    if not body:
        return reasons

    speech_bad = prompt_restates_forbidden_speech(body, cfg)
    if speech_bad:
        reasons.append("restates_forbidden_speech")

    prior_bias = False
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            overlap_ratio,
        )

        prior_act = str(cfg.get("previous_clip_action") or "").strip()
        this_act = str(cfg.get("shot_action") or "").strip()
        if (
            prior_act
            and this_act
            and overlap_ratio(prior_act, this_act) < 0.4
            and overlap_ratio(body, prior_act) >= 0.45
            and overlap_ratio(body, this_act) < 0.35
        ):
            reasons.append("films_prior_action_not_this_row")
            prior_bias = True
    except Exception:  # noqa: BLE001
        pass

    # Replay heuristic alone is noisy (shared names/verbs). Require speech or
    # prior-action bias before hard-rejecting.
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
            agent_replays_finished_events,
        )

        if agent_replays_finished_events(
            body,
            already_done=[str(x) for x in (cfg.get("already_done") or []) if str(x)],
            this_action=str(cfg.get("shot_action") or ""),
        ) and (speech_bad or prior_bias):
            reasons.append("replays_finished_events")
    except Exception:  # noqa: BLE001
        pass
    return reasons


def apply_continuity_contract(
    cfg: dict[str, Any],
    *,
    graph: dict[str, Any] | None = None,
    prompt: str = "",
) -> tuple[dict[str, Any], str, list[str]]:
    """Full Director gate: merge storyboard continuity, scrub speech, flag prompt."""
    out, notes = merge_storyboard_continuity(cfg, graph=graph)
    reasons = prompt_violates_continuity(prompt, cfg=out) if prompt else []
    if reasons:
        notes.extend(f"reject:{r}" for r in reasons)
    return out, prompt, notes
