# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Spatial / motion continuity locks for keyframe and clip prompts."""

from __future__ import annotations

import re
from typing import Any


def infer_continuity_lock(action: str) -> dict[str, str]:
    """Derive a short motion/geography lock from shot action text (domain-agnostic)."""
    text = (action or "").lower()
    lock: dict[str, str] = {}
    if re.search(r"walk(?:s|ing)?\s+away|leave|leaving|exit(?:s|ing)?\b|depart", text):
        lock["motion"] = "subject_exits_away"
        lock["facing"] = "moving_away_from_speaker_or_camera_subject"
        lock["forbid"] = "do_not_walk_toward_the_speaker_or_toward_camera_subject"
    elif re.search(
        r"walk(?:s|ing)?\s+(?:towards|toward|to)\b|approach|approaching|come\s+(?:closer|near)",
        text,
    ):
        lock["motion"] = "subject_approaches"
        lock["facing"] = "moving_toward_speaker_or_camera_subject"
        lock["forbid"] = "do_not_walk_away_from_the_speaker"
    elif re.search(r"facing\s+away|back\s+to\s+(?:the\s+)?(?:camera|speaker)", text):
        lock["facing"] = "back_to_camera_or_speaker"
        lock["forbid"] = "do_not_face_toward_camera_unless_cut_requires"
    if re.search(r"behind\s+(?:the\s+)?camera|off[- ]?screen\s+voice|voice\s+off", text):
        lock["speaker_placement"] = "speaker_behind_or_off_camera"
    if re.search(r"speak(?:s|ing)?|talk(?:s|ing)?|say(?:s|ing)?|narrat", text):
        lock["audio_beat"] = "speech_present_in_beat"
    if not lock:
        lock["note"] = "keep_screen_direction_and_blocking_consistent_with_brief"
    return lock


def continuity_prompt_clause(lock: dict[str, str] | None) -> str:
    if not lock:
        return ""
    parts = [f"{k}={v}" for k, v in lock.items() if v]
    if not parts:
        return ""
    return (
        " CONTINUITY LOCK ("
        + "; ".join(parts)
        + "). Obey lock; do not reverse motion/facing."
    )


def merge_lock_with_previous(
    lock: dict[str, str], prev: dict[str, str] | None
) -> dict[str, str]:
    prev = prev or {}
    out = dict(lock)
    if out.get("motion") == "subject_exits_away" and prev.get("speaker_placement"):
        out["speaker_placement"] = prev["speaker_placement"]
    if prev.get("facing") and "facing" not in out and out.get("motion"):
        out.setdefault("screen_direction", prev.get("facing", ""))
    return out
