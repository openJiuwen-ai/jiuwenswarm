# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image intermediate handlers: character spec sheet and scene spec."""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_TYPE_IMAGE,
    DesignerGraphNode,
)
from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    role_output_text,
)
from jiuwenswarm.server.runtime.designer.user_references import (
    graph_user_references,
    identity_reference_images_for_node,
    prompt_slot_roster,
    wired_user_reference_images,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult
from jiuwenswarm.server.runtime.designer.media_settings import image_settings


logger = logging.getLogger(__name__)


def _drop_story_context(source: str) -> str:
    """Solo sheets must not receive the scene paragraph or a 'Story context' dump."""
    import re

    kept: list[str] = []
    for line in str(source or "").splitlines():
        if re.search(r"(?i)\bstory context\s*:", line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _character_prompt(source: str, *, combined_cast: bool = False) -> str:
    cleaned = _drop_story_context(source)
    if combined_cast:
        return (
            "Combined cast postcard: all listed characters side-by-side on one sheet, "
            "full or three-quarter body each, consistent scale, plain empty backdrop. "
            "Clear identity for each person. One clear image.\n"
            f"{cleaned}"
        )
    return (
        "One person only on a plain empty studio backdrop, solid neutral background. "
        "Full or three-quarter body. Identity and costume only.\n"
        f"{cleaned}"
    )


def _scene_prompt(source: str, *, derive_from_master: bool = False, composed: bool = True) -> str:
    if derive_from_master:
        return (
            "EDIT / REFRAME the provided master composed scene. "
            "Same landmarks, buildings, terrain, props, lighting, and placed cast identities. "
            "Only change camera angle/framing for this shot if asked. "
            "One clear image.\n"
            f"{source}"
        )
    if composed:
        return (
            "COMPOSED SCENE MASTER: generate the SETTING and put ALL listed characters "
            "into it at opening blocking. Identity from attached character specs "
            "(Image 1, Image 2, …). People are IN the scene — not an empty room and "
            "not a studio lineup. One instance per person. One clear image.\n"
            f"{source}"
        )
    return str(source or "").strip()


async def _require_image(
    *,
    prompt: str,
    stem: str,
    reference_images: list[str] | None = None,
    size: str = "1024x1024",
    max_tries: int = 2,
    ctx: NodeExecutionContext | None = None,
    keep_references: bool = False,
) -> NodeResult:
    """Generate an image; fail closed when the image model returns nothing."""
    refs = [str(p) for p in (reference_images or []) if str(p).strip()]
    # Only pass real image files — markdown/extra stubs break DashScope uploads.
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    clean_refs: list[str] = []
    for raw in refs:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in image_suffixes:
            clean_refs.append(str(path.resolve()))
    clean_refs = clean_refs[:3]
    emit = getattr(ctx, "emit_activity", None) if ctx is not None else None
    if callable(emit):
        try:
            emit("stage", "calling image model", "image_gen")
        except TypeError:
            emit("stage", "calling image model", "image_gen")

    try:
        from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
            resolve_prompt_limit,
        )

        lim = resolve_prompt_limit("image")
        text_len = len(str(prompt or ""))
        if lim.max_chars and text_len > lim.max_chars:
            logger.warning(
                "image prompt length=%s exceeds limit %s (model=%s); passing as-is",
                text_len,
                lim.max_chars,
                lim.model,
            )
    except Exception:  # noqa: BLE001
        pass

    save_dir = None
    if ctx is not None:
        save_dir = str(handler_io.canvas_output_dir(ctx.graph if isinstance(ctx.graph, dict) else None))
    generated = await handler_io.generate_designer_image(
        prompt,
        size=size,
        reference_images=clean_refs or None,
        max_tries=max_tries,
        save_dir=save_dir,
    )
    err = str((generated or {}).get("error") or "")
    # DashScope often rejects ref uploads ("Cannot determine file type") — retry T2I-only.
    # Reference-led stills fail closed instead of dropping the file.
    refs_rejected = not (generated or {}).get("image_path") and (
        "file type" in err.lower() or "InvalidParameter" in err or "181001" in err
    )
    if not keep_references and clean_refs and refs_rejected:
        generated = await handler_io.generate_designer_image(
            prompt + " Match the described architecture and cast from text alone.",
            size=size,
            reference_images=None,
            max_tries=max(2, max_tries // 2),
            save_dir=save_dir,
        )
    if generated and generated.get("image_path"):
        path = Path(generated["image_path"])
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
            message="image generated",
        )
    error = str((generated or {}).get("error") or "").strip()
    raise RuntimeError(
        f"image_gen required but failed for {stem}: {error or 'no image_path'}"
    )


def _aligned_source(ctx: NodeExecutionContext, node: DesignerGraphNode) -> str:
    # The node prompt can be cleared from the toolbar or chat; the brief then
    # carries the cast and setting descriptions.
    return (
        str((node.get("config") or {}).get("prompt") or "").strip()
        or role_output_text(ctx, NODE_ROLE_BRIEF)
        or graph_prompt(ctx.graph, node)
    )


def _references_for_paths(graph: object, paths: list[str]) -> list[dict]:
    """Roster entries for the upload files this node actually received."""
    wanted: set[str] = set()
    for raw in paths:
        candidate = Path(str(raw))
        if candidate.is_file():
            wanted.add(str(candidate.resolve()))
    if not wanted or not isinstance(graph, dict):
        return []
    kept: list[dict] = []
    for item in graph_user_references(graph):
        candidate = Path(str(item.get("path") or item.get("uri") or ""))
        if candidate.is_file() and str(candidate.resolve()) in wanted:
            kept.append(item)
    return kept


def _still_user_references(ctx: NodeExecutionContext, node: DesignerGraphNode) -> list[str]:
    """Upload files for this still: edge-wired first, then the un-wired identity upload.

    An upload added on the canvas never gets an edge until the supervisor labels
    it. Without this the still silently degraded to text-to-image.
    """
    wired = [str(path) for path in wired_user_reference_images(ctx, node)]
    if wired:
        return wired
    fallback = [str(path) for path in identity_reference_images_for_node(ctx, node)]
    if fallback:
        logger.info(
            "still %s uses %d upload(s) that arrived on no edge",
            getattr(ctx, "node_id", ""),
            len(fallback),
        )
    return fallback


def _note_unused_references(
    ctx: NodeExecutionContext | None,
    node: DesignerGraphNode,
    used: list[str],
) -> None:
    """Tell the user when their upload cannot reach this still, instead of dropping it."""
    if used or ctx is None:
        return
    graph = ctx.graph if isinstance(ctx.graph, dict) else {}
    if not graph_user_references(graph):
        return
    label = str(node.get("label") or ctx.node_id or "still").strip()
    text = (
        f"{label}: the uploaded reference image is not wired to this node, "
        "so this still is generated from text only"
    )
    logger.warning(text)
    emit = getattr(ctx, "emit_activity", None)
    if not callable(emit):
        return
    try:
        emit("stage", text, "reference_image")
    except Exception:  # noqa: BLE001
        logger.debug("reference notice failed", exc_info=True)


class CharacterDesignNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, node)
        name = str(cfg.get("character_name") or node.get("label") or "Character")
        size = image_settings(ctx.graph).size
        combined = bool(cfg.get("combined_cast"))
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        user_images = _still_user_references(ctx, node)
        _note_unused_references(ctx, node, user_images)
        task = str(cfg.get("reference_image_task") or "").strip()
        if task in {"character_specs", "medium_change"}:
            from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
                still_task_prompt,
            )

            prompt = still_task_prompt(cfg)
        else:
            roster = prompt_slot_roster(_references_for_paths(ctx.graph, user_images))
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                    resolve_user_origin_prompt,
                )

                user_prompt = resolve_user_origin_prompt(cfg, "")
            except Exception:  # noqa: BLE001
                user_prompt = ""
            if user_prompt:
                prompt = user_prompt
            else:
                prompt = _character_prompt(f"{name}\n{source}", combined_cast=combined)
            # No hard-coded ensure_still rewrite — user/LLM text is authority.
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
                    apply_image_call_locks,
                )

                prompt = apply_image_call_locks(
                    prompt, cfg=cfg, graph=ctx.graph if isinstance(ctx.graph, dict) else {}
                )
            except Exception:  # noqa: BLE001
                pass
            if roster:
                prompt = (
                    f"{prompt}\nUser reference slots (original files are visual authority):\n{roster}"
                )
        result = await _require_image(
            prompt=prompt,
            stem=f"designer_character_{ctx.run_id}_{ctx.node_id}",
            size=size,
            max_tries=max_tries,
            reference_images=user_images or None,
            ctx=ctx,
            keep_references=bool(cfg.get("require_reference_images")),
        )
        return result


class SceneNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, node)
        size = image_settings(ctx.graph).size
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        strategy = str(cfg.get("scene_strategy") or "").strip()
        derive = strategy == "edit_master_view"
        refs: list[str] = []
        master_id = str(cfg.get("master_scene_node_id") or "n_scene").strip()
        if derive:
            from jiuwenswarm.server.runtime.designer.handlers.common import (
                node_ids_output_image_paths,
            )

            master_paths = node_ids_output_image_paths(ctx, [master_id])
            refs = [str(p) for p in master_paths]
            if not refs:
                derive = False
            else:
                lock = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
                if lock:
                    arch = str(lock.get("architecture") or lock.get("setting") or "").strip()
                    if arch:
                        source = f"{source}\n{arch}"
        else:
            refs = _still_user_references(ctx, node)
            _note_unused_references(ctx, node, refs)
            identity_refs = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
            char_nids_raw = cfg.get("character_node_ids") or identity_refs.get("character_node_ids") or []
            char_nids = [str(x) for x in char_nids_raw if str(x).strip()]
            if char_nids and bool(cfg.get("composed_scene")):
                from jiuwenswarm.server.runtime.designer.handlers.common import (
                    node_ids_output_image_paths,
                )

                refs = [str(p) for p in node_ids_output_image_paths(ctx, char_nids)] + refs
        composed = bool(cfg.get("composed_scene", False)) and not derive
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                resolve_user_origin_prompt,
            )

            user_prompt = resolve_user_origin_prompt(cfg, "")
        except Exception:  # noqa: BLE001
            user_prompt = ""
        if user_prompt:
            scene_prompt = user_prompt
        else:
            scene_prompt = _scene_prompt(
                source, derive_from_master=derive, composed=composed
            )
        # No hard-coded ensure_still rewrite — user/LLM text is authority.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
                apply_image_call_locks,
            )

            scene_prompt = apply_image_call_locks(
                scene_prompt, cfg=cfg, graph=ctx.graph
            )
        except Exception:  # noqa: BLE001
            pass
        roster = prompt_slot_roster(_references_for_paths(ctx.graph, refs))
        if roster:
            scene_prompt = (
                f"{scene_prompt}\nUser reference slots (original files are visual authority):\n"
                f"{roster}"
            )
        result = await _require_image(
            prompt=scene_prompt,
            stem=f"designer_scene_{ctx.run_id}_{ctx.node_id}",
            size=size,
            max_tries=max_tries,
            reference_images=refs or None,
            ctx=ctx,
        )
        return result
