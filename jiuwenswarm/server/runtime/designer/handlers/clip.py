# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clip node handler: generate one video per storyboard shot."""

from __future__ import annotations

import logging
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
from jiuwenswarm.server.runtime.designer.pipeline.reference_binding import (
    ClipReferenceError,
    ReferenceRole,
    ReferenceSlot,
    reindex_slots,
)

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


def _graph_node(ctx: NodeExecutionContext, node_id: str) -> DesignerGraphNode | None:
    return next(
        (
            n
            for n in (ctx.graph.get("nodes") or [])
            if isinstance(n, dict) and str(n.get("id") or "") == node_id
        ),
        None,
    )


def _declared_scene_node(
    ctx: NodeExecutionContext,
    cfg: dict[str, Any],
) -> DesignerGraphNode | None:
    """The scene node this clip names by ``scene_node_id`` or shares a ``setting_id`` with.

    A graph with a single scene node binds every clip of that (or no) setting
    to it. With several scenes and no declared relation there is no scene,
    never a guessed one.
    """
    scene_nid = str(
        cfg.get("scene_node_id")
        or (cfg.get("identity_refs") or {}).get("scene_node_id")
        or ""
    ).strip()
    if scene_nid:
        declared = _graph_node(ctx, scene_nid)
        if declared is not None:
            return declared
    scenes = [
        n
        for n in (ctx.graph.get("nodes") or [])
        if isinstance(n, dict) and node_pipeline(n) == NODE_ROLE_SCENE
    ]
    setting_id = str(cfg.get("setting_id") or "").strip()
    if setting_id:
        same_setting = next(
            (n for n in scenes if str((n.get("config") or {}).get("setting_id") or "").strip() == setting_id),
            None,
        )
        if same_setting is not None:
            return same_setting
    if len(scenes) != 1:
        return None
    sole_setting = str((scenes[0].get("config") or {}).get("setting_id") or "").strip()
    if setting_id and sole_setting and sole_setting != setting_id:
        return None
    return scenes[0]


def collect_clip_scene_image(
    ctx: NodeExecutionContext | None,
    clip_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> Path | None:
    """Resolve the setting's scene-card image (empty environment plate for R2V).

    None only when the clip has no scene at all. A declared scene without an
    image raises ``ClipReferenceError``; another setting's plate is never used.
    """
    del clip_index  # scene specs are keyed by setting / scene_node_id, not shot index
    if ctx is None:
        return None
    cfg = (
        node.get("config")
        if isinstance(node, dict) and isinstance(node.get("config"), dict)
        else {}
    )
    scene = _declared_scene_node(ctx, cfg)
    if scene is None:
        return None
    scene_id = str(scene.get("id") or "")
    paths = node_output_image_paths(ctx, scene_id)
    if not paths:
        raise ClipReferenceError(
            f"Scene node {scene_id} has no image yet; run it before this clip."
        )
    return paths[0]


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


def _character_slot_label(
    other: DesignerGraphNode | None,
    fallback: str,
    graph: DesignerExecutionGraph | None = None,
) -> str:
    """The cast name the clip prompt uses for this sheet, so body and binding agree."""
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import _name_for_id

    oc = other.get("config") if isinstance(other, dict) and isinstance(other.get("config"), dict) else {}
    names = oc.get("character_names") if isinstance(oc.get("character_names"), list) else []
    name = str(oc.get("character_name") or (names[0] if names else "") or "").strip()
    if name:
        return name
    ids = oc.get("character_ids") if isinstance(oc.get("character_ids"), list) else []
    token = str(ids[0] if ids else "").strip()
    return (_name_for_id(token, graph) if token else "") or fallback


def _scene_slot_label(other: DesignerGraphNode | None, fallback: str) -> str:
    """The place the scene plate shows; the node label when the plate names no place."""
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        visual_place_name,
    )

    oc = other.get("config") if isinstance(other, dict) and isinstance(other.get("config"), dict) else {}
    specs = oc.get("scene_specs") if isinstance(oc.get("scene_specs"), dict) else {}
    return visual_place_name(specs.get("scene_name")) or fallback


def _wired_scene_ids_without_image(
    ctx: NodeExecutionContext,
    node: DesignerGraphNode | None,
) -> list[str]:
    """Scene nodes wired into this clip by a data edge that have no image output."""
    from jiuwenswarm.common.schema.designer_graph import EDGE_KIND_DATA, edge_kind

    node_id = str((node or {}).get("id") or ctx.node_id or "")
    missing: list[str] = []
    for edge in ctx.graph.get("edges") or []:
        if not isinstance(edge, dict) or edge_kind(edge) != EDGE_KIND_DATA:
            continue
        if str(edge.get("target") or "") != node_id:
            continue
        source_id = str(edge.get("source") or "").strip()
        source = _graph_node(ctx, source_id) if source_id else None
        if source is None or node_pipeline(source) != NODE_ROLE_SCENE:
            continue
        if not node_output_image_paths(ctx, source_id) and source_id not in missing:
            missing.append(source_id)
    return missing


def collect_clip_reference_slots(
    ctx: NodeExecutionContext | None,
    clip_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> list[ReferenceSlot]:
    """Wan reference_images with their roles: character specs, user images, scene spec last.

    Design clips always attach these as R2V refs — never as a single
    first-frame image. Order matches Wan R2V labeling: character specs are
    character1…, uploads and user-wired image nodes follow, and the scene spec
    stays last as the environment. The slots carry what each image is so the
    prompt binding can be rendered from the files actually sent.

    Raises:
        ClipReferenceError: a scene the clip is wired to or declares has no image.
    """
    from jiuwenswarm.server.runtime.designer.handlers.common import uploaded_material_image_paths

    attached = uploaded_material_image_paths(node if isinstance(node, dict) else None)

    entries: list[tuple[ReferenceRole, str, Path, str | None]] = []
    seen: set[str] = set()

    def add(role: ReferenceRole, label: str, path: Path | None, node_id: str | None = None) -> None:
        if path is None:
            return
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        entries.append((role, label, resolved, node_id))

    def slots() -> list[ReferenceSlot]:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
            cap_r2v_reference_paths,
        )

        by_path = {str(path): (role, label, nid) for role, label, path, nid in entries}
        kept = cap_r2v_reference_paths([path for _r, _l, path, _n in entries])
        out: list[ReferenceSlot] = []
        for index, path in enumerate(kept, start=1):
            role, label, nid = by_path[str(path)]
            out.append(ReferenceSlot(index=index, role=role, label=label, path=path, node_id=nid))
        return out

    if ctx is None:
        return []

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
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs

    outputs = predecessor_outputs(ctx, node if isinstance(node, dict) else None)
    if outputs is not None:
        missing = _wired_scene_ids_without_image(ctx, node)
        if missing:
            raise ClipReferenceError(
                f"Scene node {', '.join(missing)} has no image yet; run it before this clip."
            )
        images = [item for item in outputs if item.kind == "image" and item.path is not None]
        for item in images:
            if _flow_role_bucket(item.role) == "character":
                label = _character_slot_label(_graph_node(ctx, item.node_id), item.label, ctx.graph)
                add("character", label, item.path, item.node_id)
        for path in attached:
            add("upload", path.stem, path)
        for item in images:
            if _flow_role_bucket(item.role) == "other":
                add("other", item.label, item.path, item.node_id)
        for item in images:
            if _flow_role_bucket(item.role) == "scene":
                add("scene", _scene_slot_label(_graph_node(ctx, item.node_id), item.label),
                    item.path, item.node_id)
        return slots()

    if solo_nids:
        character_nids = solo_nids
    elif not on_screen:
        character_nids = [
            str(other.get("id") or "")
            for other in graph.get("nodes") or []
            if isinstance(other, dict) and node_pipeline(other) == NODE_ROLE_CHARACTER
        ]
    else:
        character_nids = []
    character_paths = 0
    for nid in character_nids:
        other = _graph_node(ctx, nid)
        label = _character_slot_label(other, str((other or {}).get("label") or nid), ctx.graph)
        for path in node_output_image_paths(ctx, nid):
            if not solo_nids and character_paths >= 4:
                break
            add("character", label, path, nid)
            character_paths += 1

    for path in attached:
        add("upload", path.stem, path)

    scene = collect_clip_scene_image(ctx, clip_index, node=node)
    # Scene specs is the last environment reference for every clip.
    if scene is not None:
        scene_node = _declared_scene_node(ctx, cfg)
        scene_id = str((scene_node or {}).get("id") or "") or None
        add("scene", _scene_slot_label(scene_node, str((scene_node or {}).get("label") or "")),
            scene, scene_id)
    return slots()


def collect_clip_reference_images(
    ctx: NodeExecutionContext | None,
    clip_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> list[Path]:
    """File paths of :func:`collect_clip_reference_slots`, in attach order."""
    return [slot.path for slot in collect_clip_reference_slots(ctx, clip_index, node=node)]


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


def attachable_slots(slots: list[ReferenceSlot]) -> list[ReferenceSlot]:
    """Slots whose file can be sent as a reference image, renumbered in order."""
    kept = [slot.path for slot in slots if slot.path.is_file() and slot.path.suffix.lower() in _IMAGE_SUFFIXES]
    return reindex_slots(slots, kept)


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


def build_clip_prompt(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
    slots: list[ReferenceSlot] | None = None,
) -> str:
    """Use the authored prompt, or seed a new prompt from this storyboard row.

    Only a missing draft is composed. Existing prose is never classified or
    replaced based on its language, wording, or similarity to other shots.
    """
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
        resolve_user_origin_prompt,
    )

    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    user_text = resolve_user_origin_prompt(cfg)
    generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    # generate_node_output injects this call's authored text into cfg.prompt.
    for candidate in (user_text, cfg.get("prompt"), generate.get("prompt")):
        if candidate is None or candidate == "":
            continue
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Video prompt must be a non-empty string")
        return candidate.strip()
    _clip_index, shot = _shot_for_node(graph, node, ctx)
    action = _storyboard_narrative_action(shot) or str(cfg.get("clip_action") or "").strip()
    if not action:
        raise ValueError("Video prompt or storyboard action required")
    return compose_practice_prompt(
        cfg=cfg,
        graph=graph,
        action=action,
        camera=str((shot or {}).get("camera") or cfg.get("camera") or ""),
        slots=slots,
    )


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
            scrub_restated_speech,
            stamp_speech_onto_next_clips,
        )

        cfg = scrub_restated_speech(cfg)
        node["config"] = cfg

        slots = attachable_slots(collect_clip_reference_slots(ctx, clip_index, node=node))
        ref_files = [slot.path for slot in slots]
        duration = video_duration_seconds(ctx, node)
        note_video_duration(ctx, node, duration)
        prompt = build_clip_prompt(ctx.graph, node, ctx, slots=slots)
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
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            video_generation_overrides,
        )
        from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
            apply_wan_call_locks,
        )

        overrides = video_generation_overrides(
            cfg,
            graph,
            [str(p) for p in ref_files],
        )
        prompt = apply_wan_call_locks(
            prompt,
            cfg=cfg,
            graph=graph,
            clip_index=clip_index,
            slots=reindex_slots(slots, overrides["reference_images"] or []),
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
        try:
            from jiuwenswarm.server.runtime.designer.prompt_publish import emit_prompt_artifact

            emit_prompt_artifact(ctx, prompt)
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
            try:
                stamp_speech_onto_next_clips(
                    graph,
                    completed_clip_id=str(node.get("id") or ""),
                    clip_index=clip_index,
                    speech_line=str(
                        cfg.get("speech_line") or cfg.get("previous_clip_speech") or ""
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("post-clip speech handoff stamp failed", exc_info=True)
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
