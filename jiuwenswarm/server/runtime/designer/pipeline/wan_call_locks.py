# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Preserve media prompt authority and bind video calls to actual attachments."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.server.runtime.designer.pipeline.reference_binding import (
    ReferenceSlot,
    append_reference_binding,
    strip_reference_binding,
)


def _style_from(cfg: dict[str, Any], graph: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    for src in (
        cfg.get("style_lock"),
        (graph.get("metadata") or {}).get("style_lock") if isinstance(graph.get("metadata"), dict) else None,
        analysis.get("style_lock"),
    ):
        if isinstance(src, dict) and src:
            return src
    return {}


def set_orientation_lock_clause(*, scene_specs: bool = False) -> str:
    """Keep first-frame room orientation stable (fixes spin/flip drift)."""
    subject = "Scene spec / Image 1" if scene_specs else "Image 1"
    return (
        f"SET/ORIENTATION LOCK: keep {subject}'s exact room geometry, wall/window sides, "
        "furniture layout, and camera roll/horizon — FORBIDDEN: spin or rotate the set, "
        "mirror/flip architecture left-right, tilt the world, or rebuild a different room. "
        "Camera may push-in/pan slightly only if it does not reorient the space."
    )


def apply_image_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> str:
    """Resolve image call text without hard-coded practice rewrites.

    User toolbar text (``user_edit_prompt`` / packet) wins; otherwise the
    LLM-authored ``prompt`` argument is sent as-is. Structured locks stay on
    cfg for the leaf agent — they are not appended or rewritten here.
    """
    del graph  # locks remain structured on cfg; not stamped into the API body
    cfg_map = cfg if isinstance(cfg, dict) else {}
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        resolve_user_origin_prompt,
    )

    user_text = resolve_user_origin_prompt(cfg_map, "")
    text = user_text or str(prompt or "").strip()
    if text:
        # Stamp last_* with what we send; keep durable user surfaces intact.
        _stamp_sent_prompt(
            cfg_map,
            text,
            user_origin=bool(user_text),
            user_edit=user_text,
        )
        cfg_map["director_image_prompt_notes"] = (
            ["image_user_authority"] if user_text else ["image_llm_authority"]
        )
        cfg_map["director_image_prompt_approved"] = True
    return text


def _stamp_sent_prompt(
    cfg: dict[str, Any],
    approved: str,
    *,
    user_origin: bool,
    user_edit: str = "",
) -> None:
    """Keep saved surfaces honest with the text actually sent to the video API.

    When origin is user, durable beat authority (``user_edit_prompt`` + packet)
    stays the original toolbar text, independent of appended dialogue and
    attachment bindings.
    """
    text = str(approved or "").strip()
    if not text:
        return
    edit = str(user_edit or cfg.get("user_edit_prompt") or "").strip()
    if user_origin and edit:
        cfg["user_edit_prompt"] = edit
        packet = (
            dict(cfg["regenerate_packet"])
            if isinstance(cfg.get("regenerate_packet"), dict)
            else {}
        )
        packet["prompt"] = edit
        cfg["regenerate_packet"] = packet
    cfg["prompt"] = text
    generate = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
    generate["prompt"] = text
    if user_origin:
        generate["prompt_origin"] = "user"
    cfg["generate"] = generate
    cfg["last_wan_prompt"] = text
    cfg["last_approved_prompt"] = text


def apply_wan_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
    clip_index: int = 0,
    slots: list[ReferenceSlot] | None = None,
) -> str:
    """Preserve authored video prose, explicit dialogue, and actual image bindings."""
    del clip_index
    from jiuwenswarm.server.runtime.designer.pipeline.video_call_contract import (
        prepare_video_prompt,
    )
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        active_video_family,
        resolve_user_origin_prompt,
    )

    cfg_map = cfg if isinstance(cfg, dict) else {}
    user_text = resolve_user_origin_prompt(cfg_map)
    candidate = user_text or prompt
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("Video prompt must be a non-empty string")
    # Remove only a previously stamped binding, then render the actual attach order.
    candidate = strip_reference_binding(candidate, cfg_map)
    prepared = prepare_video_prompt(candidate, cfg=cfg_map, graph=graph)
    if slots is not None:
        prepared = append_reference_binding(
            prepared,
            slots,
            cfg=cfg_map,
            family=active_video_family(),
        )
    _stamp_sent_prompt(
        cfg_map,
        prepared,
        user_origin=bool(user_text),
        user_edit=user_text,
    )
    return prepared
