# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clip node handler: generate one video per storyboard shot."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    NODE_ROLE_CHARACTER,
    NODE_ROLE_CLIP,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_VIDEO,
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_pipeline,
    node_clip_index,
    video_concat_source_ids,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    canvas_output_dir,
    node_output_image_paths,
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
        logger.debug("imageio_ffmpeg unavailable for image→mp4", exc_info=True)
        return None



def generates_timed_video(graph: DesignerExecutionGraph, node: DesignerGraphNode) -> bool:
    """True when running ``node`` asks the video model for a clip of some length."""
    from jiuwenswarm.server.runtime.designer.handlers import resolve_handler_key

    key = resolve_handler_key(node)
    if key == NODE_ROLE_CLIP:
        return True
    if key != NODE_TYPE_VIDEO:
        return False
    return not video_concat_source_ids(graph, str(node.get("id") or ""))


def video_duration_seconds(ctx: NodeExecutionContext | None, node: DesignerGraphNode) -> int:
    """Seconds a video node is generated at, snapped to the model.

    A manual ``config.duration_sec`` wins; a storyboard clip otherwise follows its
    shot's Timeline (5s without one); any other video uses the model default.
    """
    from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import clamp_clip_duration
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import active_video_capacity

    requested = requested_video_seconds(ctx, node)
    if requested is not None:
        return clamp_clip_duration(requested)
    if node_pipeline(node) == NODE_ROLE_CLIP:
        return clamp_clip_duration(5)
    return clamp_clip_duration(active_video_capacity().default_sec)


def requested_video_seconds(
    ctx: NodeExecutionContext | None, node: DesignerGraphNode
) -> float | None:
    """Length the node asks for before it is snapped to the video model.

    ``config.duration_sec``, else a storyboard clip's Timeline window; None when
    neither is set.
    """
    from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import timeline_seconds

    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    override = cfg.get("duration_sec")
    if isinstance(override, (int, float)) and not isinstance(override, bool):
        return float(override)
    if node_pipeline(node) == NODE_ROLE_CLIP:
        _, shot = _shot_for_node(ctx.graph if ctx is not None else {}, node, ctx)
        return timeline_seconds((shot or {}).get("timeline") or "")
    return None


def note_video_duration(
    ctx: NodeExecutionContext | None, node: DesignerGraphNode, duration: int
) -> None:
    """Tell the node card when the video model cannot make the length the node asks for."""
    if ctx is None or not callable(ctx.emit_activity):
        return
    requested = requested_video_seconds(ctx, node)
    if requested is None or requested <= 0 or round(requested) == duration:
        return
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        active_video_capacity,
    )

    capacity = active_video_capacity()
    ctx.emit_activity(
        ACTIVITY_KIND_STAGE,
        f"{capacity.model_id} makes {capacity.min_sec}-{capacity.max_sec}s clips: "
        f"generating {duration}s instead of the requested {requested:g}s",
        "video_gen",
    )


def collect_clip_scene_image(
    ctx: NodeExecutionContext | None,
    clip_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> Path | None:
    """Resolve the setting's scene-card image (empty environment plate for R2V)."""
    del clip_index  # scene specs are keyed by setting / scene_node_id, not shot index
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
    *,
    exclude_roles: frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    """Text bodies that arrive on incoming data edges, in edge order."""
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None) or []
    texts: list[tuple[str, str]] = []
    for item in outputs:
        if item.kind != "text" or item.role in exclude_roles:
            continue
        body = item.text.strip()
        if body:
            texts.append((item.label, body))
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
    if role in {NODE_ROLE_CHARACTER, "character"}:
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
    clip_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> list[Path]:
    """Wan reference_images: on-screen character specs, then user images, then the scene spec.

    Design clips always attach these as R2V refs — never as a single
    first-frame image. Order matches Wan R2V labeling: character specs are
    character1…, uploads and user-wired image nodes follow, and the scene spec
    stays last as the environment.
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
        if node_pipeline(other) != NODE_ROLE_CHARACTER:
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
        for path in role_output_image_paths(ctx, NODE_ROLE_CHARACTER)[:4]:
            add(path)

    for path in attached:
        add(path)

    scene = collect_clip_scene_image(ctx, clip_index, node=node)
    # Scene specs is the last environment reference for every clip.
    add(scene)
    return cap_r2v_reference_paths(paths)


def edge_storyboard_text(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode | None,
) -> str:
    """The storyboard table that arrives on this clip's incoming data edge."""
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None) or []
    for item in outputs:
        if item.kind == "text" and item.role == NODE_ROLE_STORYBOARD and item.text.strip():
            return item.text
    return ""


def _storyboard_narrative_action(shot: dict[str, Any] | None) -> str:
    if not isinstance(shot, dict):
        return ""
    return str(shot.get("character_action") or "").strip()


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
    index = node_clip_index(node)
    shots = parse_storyboard_shots(edge_storyboard_text(ctx, node))
    if shots and 1 <= index <= len(shots):
        return index, shots[index - 1]
    return index, None


def _looks_like_contaminated_prompt(text: str) -> bool:
    """True only for pasted handoff/assignment dumps — not normal SCENE SPECS / staging."""
    raw = (text or "").upper()
    needles = (
        "PRIOR CLIP PROMPT",
        "PREVIOUS CLIP HAD",
        "PRIOR SHOT CONSISTENCY",
        "PREVIOUS CLIP HAD",
        "YOUR ASSIGNMENT",
        "MASTER SCENE PROMPT",
        "CHARACTER CONSISTENCY (MANAGER)",
        "PRIOR CLIP WAN",
        "PREVIOUS WAN PROMPT",
    )
    return any(n in raw for n in needles)


def build_clip_prompt(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
) -> str:
    """Shot-specific clip prompt. Prefer shot row over full Brief to avoid identical clips."""
    clip_index, shot = _shot_for_node(graph, node, ctx)
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
    sb_action = _storyboard_narrative_action(shot if isinstance(shot, dict) else None)
    action = str(sb_action or cfg.get("clip_action") or "").strip()
    if not action:
        local = str(cfg.get("prompt") or "").strip()
        user = str(graph.get("description") or "")
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import (
                looks_like_full_story_restatement,
            )

            if local and not looks_like_full_story_restatement(local, user) and len(local) <= 400:
                action = local
        except Exception:  # noqa: BLE001
            if local and len(local) <= 220:
                action = local
    camera = str(
        (shot or {}).get("camera") or cfg.get("camera") or ""
    ).strip()
    generate_cfg = cfg.get("generate")
    override = str(generate_cfg.get("prompt") or "").strip() if isinstance(generate_cfg, dict) else ""
    if override and not sb_action and not action:
        extracted = _extract_action_from_generate_prompt(override)
        if extracted:
            action = extracted
    if not str(cfg.get("costume_lock") or "").strip():
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                ensure_cfg_clothing_lock,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            ensure_cfg_clothing_lock(cfg, characters=analysis_chars)
        except Exception:  # noqa: BLE001
            logger.debug("ensure_cfg_clothing_lock failed in build_clip_prompt", exc_info=True)
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
        clip_index=clip_index,
        action=action,
        camera=camera,
        extra_image_labels=extra_labels,
    )
    return approved


async def generate_clip_video(
    prompt: str,
    *,
    aspect_ratio: str,
    resolution: str,
    save_dir: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    duration: int = 5,
    audio: bool | None = None,
    model: str | None = None,
    force_reference_mode: bool = False,
    node_label: str = "",
) -> dict[str, Any]:
    """Call the Settings > Agent video model and wait for the clip. Tests monkeypatch this function.

    ``aspect_ratio`` / ``resolution`` are the canvas video settings (``media_settings``).
    ``node_label`` names the node in the per-node Settings-gap warning, so a
    blocked clip is traceable in the log for first runs and regenerates alike.
    """
    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime
    from jiuwenswarm.server.runtime.designer import media_generation

    try:
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        logger.debug("Failed to reload video generation env before generation", exc_info=True)

    media_generation.note_generation_problem("video", node_label or "clip")

    from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import clamp_clip_duration

    clamped_duration = clamp_clip_duration(duration)
    resolved_model = (str(model).strip() or None) if model else None
    tool_input = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
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
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                duration=clamped_duration,
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
        clip_index = node_clip_index(node)
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
            ctx, clip_index, node=node
        )
        image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
        ref_files = [p for p in refs if p.is_file() and p.suffix.lower() in image_suffixes]
        scene_chain = resolve_gated_last_frame_chain(cfg, graph=graph, ctx=ctx)
        if scene_chain:
            cfg = stamp_scene_last_frame_chain(cfg, scene_chain)
            node["config"] = cfg
        duration = video_duration_seconds(ctx, node)
        note_video_duration(ctx, node, duration)
        prompt = build_clip_prompt(ctx.graph, node, ctx)
        payload = connected_payload_clause(
            edge_text_inputs(ctx, node, exclude_roles=frozenset({NODE_ROLE_STORYBOARD})),
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
            clip_index=clip_index,
        )
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
                resolve_prompt_limit,
            )

            vlim = resolve_prompt_limit("video")
            cfg["prompt_char_limit"] = vlim.max_chars
            if vlim.max_chars and len(prompt) > vlim.max_chars:
                logger.warning(
                    "clip prompt length=%s exceeds limit %s (model=%s); passing as-is",
                    len(prompt),
                    vlim.max_chars,
                    vlim.model,
                )
        except Exception:  # noqa: BLE001
            pass
        if ctx is not None and callable(getattr(ctx, "on_prompt_artifact", None)):
            try:
                ctx.on_prompt_artifact(prompt)
            except Exception:  # noqa: BLE001
                logger.debug("clip early prompt handoff failed", exc_info=True)
        from jiuwenswarm.server.runtime.designer.media_settings import video_settings

        video = video_settings(graph)
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
                save_dir=str(canvas_output_dir(graph if isinstance(graph, dict) else None)),
                first_frame=overrides["first_frame"],
                reference_images=overrides["reference_images"],
                reference_file=reference_file,
                duration=duration,
                aspect_ratio=video.aspect_ratio,
                resolution=video.resolution,
                audio=True if want_audio else False,
                model=None,
                force_reference_mode=overrides["force_reference_mode"],
                node_label=f"designer_clip_{ctx.run_id}_{ctx.node_id}",
            )
            path = Path(str(result["video_path"]))
            message = f"clip {clip_index} generated" + (" (with audio)" if want_audio else "")
            cfg["last_wan_prompt"] = str(prompt)
            cfg["last_approved_prompt"] = str(prompt)
            cfg["clip_prompt_preview"] = str(prompt)
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
                    clip_index=clip_index,
                    prompt=str(prompt),
                    node_id=str(node.get("id") or ""),
                    clip_action=str(cfg.get("clip_action") or ""),
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
                        clip_index=clip_index,
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
                f"Video gen failed for shot {clip_index}; image→mp4 fallback is disabled: {exc}"
            ) from exc
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": path.name,
        }
        return NodeResult(output_ref=output_ref, message=message)
