# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Named video-generation director styles for DesignSwarm.

These are film-level motion/story recipes injected into Brief / Storyboard /
Clip prompts — distinct from ``style_lock`` (visual medium: photoreal vs toon).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

VIDEO_STYLE_FINAL_FRAME_REVERSE = "final_frame_reverse"
VIDEO_STYLE_DEFAULT_CINEMATIC = "default_cinematic"

VIDEO_STYLES: dict[str, dict[str, Any]] = {
    VIDEO_STYLE_FINAL_FRAME_REVERSE: {
        "id": VIDEO_STYLE_FINAL_FRAME_REVERSE,
        "label": "终帧倒推 · 定格前最后几秒",
        "label_en": "Final-frame reverse (last seconds before the still)",
        "duration_hint_sec": "10-15",
        # The style is multi-beat by definition (rule: never a pure single take).
        "min_shots": 4,
        "summary": (
            "Treat the user reference / classic still as the LAST 1s endpoint. "
            "Reverse-engineer the action that forms that composition; "
            "pseudo-oner push + key close-ups; motif-driven transitions; "
            "speed early, settle late, freeze on the reference framing."
        ),
        # Keywords that activate this style from the user prompt.
        "cues": (
            "终帧",
            "定格",
            "定格图",
            "名画",
            "世界名画",
            "名作",
            "原画",
            "原作",
            "画作",
            "油画",
            "魔改",
            "致敬",
            "经典构图",
            "最后一秒",
            "最后1秒",
            "最后几秒",
            "倒推",
            "封神定格",
            "伪一镜",
            "final frame",
            "freeze frame",
            "freeze-frame",
            "last second",
            "last 1s",
            "classic painting",
            "famous painting",
            "oil painting",
            "masterpiece",
            "homage to",
            "endpoint still",
            "still as endpoint",
            "before the still",
            "resolve into the still",
        ),
    },
    VIDEO_STYLE_DEFAULT_CINEMATIC: {
        "id": VIDEO_STYLE_DEFAULT_CINEMATIC,
        "label": "默认电影短片",
        "label_en": "Default cinematic short",
        "duration_hint_sec": "flexible",
        "min_shots": 0,
        "summary": (
            "Standard storyboarded cinematic short: beat-driven shots, "
            "continuity locks, R2V shots from on-screen solos + scene specs."
        ),
        "cues": (),
    },
}

# Canonical beat roles for final_frame_reverse (director rewrites the wording).
FINAL_FRAME_REVERSE_BEATS: tuple[tuple[str, str, str], ...] = (
    (
        "空间推进",
        "wide / fast push-in",
        "伪一镜到底：从环境切入并快速推进，建立空间、气势与速度；尚未形成参考图构图。",
    ),
    (
        "关键特写",
        "close-up / eye-level",
        "关键特写打点：神态、关键动作或关键物件的细节；由视觉母题（布料/烟尘/光斑/倒影）带入。",
    ),
    (
        "动作归位",
        "medium / decelerating",
        "动作链收束：人物、道具、披风、光线开始向最终构图归位，速度明显减慢。",
    ),
    (
        "终帧定格",
        "match reference framing",
        "最后 1 秒：完全对齐参考图构图（姿态、取景、光线），稳定定格封神。",
    ),
)


def list_video_styles() -> list[dict[str, Any]]:
    """Public catalog for UI / metadata (id + labels + summary)."""
    out: list[dict[str, Any]] = []
    for style in VIDEO_STYLES.values():
        out.append(
            {
                "id": style["id"],
                "label": style["label"],
                "label_en": style["label_en"],
                "duration_hint_sec": style.get("duration_hint_sec"),
                "summary": style["summary"],
            }
        )
    return out


def detect_video_style(
    prompt: str,
    *,
    has_reference_images: bool = False,
    forced: str | None = None,
) -> str:
    """Pick a director style. Explicit force wins; else cue match; else default."""
    forced_id = str(forced or "").strip().lower()
    if forced_id in VIDEO_STYLES:
        return forced_id
    blob = (prompt or "").lower()
    # Prefer the reverse style when classic-still language is present.
    for cue in VIDEO_STYLES[VIDEO_STYLE_FINAL_FRAME_REVERSE]["cues"]:
        if cue.lower() in blob:
            return VIDEO_STYLE_FINAL_FRAME_REVERSE
    # Uploaded still + short-form language often means "animate into this pose".
    if has_reference_images and any(
        k in blob
        for k in (
            "10秒",
            "15秒",
            "10-15",
            "10–15",
            "short video",
            "短视频",
            "动起来",
            "animate",
            "bring to life",
            "活过来",
        )
    ):
        return VIDEO_STYLE_FINAL_FRAME_REVERSE
    return VIDEO_STYLE_DEFAULT_CINEMATIC


def resolve_video_style(graph: dict[str, Any] | None = None, prompt: str = "") -> str:
    """Resolve style from graph metadata overrides, then prompt cues."""
    meta = (graph or {}).get("metadata") if isinstance(graph, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    forced = (
        meta.get("video_style")
        or meta.get("director_style")
        or meta.get("director_method")
    )
    if not forced and isinstance(meta.get("video_style_lock"), dict):
        forced = (meta.get("video_style_lock") or {}).get("id")
    has_refs = False
    try:
        from jiuwenswarm.server.runtime.designer.user_references import (
            user_reference_image_paths,
        )

        has_refs = bool(user_reference_image_paths(graph or {}))
    except Exception:  # noqa: BLE001
        refs = meta.get("user_references") if isinstance(meta.get("user_references"), list) else []
        has_refs = any(
            isinstance(r, dict) and str(r.get("kind") or "").lower() in {"image", "img", "still"}
            for r in refs
        )
    text = prompt or str((graph or {}).get("description") or meta.get("user_prompt") or "")
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        skips_final_frame_reverse,
    )

    if not forced and skips_final_frame_reverse(graph):
        return VIDEO_STYLE_DEFAULT_CINEMATIC
    return detect_video_style(text, has_reference_images=has_refs, forced=str(forced or "") or None)


def video_style_record(style_id: str) -> dict[str, Any]:
    return dict(VIDEO_STYLES.get(style_id) or VIDEO_STYLES[VIDEO_STYLE_DEFAULT_CINEMATIC])


def video_style_min_shots(style_id: str) -> int:
    """Minimum beats the style needs to exist at all (0 = no opinion)."""
    try:
        return int(video_style_record(style_id).get("min_shots") or 0)
    except (TypeError, ValueError):
        return 0


def enforce_style_shot_floor(
    analysis: dict[str, Any], prompt: str = "", *, style_id: str = ""
) -> dict[str, Any]:
    """Grow a below-floor shot list to the style's canonical beats.

    ``final_frame_reverse`` forbids a pure single take, so a 1-shot analysis
    contradicts the selected style. Explicit N in the prompt still wins.
    """
    data = dict(analysis or {})
    text = prompt or str(data.get("user_prompt") or "")
    sid = style_id or detect_video_style(text)
    floor = video_style_min_shots(sid)
    if floor < 2:
        return data
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        if int(_explicit_shot_count_from_prompt(text) or 0) >= 1:
            return data
    except Exception:  # noqa: BLE001
        pass
    shots = [dict(s) for s in (data.get("shots") or []) if isinstance(s, dict)]
    if len(shots) >= floor:
        return data
    base = shots[0] if shots else {}
    cast = list(base.get("character_ids") or base.get("on_screen") or [])
    setting = str(base.get("setting_id") or "set_1")
    keep = str(base.get("action") or text)[:400]
    grown: list[dict[str, Any]] = []
    for i, (title, camera, intent) in enumerate(FINAL_FRAME_REVERSE_BEATS[:floor], start=1):
        existing = shots[i - 1] if i <= len(shots) else {}
        shot = dict(existing)
        shot["shot_index"] = i
        shot.setdefault("title", title)
        shot["camera"] = str(existing.get("camera") or camera)[:120]
        shot["action"] = str(existing.get("action") or f"{intent} 画面内容：{keep}")[:800]
        shot["keyframe_prompt"] = str(
            existing.get("keyframe_prompt") or shot["action"]
        )[:1200]
        shot.setdefault("character_ids", list(cast))
        shot.setdefault("on_screen", list(cast))
        shot.setdefault("setting_id", setting)
        shot["style_beat_role"] = title
        grown.append(shot)
    data["shots"] = grown
    data["target_shot_count"] = len(grown)
    data["style_shot_floor"] = floor
    return data


def final_frame_reverse_playbook() -> str:
    """Director playbook injected into Director / Storyboard / Clip when active."""
    return """
## Video style: final_frame_reverse（终帧倒推）
User reference / classic still = LAST ~1s ENDPOINT, not the opening frame.
Reverse-engineer the final few seconds BEFORE that composition locks.

Formula:
  endpoint still → action chain that forms it → pseudo-oner spatial push →
  key close-ups (face / hands / prop) → motif-driven transitions →
  last second resolves into the reference framing and freezes.

Rules:
1. Do NOT orbit a finished still as a 3D model turntable.
2. Not pure one-take: long push builds space/energy; cut to close-ups for emotion/action/prop.
3. Every cut needs a visual carrier (smoke, rain, cloth, feather, light shaft, muzzle flash,
   wipe-by body, rail line, reflection, fog) — never an unmotivated hard cut list.
4. Rhythm: early speed/impact/travel → mid close-up hits → late decelerate/settle → final 1s freeze.
5. Last keyframe + last clip beat MUST match the reference composition (pose, framing, light).
6. Prompt structure for storyboard/clips: overall intent → action chain → timed beats →
   per-beat (frame content, camera move, cast action, transition motive, emotion) →
   negative prompts → one execution principle.

Negatives: static showcase orbit, vague "cinematic" with no action, all-slow drift,
unmotivated cuts, ending that misses the reference still.
""".strip()


def video_style_clause(style_id: str, *, for_clip: bool = False) -> str:
    """Short lock line stamped onto media prompts."""
    if style_id != VIDEO_STYLE_FINAL_FRAME_REVERSE:
        return ""
    if for_clip:
        return (
            "VIDEO STYLE LOCK (final_frame_reverse): This shot is part of a 10–15s arc "
            "that ENDS on the user reference / classic still. Animate toward that final "
            "composition — do not orbit a finished pose. Prefer decisive motion early; "
            "if this is a late shot, decelerate and settle. Use motif-motivated continuity "
            "from the prior beat (cloth/smoke/light/reflection wipe). "
            "No turntable showcase. No subtitles."
        )
    return (
        "VIDEO STYLE LOCK (final_frame_reverse): Reference still = final 1s endpoint. "
        "Author beats that reverse-form that framing; pseudo-oner + key close-ups; "
        "motif transitions; speed→settle→freeze on reference composition."
    )


def video_style_skill_excerpt(style_id: str) -> str:
    """Longer excerpt for node skill_excerpt / director metadata."""
    if style_id == VIDEO_STYLE_FINAL_FRAME_REVERSE:
        return final_frame_reverse_playbook()
    return ""


def stamp_video_style_on_graph(graph: dict[str, Any], prompt: str | None = None) -> str:
    """Write ``metadata.video_style`` + ``video_style_lock``; return resolved id."""
    text = prompt or str(graph.get("description") or "")
    style_id = resolve_video_style(graph, text)
    record = video_style_record(style_id)
    meta = dict(graph.get("metadata") or {})
    meta["video_style"] = style_id
    meta["video_style_lock"] = {
        "id": style_id,
        "label": record.get("label"),
        "label_en": record.get("label_en"),
        "summary": record.get("summary"),
        "duration_hint_sec": record.get("duration_hint_sec"),
    }
    graph["metadata"] = meta
    return style_id
