# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clip node handler: generate one video per storyboard shot."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_VIDEO,
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_pipeline,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    node_output_image_paths,
    role_output_image_path,
    role_output_text,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    parse_storyboard_shots,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:
        logger.debug("imageio_ffmpeg unavailable for still→mp4", exc_info=True)
        return None



def parse_shot_duration_seconds(timeline: str, default: int = 5) -> int:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        duration_from_timeline,
    )

    return duration_from_timeline(timeline, default=default)


def collect_clip_scene_image(
    ctx: NodeExecutionContext | None,
    shot_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> Path | None:
    """Resolve the setting's scene-card image (empty environment plate for R2V)."""
    del shot_index  # scene specs are keyed by setting / scene_node_id, not shot index
    if ctx is None:
        return None
    cfg = (
        node.get("config")
        if isinstance(node, dict) and isinstance(node.get("config"), dict)
        else {}
    )
    scene_nid = str(
        cfg.get("scene_node_id")
        or (cfg.get("identity_refs") or {}).get("scene_node_id")
        or ""
    ).strip()
    if scene_nid:
        paths = node_output_image_paths(ctx, scene_nid)
        if paths:
            return paths[0]
    setting_id = str(cfg.get("setting_id") or "").strip()
    scenes = [
        n
        for n in (ctx.graph.get("nodes") or [])
        if node_pipeline(n) == NODE_ROLE_SCENE
    ]
    if setting_id:
        matched = next(
            (
                n
                for n in scenes
                if str((n.get("config") or {}).get("setting_id") or "").strip() == setting_id
            ),
            None,
        )
        if matched is not None:
            paths = node_output_image_paths(ctx, str(matched.get("id") or ""))
            if paths:
                return paths[0]
    if scenes:
        paths = node_output_image_paths(ctx, str(scenes[0].get("id") or ""))
        if paths:
            return paths[0]
    return None


def edge_image_flow(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode | None,
) -> list[tuple[str, str, Path]] | None:
    """Image outputs that arrive on this node's incoming data edges.

    Returns None when the node has no incoming data edges, so legacy clips that
    only declare cast and scene in config keep that recipe. Otherwise every
    connected image output is an input, in edge order. Role is only a label for
    later ordering; it does not decide whether the value flows.
    """
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None)
    if outputs is None:
        return None
    return [
        (item.role, item.label, item.path)
        for item in outputs
        if item.kind == "image" and item.path is not None
    ]


def edge_text_inputs(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode | None,
) -> list[tuple[str, str]]:
    """Text bodies that arrive on incoming data edges, in edge order."""
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None) or []
    texts: list[tuple[str, str]] = []
    for item in outputs:
        if item.kind != "text":
            continue
        body = item.text.strip()
        if body:
            texts.append((item.label, body[:4000]))
    return texts


def edge_video_inputs(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode | None,
) -> list[tuple[str, Path]]:
    """Video files that arrive on incoming data edges, in edge order."""
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None) or []
    return [
        (item.label, item.path)
        for item in outputs
        if item.kind == "video" and item.path is not None
    ]


def first_connected_video_file(videos: list[tuple[str, Path]]) -> str | None:
    """Path of the first video that arrived on a data edge.

    The video call accepts a single reference file. Later connected videos stay
    in the prompt text instead of being attached as extra files.
    """
    if not videos:
        return None
    _label, path = videos[0]
    return str(path)


def connected_payload_clause(
    texts: list[tuple[str, str]],
    videos: list[tuple[str, Path]],
) -> str:
    """Name connected text and video inputs so the prompt sees them."""
    parts: list[str] = []
    if texts:
        blocks = [f"{label}:\n{body}" for label, body in texts]
        parts.append("Connected text inputs:\n" + "\n\n".join(blocks))
    if videos:
        named = "; ".join(f"{label} ({path.name})" for label, path in videos)
        parts.append(
            "Connected video inputs, in edge order: "
            f"{named}. Use them as motion references for this shot."
        )
    return "\n\n".join(parts)


def _flow_role_bucket(role: str) -> str:
    if role in {NODE_ROLE_CHARACTER_DESIGN, "character"}:
        return "character"
    if role == NODE_ROLE_SCENE:
        return "scene"
    return "other"


def ordered_flow_paths(
    flow: list[tuple[str, str, Path]],
    attached: list[Path],
) -> list[Path]:
    """Wan order over values that already flowed in: cast, local uploads, other edges, scene last."""
    characters: list[Path] = []
    others: list[Path] = []
    scenes: list[Path] = []
    for role, _label, path in flow:
        bucket = _flow_role_bucket(role)
        if bucket == "character":
            characters.append(path)
        elif bucket == "scene":
            scenes.append(path)
        else:
            others.append(path)
    return [*characters, *attached, *others, *scenes]


def connected_clip_extra_images(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode | None,
) -> list[tuple[str, Path]]:
    """Non-cast, non-scene images that arrived on incoming edges."""
    flow = edge_image_flow(ctx, node)
    if not flow:
        return []
    return [
        (label, path)
        for role, label, path in flow
        if _flow_role_bucket(role) == "other"
    ]


def attach_order_clause(
    paths: list[Path],
    flow: list[tuple[str, str, Path]] | None,
) -> str:
    """Name each attached file from the edge that delivered it."""
    if not paths or not flow:
        return ""
    labels: dict[str, str] = {}
    for _role, label, path in flow:
        labels.setdefault(str(path.resolve()), label)
    bits = []
    for index, path in enumerate(paths, start=1):
        label = labels.get(str(path.resolve()), path.name)
        bits.append(f"Image {index} = {label} ({path.name})")
    return (
        "Connected inputs follow the canvas edges, in this attach order: "
        + "; ".join(bits)
        + ". These Image numbers are the inputs. Use them instead of any earlier Image numbers."
    )


def collect_clip_reference_images(
    ctx: NodeExecutionContext | None,
    shot_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> list[Path]:
    """Wan reference_images: on-screen solos, then user stills, then the scene card.

    Design clips always attach these as R2V refs — never as a single first-frame
    still. Order matches Wan R2V labeling: solos are character1…, uploads and
    user-wired image nodes follow, and the scene card stays last as the
    environment.
    """
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        node_ids_output_image_paths,
        role_output_image_paths,
        uploaded_material_image_paths,
    )

    attached = uploaded_material_image_paths(node if isinstance(node, dict) else None)

    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        paths.append(resolved)

    if ctx is None:
        return paths

    cfg = (
        node.get("config")
        if isinstance(node, dict) and isinstance(node.get("config"), dict)
        else {}
    )
    if node is None and ctx is not None:
        node = next(
            (
                n
                for n in (ctx.graph.get("nodes") or [])
                if isinstance(n, dict) and str(n.get("id") or "") == str(getattr(ctx, "node_id", "") or "")
            ),
            None,
        )
        cfg = (
            node.get("config")
            if isinstance(node, dict) and isinstance(node.get("config"), dict)
            else cfg
        )

    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        cap_r2v_reference_paths,
        offscreen_ids,
        on_screen_ids,
    )

    on_screen = on_screen_ids(cfg)
    offscreen = set(offscreen_ids(cfg))
    preferred = [str(x) for x in (cfg.get("character_node_ids") or []) if str(x).strip()]
    if not preferred:
        irefs = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        preferred = [str(x) for x in (irefs.get("character_node_ids") or []) if str(x).strip()]

    solo_by_cid: dict[str, str] = {}
    graph = ctx.graph if isinstance(ctx.graph, dict) else {}
    for other in graph.get("nodes") or []:
        if not isinstance(other, dict):
            continue
        if node_pipeline(other) != NODE_ROLE_CHARACTER_DESIGN:
            continue
        oc = other.get("config") if isinstance(other.get("config"), dict) else {}
        if oc.get("combined_cast"):
            continue
        cids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
        if len(cids) == 1:
            solo_by_cid[cids[0]] = str(other.get("id") or "")

    solo_nids: list[str] = []
    if on_screen:
        solo_nids = [solo_by_cid[c] for c in on_screen if c in solo_by_cid and c not in offscreen]
    if not solo_nids and preferred:
        # Drop preferred nodes whose cid is off-screen.
        cid_by_nid = {nid: cid for cid, nid in solo_by_cid.items()}
        solo_nids = [
            nid
            for nid in preferred
            if cid_by_nid.get(nid) not in offscreen
        ]
    flow = edge_image_flow(ctx, node if isinstance(node, dict) else None)
    if flow is not None:
        for path in ordered_flow_paths(flow, attached):
            add(path)
        return cap_r2v_reference_paths(paths)

    if solo_nids:
        for path in node_ids_output_image_paths(ctx, solo_nids):
            add(path)
    elif not on_screen:
        for path in role_output_image_paths(ctx, NODE_ROLE_CHARACTER_DESIGN)[:4]:
            add(path)

    for path in attached:
        add(path)

    scene = collect_clip_scene_image(ctx, shot_index, node=node)
    # Scene specs is the last environment reference for every clip.
    add(scene)
    return cap_r2v_reference_paths(paths)


def _storyboard_narrative_action(shot: dict[str, Any] | None) -> str:
    """Prefer Comment (keyframe/clip description), then Character action."""
    if not isinstance(shot, dict):
        return ""
    return str(shot.get("comment") or shot.get("character_action") or "").strip()


def _extract_action_from_generate_prompt(text: str) -> str:
    """Pull a clean Action: … beat from a lock-stuffed generate.prompt when present."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(
        r"(?i)(?:^|\n)\s*(?:Primary action for shot\s+\d+\s*:|Action)\s*:\s*(.+?)(?:\n|$)",
        raw,
    )
    if match:
        return match.group(1).strip()[:500]
    # Fallback: first non-lock sentence if prompt is short and narrative-only.
    if not _looks_like_contaminated_prompt(raw) and len(raw) <= 500:
        return raw[:500]
    return ""


def _shot_for_node(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None,
) -> tuple[int, StoryboardShot | None]:
    index = node_shot_index(node)
    text = role_output_text(ctx, NODE_ROLE_STORYBOARD) if ctx is not None else ""
    shots = parse_storyboard_shots(text)
    if shots and 1 <= index <= len(shots):
        shot = dict(shots[index - 1])
        # Keep live storyboard comment/character_action intact.
        # Do NOT overwrite comment with generate.prompt (often lock-stuffed / stale).
        sb_action = _storyboard_narrative_action(shot)
        if not str(shot.get("character_action") or "").strip() and sb_action:
            shot["character_action"] = sb_action
        if not str(shot.get("comment") or "").strip() and sb_action:
            shot["comment"] = sb_action
        return index, shot
    return index, None


def _looks_like_contaminated_prompt(text: str) -> bool:
    """True only for pasted handoff/assignment dumps — not normal SCENE SPECS / staging."""
    raw = (text or "").upper()
    needles = (
        "PRIOR KEYFRAME PROMPT",
        "PREVIOUS KEYFRAME HAD",
        "PRIOR SHOT CONSISTENCY",
        "PREVIOUS CLIP HAD",
        "YOUR ASSIGNMENT",
        "MASTER SCENE PROMPT",
        "CHARACTER CONSISTENCY (MANAGER)",
        "PRIOR CLIP WAN",
        "PREVIOUS WAN PROMPT",
    )
    return any(n in raw for n in needles)


def _format_shot_block(shot: StoryboardShot, shot_index: int) -> str:
    lines = [
        f"Shot {shot_index} ONLY (do not film other shots)",
        f"- Timeline: {shot.get('timeline') or ''}",
        f"- Camera: {shot.get('camera') or ''}",
        f"- Camera move: {shot.get('move') or ''}",
        f"- Character action: {shot.get('character_action') or ''}",
        f"- Scene change: {shot.get('scene_change') or ''}",
    ]
    comment = str(shot.get("comment") or "").strip()
    if comment:
        lines.append(f"- Shot description: {comment}")
    return "\n".join(lines)


def _clip_prompt_lead(
    shot_index: int,
    duration: int,
    *,
    has_character: bool,
    has_scene: bool,
    focus_names: str = "",
    continuity: bool = False,
    first_of_setting: bool = False,
) -> str:
    attached: list[str] = []
    if has_character:
        attached.append("on-screen character solo sheets as character1, character2, …")
    if has_scene:
        attached.append("scene specs last, as the room")
    extras = (
        " Explicit visual inputs are attached, in order: " + ", ".join(attached) + "."
        if attached
        else ""
    )
    focus = f" Feature only: {focus_names}." if focus_names else ""
    continue_bit = (
        "CONTINUATION: same room, same faces, same wardrobe. This window's action continues the story. "
        if continuity or not first_of_setting
        else "Opening of this setting: place the on-screen people into the empty room. "
    )
    return (
        f"Create shot {shot_index} as a {duration}-second video that plays THIS "
        "storyboard shot only. "
        f"{extras}{focus} "
        f"{continue_bit}"
        "character1/character2 are the solo sheets (face and wardrobe). "
        "The last image is the scene specs. "
        "Match the film STYLE LOCK. One instance per person. "
        "No subtitles, no cutaways.\n\n"
    )


def build_clip_prompt(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
) -> str:
    """Shot-specific clip prompt. Prefer shot row over full Brief to avoid identical clips."""
    shot_index, shot = _shot_for_node(graph, node, ctx)
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    cfg = dict(cfg)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
            ensure_prior_clip_story_on_cfg,
        )

        cfg = ensure_prior_clip_story_on_cfg(
            cfg, graph if isinstance(graph, dict) else {}
        )
        if isinstance(node, dict):
            node["config"] = cfg
    except Exception:  # noqa: BLE001
        pass
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
            scrub_restated_speech,
        )

        cfg = scrub_restated_speech(cfg)
        if isinstance(node, dict):
            node["config"] = cfg
    except Exception:  # noqa: BLE001
        pass
    duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
    focus_names = ""
    cfg_names = [str(x).strip() for x in (cfg.get("cast_names") or []) if str(x).strip()]
    if cfg_names:
        focus_names = ", ".join(cfg_names)
    first_of_setting = bool(cfg.get("first_of_setting"))
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_reference_binding import (
            clip_is_first_of_setting,
        )

        first_of_setting = clip_is_first_of_setting(cfg, graph if isinstance(graph, dict) else {})
    except Exception:  # noqa: BLE001
        pass
    if ctx is not None:
        preferred = [str(x) for x in (cfg.get("character_node_ids") or []) if str(x).strip()]
        has_character = bool(preferred) or role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None
        has_scene = (
            bool(str(cfg.get("scene_node_id") or "").strip())
            or role_output_image_path(ctx, NODE_ROLE_SCENE) is not None
        )
    else:
        has_character = False
        has_scene = bool(str(cfg.get("scene_node_id") or "").strip())
    continuity = bool(
        str(cfg.get("continuity_clip_node_id") or cfg.get("continuity_frame_node_id") or "").strip()
    )
    sb_action = _storyboard_narrative_action(shot if isinstance(shot, dict) else None)
    action = str(
        sb_action
        or cfg.get("shot_action")
        or (shot or {}).get("character_action")
        or (shot or {}).get("comment")
        or ""
    ).strip()
    if not action:
        local = str(cfg.get("prompt") or "").strip()
        user = str(graph.get("description") or "")
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                looks_like_full_story_restatement,
                storyboard_fallback_beat,
            )

            if local and not looks_like_full_story_restatement(local, user) and len(local) <= 400:
                action = local
            elif ctx is not None:
                sb_text = str(role_output_text(ctx, NODE_ROLE_STORYBOARD) or "").strip()
                action = storyboard_fallback_beat(sb_text, shot_index)
        except Exception:  # noqa: BLE001
            if local and len(local) <= 220:
                action = local
    camera = str(
        (shot or {}).get("camera") or cfg.get("camera") or ""
    ).strip()
    speech_line = str(
        cfg.get("speech_line")
        or (shot or {}).get("speech_line")
        or (shot or {}).get("dialogue")
        or ""
    ).strip()
    story_lines: list[str] = [
        f"STORYBOARD SHOT (authoritative plot for shot {shot_index} — play this window only, "
        f"do not restage the full user prompt or other shots): "
        f"{action or 'this shot row only'}."
    ]
    if camera:
        story_lines.append(f"Camera for shot {shot_index}: {camera}")
    if speech_line:
        story_lines.append(f"Speech for shot {shot_index}: {speech_line}")
    parts: list[str] = [
        "\n".join(story_lines),
        _clip_prompt_lead(
            shot_index,
            duration,
            has_character=has_character,
            has_scene=has_scene,
            focus_names=focus_names,
            continuity=continuity,
            first_of_setting=first_of_setting,
        )
    ]
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_reference_binding import (
            build_wan_reference_binding,
        )

        binding = build_wan_reference_binding(cfg=cfg, graph=graph if isinstance(graph, dict) else {})
        if binding:
            parts.append(binding)
        extras = connected_clip_extra_images(ctx, node if isinstance(node, dict) else None)
        if extras:
            named = "; ".join(f'"{label}" ({path.name})' for label, path in extras)
            parts.append(
                "USER IMAGES wired to this clip sit after the character sheets and before the scene specs: "
                f"{named}. Put each subject's appearance from those images into this shot. Do not omit them."
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
            characters_from_graph,
            compact_wan_story_clause,
        )

        story_bit = compact_wan_story_clause(
            cfg,
            characters=characters_from_graph(graph if isinstance(graph, dict) else {}),
        )
        if story_bit and "ALREADY_DONE" not in "\n".join(parts) and "SEAT HOLDS" not in "\n".join(parts):
            parts.append(story_bit)
    except Exception:  # noqa: BLE001
        pass
    # One-line style from Brief only (not the full brief — that homogenizes all clips).
    if ctx is not None:
        brief = role_output_text(ctx, NODE_ROLE_BRIEF)
        if brief:
            for line in brief.splitlines():
                if "visual style" in line.lower() or line.lower().startswith("**visual"):
                    parts.append(line.strip())
                    break
    if action:
        parts.append(f"Primary action for shot {shot_index}: {action}")
    if camera:
        parts.append(f"Camera for shot {shot_index}: {camera}")
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    setting_id = str(cfg.get("setting_id") or (shot or {}).get("setting_id") or "").strip()
    if setting_id:
        parts.append(f"Setting lock for this clip only: {setting_id}.")
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
    if not bible and isinstance(identity.get("scene_specs"), dict):
        bible = identity["scene_specs"]
    if bible:
        parts.append(
            "SCENE SPECS (architecture/objects/light — keep; only animate this shot): "
            f"scene={bible.get('scene_name') or bible.get('place')}; lighting={bible.get('lighting')}; "
            f"objects={', '.join(str(x) for x in (bible.get('objects') or [])[:6])}; "
            f"crowd={bible.get('crowd')}."
        )
    view_key = str(cfg.get("view_key") or "").strip()
    if view_key:
        parts.append(f"Active view_key: {view_key}")
    costume_lock = str(identity.get("costume_lock") or cfg.get("costume_lock") or "").strip()
    if not costume_lock:
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                ensure_cfg_clothing_lock,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            costume_lock = ensure_cfg_clothing_lock(cfg, characters=analysis_chars)
        except Exception:  # noqa: BLE001
            costume_lock = ""
    if costume_lock:
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            clothing_lock_clause,
        )

        cloth = clothing_lock_clause(costume_lock, for_clip=True)
        parts.append(cloth if cloth else f"Costume / identity lock (do not redesign): {costume_lock}")
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
            ensure_cfg_staging_locks,
            staging_locks_from_cfg,
        )

        analysis_chars = list(
            ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
            or []
        )
        shot_row = shot if isinstance(shot, dict) else None
        # Staging must use the preferred narrative (storyboard), not stale cfg.shot_action.
        staging_cfg = dict(cfg)
        if action:
            staging_cfg["shot_action"] = action
        staging = ensure_cfg_staging_locks(
            staging_cfg, shot=shot_row, characters=analysis_chars
        ) or staging_locks_from_cfg(staging_cfg, for_clip=True)
        if staging and not any("STAGING LOCK" in p for p in parts):
            parts.append(staging)
    except Exception:  # noqa: BLE001
        pass
    char_nodes = [
        str(x)
        for x in (identity.get("character_node_ids") or cfg.get("character_node_ids") or [])
        if str(x).strip()
    ]
    cast_actions = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}
    if cast_actions:
        who = "; ".join(f"{cid}: {cast_actions[cid]}" for cid in cast_actions if cast_actions.get(cid))
        if who:
            parts.append(f"WHO DOES WHAT (on-screen only): {who}")
    on_screen = [str(x) for x in (cfg.get("on_screen") or []) if str(x).strip()]
    offscreen = [str(x) for x in (cfg.get("offscreen") or []) if str(x).strip()]
    if on_screen or offscreen:
        parts.append(
            f"ON-SCREEN: {on_screen or 'see cast_actions'}; OFFSCREEN (do not draw): {offscreen or 'none'}."
        )
    if char_nodes:
        parts.append(
            f"Use character reference sheets from nodes: {', '.join(char_nodes)}. "
            "Match faces and wardrobe exactly. One instance per person — no clones. "
            "Place them into the scene-card geography for this shot."
        )
    lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else None
    if not lock and action:
        from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock

        lock = infer_continuity_lock(action)
    if lock:
        from jiuwenswarm.server.runtime.designer.continuity import continuity_prompt_clause

        clause = continuity_prompt_clause(lock)
        if clause:
            parts.append(clause.strip())
    # THIS shot only — never dump the full storyboard (homogenizes / mixes scenes).
    if shot is not None:
        parts.append(_format_shot_block(shot, shot_index))
    else:
        parts.append(
            f"Storyboard shot for shot {shot_index} only "
            f"(action={action or 'see keyframe'}; camera={camera or 'match keyframe'})."
        )
    override = str((cfg.get("generate") or {}).get("prompt") or "").strip() if isinstance(cfg.get("generate"), dict) else ""
    # When live storyboard already provided the shot, skip stale generate.prompt narratives.
    if override and not sb_action:
        extracted = _extract_action_from_generate_prompt(override)
        if extracted and extracted.casefold() not in (action or "").casefold():
            if not action:
                action = extracted
                parts.append(f"Primary action for shot {shot_index}: {action}")
            elif not _looks_like_contaminated_prompt(override):
                parts.append(f"Director shot brief: {extracted[:400]}")
        elif (
            not extracted
            and not _looks_like_contaminated_prompt(override)
            and override.casefold() not in (action or "").casefold()
        ):
            parts.append(f"Director shot brief: {override[:400]}")
    occupancy = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    if occupancy:
        parts.append(
            f"OCCUPANCY: people in frame={occupancy.get('must_appear')}; "
            f"featured={occupancy.get('featured')}."
        )
    joined = "\n".join(parts)
    if "CONTINUITY STATE" not in joined:
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
                characters_from_graph,
                compact_wan_story_clause,
            )

            story_bit = compact_wan_story_clause(
                cfg,
                characters=characters_from_graph(graph if isinstance(graph, dict) else {}),
            )
            if story_bit and "CONTINUITY STATE" not in joined:
                parts.append(story_bit)
        except Exception:  # noqa: BLE001
            pass
    from jiuwenswarm.server.runtime.designer.user_references import (
        graph_user_references,
        prompt_slot_roster,
        user_reference_video_path,
        user_reference_audio_path,
    )

    roster = prompt_slot_roster(graph_user_references(graph))
    if roster:
        parts.append(
            "User reference slots (original files are visual/audio authority). "
            "Video and audio are generic references — not the first frame or keyframe:\n"
            f"{roster}"
        )
    if user_reference_video_path(graph) is not None:
        parts.append(
            "Attached video 1 is a motion/style reference only. "
            "Do not treat it as this shot's first frame."
        )
    if user_reference_audio_path(graph) is not None:
        parts.append(
            "Attached audio 1 is a soundtrack/voice reference only; "
            "do not invent a conflicting score."
        )
    # Locked speech / language / BGM (Director storyboard + Director).
    from jiuwenswarm.server.runtime.designer.audio_locks import (
        audio_lock_prompt_block,
        resolve_audio_intent_flags,
    )

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    flags = resolve_audio_intent_flags(meta, cfg if isinstance(cfg, dict) else {})
    # After scrub: do not re-inject cleared duplicate speech.
    speech_line_now = str(cfg.get("speech_line") or flags.get("speech_line") or "").strip()
    by_char_now = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    if not by_char_now:
        by_char_now = flags.get("speech_by_character") or {}
    include_speech = bool(speech_line_now or by_char_now) and not bool(
        cfg.get("speech_continuation_only") and not speech_line_now and not by_char_now
    )
    block = audio_lock_prompt_block(
        language_lock=str(flags.get("language_lock") or ""),
        speech_by_character=by_char_now if include_speech else {},
        speech_line=speech_line_now if include_speech else "",
        bgm_lock=flags.get("bgm_lock") or {},
        include_speech=include_speech,
        include_music=bool(flags.get("include_music")),
        clip_embedded=bool(flags.get("clip_embedded")),
    )
    if block and "LANGUAGE LOCK" not in "\n".join(parts) and "SPEECH LOCK" not in "\n".join(parts):
        parts.append(block)
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        director_approve_video_prompt,
    )

    extra_labels = [
        f"{label} ({path.name})"
        for label, path in connected_clip_extra_images(ctx, node if isinstance(node, dict) else None)
    ]
    approved, _notes = director_approve_video_prompt(
        action or "",
        cfg=cfg,
        graph=graph if isinstance(graph, dict) else {},
        shot_index=shot_index,
        action=action,
        camera=camera,
        extra_image_labels=extra_labels,
    )
    return approved


async def generate_clip_video(
    prompt: str,
    save_dir: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    duration: int = 5,
    audio: bool | None = None,
    size: str | None = None,
    resolution: str | None = None,
    model: str | None = None,
    force_reference_mode: bool = False,
) -> dict[str, Any]:
    """Call the Settings > Agent video model and wait for the clip. Tests monkeypatch this function."""
    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime
    from jiuwenswarm.server.runtime.designer import media_generation

    try:
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        logger.debug("Failed to reload video generation env before generation", exc_info=True)

    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
        resolve_clip_video_duration,
        resolve_clip_video_format,
    )

    if size or resolution:
        video_size, video_res = resolve_clip_video_format(
            user_resolution=str(resolution or ""),
            director_resolution=str(resolution or ""),
        )
        if size and "*" in str(size).replace("x", "*").replace("X", "*"):
            video_size = str(size).replace("x", "*").replace("X", "*")
    else:
        video_size, video_res = resolve_clip_video_format()
    clamped_duration = resolve_clip_video_duration(duration, default=5)
    resolved_model = (str(model).strip() or None) if model else None
    tool_input = {
        "prompt": prompt,
        "size": video_size,
        "resolution": video_res,
        "first_frame": first_frame,
        "reference_images": reference_images,
        "reference_file": reference_file,
        "duration": clamped_duration,
        "audio": audio,
        "model": resolved_model,
        "force_reference_mode": force_reference_mode,
    }
    from jiuwenswarm.server.runtime.designer.trajectory import (
        current_trajectory_span,
    )

    with current_trajectory_span(
        action="tool_call",
        tool="video_generation",
        phase="tool",
        detail={"input": tool_input},
    ):
        result = await media_generation.generate_video(
            media_generation.DesignerVideoRequest(
                prompt=prompt,
                duration=clamped_duration,
                size=video_size,
                resolution=video_res,
                first_frame=first_frame,
                reference_images=tuple(reference_images or ()),
                reference_file=reference_file,
                audio=bool(audio),
                reference_mode=force_reference_mode,
                model=resolved_model,
            ),
            save_dir=save_dir,
        )
    if "error" in result:
        raise RuntimeError(str(result["error"]))

    video_path = str(result.get("video_path") or "").strip()
    if not video_path:
        raise RuntimeError("video generation returned no video_path")
    return {**result, "video_path": str(Path(video_path).resolve())}


class ClipNodeHandler:
    """Submit one video job per storyboard shot."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        shot_index = node_shot_index(node)
        graph = ctx.graph if isinstance(ctx.graph, dict) else {}
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        cfg = dict(cfg)
        from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
            extract_last_frame,
            resolve_gated_last_frame_chain,
            scrub_restated_speech,
            stamp_last_frame_onto_next_clips,
            stamp_scene_last_frame_chain,
        )

        cfg = scrub_restated_speech(cfg)
        node["config"] = cfg

        refs = collect_clip_reference_images(
            ctx, shot_index, node=node
        )
        _IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
        ref_files = [p for p in refs if p.is_file() and p.suffix.lower() in _IMG]
        scene_chain = resolve_gated_last_frame_chain(cfg, graph=graph, ctx=ctx)
        if scene_chain:
            cfg = stamp_scene_last_frame_chain(cfg, scene_chain)
            node["config"] = cfg
        _, shot = _shot_for_node(ctx.graph, node, ctx)
        duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
        prompt = build_clip_prompt(ctx.graph, node, ctx)
        payload = connected_payload_clause(
            edge_text_inputs(ctx, node),
            edge_video_inputs(ctx, node),
        )
        if payload and payload not in prompt:
            prompt = f"{prompt.rstrip()}\n\n{payload}".strip()
        from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
            apply_regenerate_packet,
        )

        prompt, ref_files = apply_regenerate_packet(
            cfg,
            graph,
            ctx.run if isinstance(ctx.run, dict) else {},
            prompt=prompt,
            reference_paths=[str(p) for p in ref_files],
        )
        ref_files = [Path(p) for p in ref_files]
        from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
            apply_wan_call_locks,
        )

        prompt = apply_wan_call_locks(
            prompt,
            cfg=cfg,
            graph=graph,
            shot_index=shot_index,
        )
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
                resolve_prompt_limit,
                trim_prompt_to_limit,
            )

            vlim = resolve_prompt_limit("video")
            cfg["prompt_char_limit"] = vlim.max_chars
            prompt, _ = trim_prompt_to_limit(prompt, vlim)
        except Exception:  # noqa: BLE001
            pass
        if ctx is not None and callable(getattr(ctx, "on_prompt_artifact", None)):
            try:
                ctx.on_prompt_artifact(prompt)
            except Exception:  # noqa: BLE001
                logger.debug("clip early prompt handoff failed", exc_info=True)
        aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
        if not aspect:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
            video_format_for_node,
        )

        video_size, video_res = video_format_for_node(graph, cfg, aspect if isinstance(aspect, dict) else None)
        from jiuwenswarm.server.runtime.designer.user_references import (
            user_reference_video_path,
        )

        wired_video = first_connected_video_file(edge_video_inputs(ctx, node))
        user_video = user_reference_video_path(ctx.graph)
        reference_file = (
            wired_video
            if wired_video
            else (
                str(user_video.resolve())
                if user_video is not None and user_video.is_file()
                else None
            )
        )
        from jiuwenswarm.server.runtime.designer.audio_locks import resolve_video_audio_request

        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        want_audio, _model_override = resolve_video_audio_request(cfg, meta)
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            video_generation_overrides,
        )

        overrides = video_generation_overrides(
            cfg,
            graph,
            [str(p) for p in ref_files],
        )
        try:
            result = await generate_clip_video(
                prompt,
                first_frame=overrides["first_frame"],
                reference_images=overrides["reference_images"],
                reference_file=reference_file,
                duration=duration,
                size=video_size,
                resolution=video_res,
                audio=True if want_audio else False,
                model=None,
                force_reference_mode=overrides["force_reference_mode"],
            )
            path = Path(str(result["video_path"]))
            message = f"clip {shot_index} generated" + (" (with audio)" if want_audio else "")
            cfg["last_wan_prompt"] = str(prompt)[:4000]
            cfg["last_approved_prompt"] = str(prompt)[:4000]
            cfg["clip_prompt_preview"] = str(prompt)[:1200]
            node["config"] = cfg
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
                    remember_generation,
                )

                remember_generation(
                    node,
                    graph,
                    ctx.run if isinstance(ctx.run, dict) else {},
                    prompt=str(prompt),
                    reference_images=[str(p) for p in ref_files],
                )
            except Exception:  # noqa: BLE001
                logger.debug("regenerate packet stamp failed", exc_info=True)
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
                    stamp_wan_prompt_handoff,
                )

                stamp_wan_prompt_handoff(
                    graph,
                    shot_index=shot_index,
                    prompt=str(prompt),
                    node_id=str(node.get("id") or ""),
                    shot_action=str(cfg.get("shot_action") or ""),
                    speech_line=str(cfg.get("speech_line") or ""),
                )
            except Exception:  # noqa: BLE001
                logger.debug("post-clip wan-prompt stamp failed", exc_info=True)
            # Keep last-frame files for debug; do not attach them as next-clip refs.
            try:
                frame_out = path.with_name(f"{path.stem}_lastframe.jpg")
                extracted = extract_last_frame(path, dest=frame_out)
                if extracted is not None:
                    stamp_last_frame_onto_next_clips(
                        graph,
                        completed_clip_id=str(node.get("id") or ""),
                        last_frame_path=str(extracted),
                        shot_index=shot_index,
                        speech_line=str(
                            cfg.get("speech_line") or cfg.get("previous_clip_speech") or ""
                        ),
                    )
                    cfg["last_frame_path"] = str(extracted)
                    node["config"] = cfg
            except Exception:  # noqa: BLE001
                logger.debug("post-clip last-frame stamp failed", exc_info=True)
        except Exception as exc:
            raise RuntimeError(
                f"Video gen failed for shot {shot_index}; still→mp4 fallback is disabled: {exc}"
            ) from exc
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": path.name,
        }
        return NodeResult(output_ref=output_ref, message=message)
