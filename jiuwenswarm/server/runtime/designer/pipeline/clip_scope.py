# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-clip plot scope: this storyboard window only — never the full user prompt.

A clip keeps the duration of its own storyboard timeline, snapped onto the
configured video model's ``min_sec``–``max_sec``. The film's total runtime is a
planning target only; compose concatenates whatever the clips add up to.
"""

from __future__ import annotations

import re
from typing import Any


_STOP = frozenset(
    """
    a an the and or of to in on at for with from into over after before
    this that these those then while still also just as by is are was were
    be been being it its they them their he she his her we our you your
    video film Shot scene camera cinematic photoreal please create make
    generate want need 的 了 在 和 与 并 将 把 是 一段 视频 镜头 分镜
    """.split()
)

_COVERAGE_RE = re.compile(
    r"\b(?:multi[- ]cams?|multi[- ]camera|coverage|"
    r"same moment|different angles?|multiple angles?|several angles?)\b|"
    r"多机位|同一时刻",
    re.I,
)


def clamp_clip_duration(seconds: int | float | None, *, default: int = 5) -> int:
    """Clamp a clip to the configured video model's duration capacity."""
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        active_video_capacity,
        snap_duration,
    )

    value = seconds if seconds is not None else default
    return snap_duration(value, active_video_capacity())


def timeline_seconds(timeline: str) -> float | None:
    """Length of a ``start-end`` storyboard window, or None when it does not parse."""
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", timeline or "")]
    if len(nums) >= 2 and nums[1] > nums[0]:
        return nums[1] - nums[0]
    return None


def duration_from_timeline(timeline: str, *, default: int = 5) -> int:
    seconds = timeline_seconds(timeline)
    return clamp_clip_duration(seconds if seconds is not None else default, default=default)


def user_asked_coverage(prompt: str) -> bool:
    return bool(_COVERAGE_RE.search(str(prompt or "")))


ANGLE_VIEWS = frozenset({"front", "left", "right", "side", "top", "bottom"})


def clear_unrequested_angle_view(shot: dict[str, Any], *, coverage: bool) -> None:
    """Sequential time windows are not front/left/right restages."""
    if coverage or not isinstance(shot, dict):
        return
    relation = str(shot.get("shot_relation") or "").strip().lower()
    if relation == "angle_variant":
        return
    vk = str(shot.get("view_key") or "").strip().lower()
    if vk in ANGLE_VIEWS:
        shot["view_key"] = ""
    bible = shot.get("scene_specs")
    if isinstance(bible, dict) and str(bible.get("active_view") or "").strip().lower() in ANGLE_VIEWS:
        bible = dict(bible)
        bible["active_view"] = "sequence"
        shot["scene_specs"] = bible


def action_covers_whole_prompt(action: str, user_prompt: str) -> bool:
    """True when this row is the entire user story, including short prompts under 20 tokens."""
    body = str(action or "").strip()
    user = str(user_prompt or "").strip()
    if not body:
        return True
    if looks_like_full_story_restatement(body, user):
        return True
    act = content_tokens(body)
    if len(act) < 8:
        return False
    return user_prompt_coverage(body, user) >= 0.72


def _duration_only_line(text: str) -> bool:
    """Drop 'Make a 30 second film' so it is not filmed as a story beat."""
    raw = str(text or "").strip()
    if not raw:
        return True
    from jiuwenswarm.server.runtime.designer.media_intent import prompt_duration_sec

    if prompt_duration_sec(raw) and len(content_tokens(raw)) <= 4:
        return True
    return False


def window_beat(prompt: str, clip_index: int, shot_count: int) -> str:
    """Action text for one sequential time window (1-based clip_index)."""
    beats = _beat_list_from_prompt(prompt, max(1, int(shot_count or 1)))
    i = max(0, int(clip_index or 1) - 1)
    if i < len(beats):
        return str(beats[i] or "").strip()
    return str(beats[-1] if beats else "this time window only").strip()


def content_tokens(text: str) -> set[str]:
    raw = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", (text or "").lower())
    return {t for t in raw if t not in _STOP}


def overlap_ratio(a: str, b: str) -> float:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(max(1, min(len(ta), len(tb))))


def user_prompt_coverage(action: str, user_prompt: str) -> float:
    """Fraction of user-prompt content tokens that also appear in action."""
    up = content_tokens(user_prompt)
    act = content_tokens(action)
    if not up or not act:
        return 0.0
    return len(act & up) / float(len(up))


def looks_like_full_story_restatement(text: str, user_prompt: str) -> bool:
    """True when *text* replays the whole user prompt, not one detailed beat.

    A storyboard row / Wan shot that shares tokens with the brief (wardrobe,
    place, names) is expected — that is not a restatement.
    """
    body = str(text or "").strip()
    user = str(user_prompt or "").strip()
    if not body or not user:
        return False
    if body[:180].casefold() == user[:180].casefold() and len(body) >= 40 and len(user) >= 40:
        return True
    up = content_tokens(user)
    act = content_tokens(body)
    if len(up) < 12:
        return overlap_ratio(body, user) >= 0.85 and len(act) >= max(8, int(len(up) * 0.7))
    # Require covering most of the user prompt AND being almost as long as it.
    if user_prompt_coverage(body, user) >= 0.72 and len(act) >= max(20, int(len(up) * 0.6)):
        return True
    return False


def looks_like_prior_copy(text: str, prior: str) -> bool:
    body = str(text or "").strip()
    old = str(prior or "").strip()
    if not body or not old or len(body) < 40:
        return False
    if body[:180].casefold() == old[:180].casefold():
        return True
    return overlap_ratio(body, old) >= 0.72 and user_prompt_coverage(body, old) >= 0.5


def first_sentence(text: str, *, limit: int = 220) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    if not raw:
        return ""
    m = re.split(r"(?<=[.!?。！？])\s+", raw, maxsplit=1)
    return (m[0] if m else raw)[:limit]


def trim_restated_action(action: str, user_prompt: str, *, other_actions: list[str] | None = None) -> str:
    text = str(action or "").strip()
    if not text:
        return ""
    if looks_like_full_story_restatement(text, user_prompt):
        text = first_sentence(text, limit=180)
        if looks_like_full_story_restatement(text, user_prompt):
            # Keep a short unique remainder vs the user prompt.
            up = content_tokens(user_prompt)
            kept = [w for w in re.findall(r"\S+", text) if w.lower().strip(".,") not in up]
            text = " ".join(kept)[:180] if kept else first_sentence(action, limit=120)
    for other in other_actions or []:
        if other and overlap_ratio(text, other) >= 0.75:
            uniq = [w for w in re.findall(r"\S+", text) if w.lower() not in content_tokens(other)]
            if uniq:
                text = " ".join(uniq)[:180]
            break
    return text.strip()[:500]


def clip_assignment_text(
    *,
    clip_index: int,
    action: str = "",
    camera: str = "",
    speech_line: str = "",
    duration_sec: int = 5,
    on_screen: list[str] | None = None,
) -> str:
    lines = [
        f"YOUR ASSIGNMENT (storyboard shot {int(clip_index or 1)} — film ONLY this "
        f"{clamp_clip_duration(duration_sec)}-second window):",
        f"- Action: {str(action or '(this storyboard row only)').strip()[:400]}",
    ]
    if str(camera or "").strip():
        lines.append(f"- Camera: {str(camera).strip()[:160]}")
    if str(speech_line or "").strip():
        lines.append(f"- Speech this shot: {str(speech_line).strip()[:200]}")
    names = [str(x).strip() for x in (on_screen or []) if str(x).strip()]
    if names:
        lines.append(f"- On-screen: {', '.join(names[:8])}")
    lines.append(
        "Wan MOTION = this window only. Keep clothing / position / language / occupancy "
        "locks from the brief, storyboard, and PRODUCTION LOCK BIBLE. "
        "Do not paste later shots or the full remaining plot into call_video_model."
    )
    return "\n".join(lines)


def storyboard_fallback_beat(sb_text: str, clip_index: int = 1) -> str:
    """One short line when the storyboard table did not parse — never the whole board."""
    lines = []
    for raw in str(sb_text or "").splitlines():
        line = raw.strip().strip("|").strip()
        if not line or line.startswith("#") or set(line) <= set("-|: "):
            continue
        if re.match(r"(?i)^(shot|timeline|camera|storyboard|分镜)", line):
            continue
        lines.append(line)
    if not lines:
        return ""
    idx = max(1, int(clip_index or 1)) - 1
    pick = lines[idx] if idx < len(lines) else lines[0]
    return pick[:200]


def _beat_list_from_prompt(prompt: str, n: int) -> list[str]:
    n = max(1, int(n))
    beats: list[str] = []
    try:
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _is_non_story_beat,
            _split_prompt_beats,
        )

        beats = [b for b in _split_prompt_beats(prompt) if b and not _is_non_story_beat(b)]
    except Exception:  # noqa: BLE001
        beats = []
    if not beats:
        parts = re.split(r"(?<=[.!?。！？])\s+", str(prompt or "").strip())
        beats = [p.strip() for p in parts if len(p.strip()) > 12]
    beats = [b for b in beats if b and not _duration_only_line(b)]
    if not beats:
        beats = [first_sentence(prompt, limit=200) or "this storyboard window only"]
    if len(beats) >= n:
        return [b[:400] for b in beats[:n]]
    # Fewer story beats than clips: repeat beat identity but later windows say "continue".
    out: list[str] = []
    for i in range(n):
        src = beats[min(i, len(beats) - 1)]
        if i < len(beats):
            out.append(src[:400])
        else:
            out.append(
                f"Continue ONLY this already-started action (do not restart from the beginning; "
                f"do not play later plot): {first_sentence(src, limit=180)}"
            )
    return out


def apply_shot_scope(
    analysis: dict[str, Any] | None,
    user_prompt: str,
) -> dict[str, Any]:
    """Keep each shot's own window; collapse only verbatim full-prompt restatements."""
    out = dict(analysis or {})
    prompt = str(user_prompt or out.get("user_prompt") or "").strip()
    shots = [dict(s) for s in (out.get("clips") or []) if isinstance(s, dict)]
    coverage = user_asked_coverage(prompt)
    beats = _beat_list_from_prompt(prompt, max(1, len(shots)))
    for i, shot in enumerate(shots):
        action = str(shot.get("action") or shot.get("character_action") or "").strip()
        # Keep full storyboard detail (blocking/speech/wardrobe). Only collapse a
        # verbatim paste of the entire user prompt into this one row.
        if action and looks_like_full_story_restatement(action, prompt) and i > 0:
            action = first_sentence(action, limit=400)
        if not action:
            action = beats[i] if i < len(beats) else window_beat(prompt, i + 1, len(shots))
        if action:
            shot["action"] = action[:800]
            shot["character_action"] = action[:800]
        if not coverage:
            rel = str(shot.get("shot_relation") or "").strip().lower()
            if rel == "angle_variant":
                shot["shot_relation"] = "continuation" if i else "hard_cut"
            clear_unrequested_angle_view(shot, coverage=False)

    for i, shot in enumerate(shots, start=1):
        shot["clip_index"] = i
    out["clips"] = shots
    if shots:
        out["target_clip_count"] = len(shots)
    return out
