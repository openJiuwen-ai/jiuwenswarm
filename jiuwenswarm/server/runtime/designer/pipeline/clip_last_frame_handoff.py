# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Speech handoff between consecutive clips (anti-repeat).

Prior-clip last-frame media was removed: the images were never consumed as
Wan references (deliberately — they stole character1 and restaged a crop),
so the extraction/stamping side was dead code. What remains live is the
speech side: prior dialogue is stamped as already-delivered so the next clip
must not restate it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_speech(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().lower())
    raw = re.sub(r"[\"'`]+", "", raw)
    return raw.strip(" .,!?;:")


def speech_already_delivered(this_speech: str, prior_speech: str) -> bool:
    """True when this shot would restate prior dialogue (exact or contained)."""
    a = _normalize_speech(this_speech)
    b = _normalize_speech(prior_speech)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 12 and (a in b or b in a):
        return True
    # High token overlap for short lines.
    ta, tb = set(a.split()), set(b.split())
    if len(ta) >= 3 and len(tb) >= 3:
        overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
        if overlap >= 0.85:
            return True
    return False


def scrub_restated_speech(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clear this clip's speech fields when they duplicate previous_clip_speech."""
    cfg = dict(cfg or {})
    prior = str(cfg.get("previous_clip_speech") or "").strip()
    forbid = [
        str(x).strip()
        for x in (cfg.get("forbidden_speech") or [])
        if str(x).strip()
    ]
    if prior and prior not in forbid:
        forbid.append(prior)

    this_line = str(cfg.get("speech_line") or "").strip()
    by_char = (
        dict(cfg["speech_by_character"])
        if isinstance(cfg.get("speech_by_character"), dict)
        else {}
    )

    cleared = False
    if this_line and any(speech_already_delivered(this_line, p) for p in forbid):
        cfg["speech_line"] = ""
        cleared = True
    cleaned_by: dict[str, str] = {}
    for cid, line in by_char.items():
        text = str(line or "").strip()
        if text and any(speech_already_delivered(text, p) for p in forbid):
            cleared = True
            continue
        if text:
            cleaned_by[str(cid)] = text
    if by_char and cleaned_by != by_char:
        cfg["speech_by_character"] = cleaned_by
        if not cleaned_by:
            cfg["speech_line"] = ""
    elif cleared and not cleaned_by:
        cfg["speech_by_character"] = {}

    if cleared:
        cfg["speech_continuation_only"] = True
        done = [str(x) for x in (cfg.get("already_done") or []) if str(x).strip()]
        note = f"prior dialogue already spoken: {prior[:120]}" if prior else "prior dialogue already spoken"
        if note not in done:
            done.append(note)
        cfg["already_done"] = done
    cfg["forbidden_speech"] = forbid[:8]
    return cfg


def chain_prior_speech_across_clips(graph: dict[str, Any]) -> list[str]:
    """Stamp previous_clip_speech onto consecutive clips and scrub restatements.

    Call after Director/storyboard stamps speech_line onto clip configs so shot N+1
    does not restate dialogue already assigned to shot N.
    """
    notes: list[str] = []
    clips: list[tuple[int, dict[str, Any]]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        role = str(cfg.get("role") or "")
        nid = str(node.get("id") or "")
        if role not in {"clip", "video"} and not nid.startswith("n_clip_"):
            continue
        idx = int(cfg.get("clip_index") or 0) or 0
        if idx <= 0:
            continue
        clips.append((idx, node))
    clips.sort(key=lambda t: t[0])
    prev_speech = ""
    prev_id = ""
    prev_idx = 0
    for idx, node in clips:
        cfg = dict(node.get("config") or {})
        if prev_speech:
            cfg["previous_clip_speech"] = prev_speech[:280]
            forbid = [str(x) for x in (cfg.get("forbidden_speech") or []) if str(x)]
            if prev_speech not in forbid:
                forbid.append(prev_speech)
            cfg["forbidden_speech"] = forbid[:8]
            if prev_id and not str(cfg.get("previous_clip_node_id") or "").strip():
                cfg["previous_clip_node_id"] = prev_id
                cfg["previous_clip_clip_index"] = prev_idx
            before = str(cfg.get("speech_line") or "").strip()
            cfg = scrub_restated_speech(cfg)
            after = str(cfg.get("speech_line") or "").strip()
            if before and not after:
                notes.append(f"{node.get('id')}: scrubbed restated speech from shot {prev_idx}")
            else:
                notes.append(f"{node.get('id')}: prior speech forbid from shot {prev_idx}")
        node["config"] = cfg
        # Carry delivered (or continued) speech forward for the next clip.
        this_speech = str(cfg.get("speech_line") or "").strip()
        if this_speech:
            prev_speech = this_speech
        elif str(cfg.get("previous_clip_speech") or "").strip():
            prev_speech = str(cfg.get("previous_clip_speech") or "").strip()
        else:
            prev_speech = ""
        prev_id = str(node.get("id") or "")
        prev_idx = idx
    return notes


def stamp_speech_onto_next_clips(
    graph: dict[str, Any],
    *,
    completed_clip_id: str,
    clip_index: int,
    speech_line: str = "",
) -> list[str]:
    """After clip N completes: stamp its delivered speech onto the next clip(s).

    Speech-only successor of the removed last-frame stamping. The next clip
    must treat this line as already delivered and never restate it.
    """
    notes: list[str] = []
    speech = str(speech_line or "").strip()
    if not speech:
        return notes
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        if str(cfg.get("role") or "") not in {"clip", "video"} and not str(
            node.get("id") or ""
        ).startswith("n_clip_"):
            continue
        idx = int(cfg.get("clip_index") or 0) or 0
        nid = str(node.get("id") or "")
        if idx == int(clip_index or 0) or nid == completed_clip_id:
            continue
        is_next = idx == int(clip_index or 0) + 1
        cont = str(cfg.get("continuity_clip_node_id") or "") == str(completed_clip_id)
        if not (is_next or cont):
            continue
        cfg["previous_clip_speech"] = speech[:280]
        forbid = [str(x) for x in (cfg.get("forbidden_speech") or []) if str(x)]
        if speech not in forbid:
            forbid.append(speech)
        cfg["forbidden_speech"] = forbid[:8]
        cfg = scrub_restated_speech(cfg)
        node["config"] = cfg
        notes.append(f"{nid}: prior speech stamped from {completed_clip_id}")
    return notes
