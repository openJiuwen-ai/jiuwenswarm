# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Wan-facing prompt hygiene and regenerate packets.

Continuity stays in the clip as who is present, where they are, and what this
window does. Lines that tell Wan an action is "already done" or quote a prior
line are removed before the video call. Regenerate stores the prompt, reference
images, and upstream outputs so a later rerun can reuse them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_NEGATIVE_LINE = re.compile(
    r"(?i)("
    r"already[_\s-]?done|"
    r"already finished|"
    r"already delivered|"
    r"already staged|"
    r"already spoken|"
    r"do not restage|"
    r"do not repeat|"
    r"do not redo|"
    r"do not restate|"
    r"do not reseat|"
    r"do not film|"
    r"do not copy|"
    r"do not draw|"
    r"do not attach the peopled|"
    r"prior speech|"
    r"previous clip had|"
    r"previous wan|"
    r"forbidden speech|"
    r"please do not repeat"
    r")"
)

_BLOCK_START = re.compile(
    r"(?i)^(already[_\s-]?done|prior speech|previous clip had|previous wan|"
    r"character consistency|same-scene consistency gate)\b"
)


def scrub_negative_wan_prompt(text: str) -> str:
    """Drop already-done / do-not-repeat / quoted prior-speech lines from a Wan prompt."""
    kept: list[str] = []
    skip_block = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if _BLOCK_START.search(stripped):
            skip_block = True
            continue
        if skip_block:
            if not stripped:
                skip_block = False
                continue
            if stripped.startswith("-") or stripped.startswith("•"):
                continue
            skip_block = False
        if stripped and _NEGATIVE_LINE.search(stripped):
            sentences = re.split(r"(?<=[.!?])\s+", stripped)
            cleaned = [s for s in sentences if s and not _NEGATIVE_LINE.search(s)]
            if not cleaned:
                continue
            indent = line[: len(line) - len(line.lstrip())]
            kept.append(indent + " ".join(cleaned))
            continue
        kept.append(line)
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def positive_continuity_clause(
    cfg: dict[str, Any] | None,
    *,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Who is in frame, where they sit, and what this window does. No prior quotes."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
            character_name_map,
            on_screen_ids,
        )
    except Exception:  # noqa: BLE001
        character_name_map = lambda _chars: {}  # type: ignore[assignment]
        on_screen_ids = lambda _cfg: []  # type: ignore[assignment]

    names = character_name_map(characters)
    on = on_screen_ids(cfg)
    seats = cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else {}
    holds: list[str] = []
    for cid in on:
        anchor = seats.get(cid) if isinstance(seats.get(cid), dict) else {}
        if not anchor:
            continue
        who = names.get(cid, cid)
        bits = [who, "stays"]
        if anchor.get("zone"):
            bits.append(str(anchor["zone"]))
        if anchor.get("landmark"):
            bits.append(f"near {anchor['landmark']}")
        if anchor.get("screen"):
            bits.append(str(anchor["screen"]))
        if anchor.get("pose"):
            bits.append(str(anchor["pose"]))
        holds.append(" ".join(bits))
    who_now = ", ".join(names.get(c, c) for c in on[:8]) if on else ""
    action = str(cfg.get("shot_action") or cfg.get("character_action") or "").strip()
    camera = str(cfg.get("camera") or "").strip()
    speech = str(cfg.get("speech_line") or "").strip()
    lines = ["CONTINUITY STATE (this clip continues the same film):"]
    if who_now:
        lines.append(f"People in frame now: {who_now}.")
    if holds:
        lines.append("SEAT HOLDS: " + "; ".join(holds[:8]) + ".")
    if action:
        lines.append(f"This window: {action[:400]}.")
    if camera:
        lines.append(f"Camera this window: {camera[:120]}.")
    if speech:
        lines.append(f"Spoken words in this window: {speech[:200]}.")
    else:
        lines.append("This window has no new spoken line.")
    lines.append(
        "Same faces and wardrobe as the character sheets. "
        "Same room as the scene specs. Match the film STYLE LOCK."
    )
    return "\n".join(lines)


def _uri_to_path(uri: str) -> str:
    raw = str(uri or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("file:"):
        parsed = urlparse(raw)
        path = unquote(parsed.path or "")
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path
    return raw


def capture_regenerate_packet(
    node: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    run_states: dict[str, Any] | None = None,
    *,
    prompt: str = "",
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Snapshot the prompt, reference images, and upstream outputs for regenerate."""
    node = node if isinstance(node, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    gen = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    stored_prompt = str(
        prompt
        or cfg.get("last_wan_prompt")
        or cfg.get("last_approved_prompt")
        or gen.get("prompt")
        or cfg.get("prompt")
        or ""
    ).strip()
    nodes_by_id = {
        str(n.get("id") or ""): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and str(n.get("id") or "")
    }
    upstream_ids: list[str] = []
    for key in ("inputs", "character_node_ids"):
        upstream_ids.extend(str(x) for x in (cfg.get(key) or []) if str(x).strip())
    for key in ("scene_node_id", "continuity_clip_node_id", "previous_clip_node_id"):
        val = str(cfg.get(key) or "").strip()
        if val:
            upstream_ids.append(val)
    seen: set[str] = set()
    ordered: list[str] = []
    for nid in upstream_ids:
        if nid in seen:
            continue
        seen.add(nid)
        ordered.append(nid)
    states = run_states if isinstance(run_states, dict) else {}
    upstream: list[dict[str, Any]] = []
    image_paths: list[str] = []
    for nid in ordered:
        state = states.get(nid) if isinstance(states.get(nid), dict) else {}
        ref = state.get("output_ref") if isinstance(state.get("output_ref"), dict) else {}
        other = nodes_by_id.get(nid) or {}
        other_cfg = other.get("config") if isinstance(other.get("config"), dict) else {}
        other_gen = other_cfg.get("generate") if isinstance(other_cfg.get("generate"), dict) else {}
        other_prompt = str(
            other_cfg.get("last_wan_prompt")
            or other_cfg.get("last_approved_prompt")
            or other_gen.get("prompt")
            or other_cfg.get("prompt")
            or ""
        ).strip()
        uri = str(ref.get("uri") or "")
        path = _uri_to_path(uri)
        item = {
            "node_id": nid,
            "status": state.get("status"),
            "output_ref": ref,
            "prompt": other_prompt[:4000],
            "style_lock": other_cfg.get("style_lock") if isinstance(other_cfg.get("style_lock"), dict) else None,
            "path": path,
        }
        upstream.append(item)
        mime = str(ref.get("mime_type") or "").lower()
        if path and (mime.startswith("image/") or Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}):
            image_paths.append(path)
    for extra in reference_images or []:
        text = str(extra or "").strip()
        if text and text not in image_paths:
            image_paths.append(text)
    return {
        "node_id": str(node.get("id") or ""),
        "prompt": stored_prompt[:4000],
        "shot_action": str(cfg.get("shot_action") or "")[:500],
        "speech_line": str(cfg.get("speech_line") or "")[:280],
        "camera": str(cfg.get("camera") or "")[:160],
        "style_lock": cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else None,
        "costume_lock": str(cfg.get("costume_lock") or "")[:800],
        "character_node_ids": [str(x) for x in (cfg.get("character_node_ids") or []) if str(x)],
        "scene_node_id": str(cfg.get("scene_node_id") or ""),
        "on_screen": [str(x) for x in (cfg.get("on_screen") or []) if str(x)],
        "seat_anchors": cfg.get("seat_anchors") if isinstance(cfg.get("seat_anchors"), dict) else None,
        "previous_clip_action": str(cfg.get("previous_clip_action") or "")[:300],
        "previous_clip_wan_prompt": str(cfg.get("previous_clip_wan_prompt") or "")[:2500],
        "reference_images": image_paths[:8],
        "upstream": upstream,
    }


def remember_generation(
    node: dict[str, Any] | None,
    graph: dict[str, Any] | None,
    run: dict[str, Any] | None,
    *,
    prompt: str,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Write regenerate_packet onto the node config after a successful media call."""
    node = node if isinstance(node, dict) else {}
    cfg = dict(node.get("config") or {}) if isinstance(node.get("config"), dict) else {}
    states = {}
    if isinstance(run, dict):
        states = run.get("node_states") if isinstance(run.get("node_states"), dict) else {}
    packet = capture_regenerate_packet(
        {**node, "config": cfg},
        graph,
        states,
        prompt=prompt,
        reference_images=reference_images,
    )
    cfg["regenerate_packet"] = packet
    cfg["last_wan_prompt"] = str(prompt or "")[:4000]
    cfg["last_approved_prompt"] = str(prompt or "")[:4000]
    node["config"] = cfg
    return packet


def apply_regenerate_packet(
    cfg: dict[str, Any],
    graph: dict[str, Any] | None,
    run: dict[str, Any] | None,
    *,
    prompt: str,
    reference_paths: list[str],
) -> tuple[str, list[str]]:
    """On regenerate, restore the stored prompt and any reference images still on disk."""
    graph = graph if isinstance(graph, dict) else {}
    run = run if isinstance(run, dict) else {}
    use_prior = bool(
        (graph.get("metadata") or {}).get("use_prior_feedback")
        or (run.get("metadata") or {}).get("use_prior_feedback")
    )
    packet = cfg.get("regenerate_packet") if isinstance(cfg.get("regenerate_packet"), dict) else {}
    if not use_prior or not packet:
        return prompt, list(reference_paths)
    for key in (
        "shot_action",
        "speech_line",
        "camera",
        "style_lock",
        "costume_lock",
        "character_node_ids",
        "scene_node_id",
        "on_screen",
        "seat_anchors",
        "previous_clip_action",
        "previous_clip_wan_prompt",
    ):
        if not cfg.get(key) and packet.get(key):
            cfg[key] = packet[key]
    paths = list(reference_paths)
    seen = {str(Path(p).resolve()) for p in paths if str(p).strip()}
    for raw in packet.get("reference_images") or []:
        text = str(raw or "").strip()
        if not text or not Path(text).is_file():
            continue
        key = str(Path(text).resolve())
        if key in seen:
            continue
        seen.add(key)
        paths.append(text)
    stored = str(packet.get("prompt") or "").strip()
    if stored:
        prompt = stored
    return prompt, paths
