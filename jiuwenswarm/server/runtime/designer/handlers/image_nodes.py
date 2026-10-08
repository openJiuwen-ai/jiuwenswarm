# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image intermediate handlers: character sheet, scene, and keyframe."""

from __future__ import annotations

from pathlib import Path
from shutil import copy2

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TEXT,
    DesignerGraphNode,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
from jiuwenswarm.server.runtime.designer.handlers.common import (
    collect_frame_reference_images,
    file_output_ref,
    graph_prompt,
    role_output_image_paths,
    role_output_text,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.user_references import (
    graph_user_references,
    prompt_slot_roster,
    wired_user_reference_images,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import collaboration_card
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    parse_storyboard_shots,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult


def _resolve_image_size(cfg: dict, graph: dict | None = None) -> str:
    """Prefer Director-stamped aspect_lock image_size (~1K), then node config."""
    aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
    if not aspect and isinstance(graph, dict):
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        if not aspect:
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
    return str(
        cfg.get("image_size")
        or (aspect or {}).get("image_size")
        or "1K"
    ).strip() or "1K"


def _frame_prompt_looks_contaminated(text: str) -> bool:
    raw = (text or "").upper()
    needles = (
        "PRIOR KEYFRAME PROMPT",
        "PREVIOUS KEYFRAME HAD",
        "PRIOR SHOT CONSISTENCY",
        "PREVIOUS CLIP HAD",
        "YOUR ASSIGNMENT",
        "STAGING LOCK",
        "MASTER SCENE PROMPT",
        "CHARACTER CONSISTENCY (MANAGER)",
        "LANGUAGE LOCK",
        "ASPECT LOCK",
        "STYLE LOCK",
        "SCENE SPECS",
        "SPEECH LOCK",
        "BGM LOCK",
    )
    if any(n in raw for n in needles):
        return True
    return len(text or "") > 500


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
            "into it at opening blocking. Identity from attached solo sheets "
            "(Image 1, Image 2, …). People are IN the scene — not an empty room and "
            "not a studio lineup. One instance per person. One clear image.\n"
            f"{source}"
        )
    return str(source or "").strip()


def _strip_markdown_tables(text: str) -> str:
    """Keep prose from Brief; drop markdown tables so the image model does not paint them."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.count("|") >= 2:
            continue
        if stripped.startswith("|") or stripped.endswith("|"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _shot_frame_prompt(
    shot: StoryboardShot,
    brief: str,
    *,
    has_character: bool,
    has_scene: bool,
    cast_names: list[str] | None = None,
    character_ref_count: int = 1,
    combined_cast_ref: bool = False,
    keyframe_strategy: str = "",
    costume_lock: str = "",
    staging_lock: str = "",
) -> str:
    timeline = f" ({shot['timeline']})" if shot["timeline"] else ""
    comment = str(shot.get("comment") or "").strip()
    names = [str(n).strip() for n in (cast_names or []) if str(n).strip()]
    who = ", ".join(names)
    lead = (
        f"Cinematic keyframe, one style-consistent still for shot {shot['shot_no']}{timeline}. "
        "Clear composition, this instant only, no comic grid."
    )
    if who:
        if len(names) > 1:
            lead += (
                f" The frame MUST clearly show all of these characters: {who}. "
                "Do not drop anyone listed."
            )
        else:
            lead += f" Feature this character: {who}."
    if comment:
        lead += f" Generate the keyframe from this shot description: {comment}."
    lead += (
        " Shot notes: "
        f"shot {shot['shot_no']}; "
        f"timeline {shot['timeline'] or 'unspecified'}; "
        f"camera {shot['camera'] or 'unspecified'}; "
        f"camera move {shot['move'] or 'unspecified'}; "
        f"character action {shot['character_action'] or 'unspecified'}; "
        f"scene change {shot['scene_change'] or 'unspecified'}."
    )
    n_refs = max(1, int(character_ref_count or 1))
    prior_edit = keyframe_strategy == "edit_prior_keyframe"
    if has_character and has_scene:
        if prior_edit:
            lead += (
                " This is image-to-image EDIT of the prior keyframe of the SAME setting "
                "(first reference). Keep architecture, lighting, landmarks, faces, and costumes "
                f"{f' for {who}' if who else ''}; only change camera/pose/blocking/who is "
                "on-screen for this shot. Additional refs may include the scene-master compose "
                "still (architecture lock) and solo cast sheets (identity only). "
                "Never regenerate the set; never jump to another setting."
            )
        elif combined_cast_ref or (len(names) > 1 and n_refs == 1):
            lead += (
                " This is image-to-image. The first reference is a combined cast postcard "
                f"(all of {who or 'the cast'} on one sheet); the second is the scene. "
                "Put ALL of those people into that scene together, matching each identity "
                "and costume from the postcard, plus location, lighting, and weather."
            )
        elif n_refs > 1:
            lead += (
                f" This is image-to-image. The first {n_refs} references are CANONICAL solo "
                f"cast sheets{f' for {who}' if who else ''}. COMPOSE a new SCENE MASTER still: "
                "generate the scene AND put every listed on-screen character into it. "
                "IDENTITY LOCK: same face, hair, body, and costume as each sheet — "
                "never redesign wardrobe between shots. No empty scene specs."
            )
        else:
            lead += (
                " This is image-to-image. The first reference is the canonical character sheet. "
                "Compose a SCENE MASTER still: generate the scene and put that character in it. "
                "Keep identity and costume from the sheet."
            )
    elif has_character:
        if prior_edit:
            lead += (
                " Edit the prior same-setting keyframe (first ref). Keep the scene and identity; "
                "only update this shot's action/framing."
            )
        else:
            lead += (
                " Compose SCENE MASTER from solo sheet(s): generate the setting and put "
                "the listed on-screen cast. Character look and costume must match the sheet(s)."
            )
    elif has_scene:
        lead += " Location, lighting, and weather must match the scene reference."
    if costume_lock:
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                clothing_lock_clause,
            )

            cloth = clothing_lock_clause(costume_lock, for_clip=False)
            lead += f" {cloth}" if cloth else f" Costume lock: {costume_lock}."
        except Exception:  # noqa: BLE001
            lead += f" Costume lock: {costume_lock}."
    if staging_lock:
        lead += f" {staging_lock}"
    lead += (
        " The image must be the cinematic scene itself. "
        "No subtitles, no storyboard grid, no table, no spreadsheet, no cell borders. "
        "Do not paint words like Shot, Timeline, Camera, Move, Character action, Scene change, or Comment."
    )
    lead += (
        " ANTI-CLONE: exactly one body per named character — never duplicate the same face "
        "in two places at once."
    )
    lead += (
        " CROWD LOCK: if the brief needs extras, show the SAME group layout "
        "across shots (same coats/positions). Do not empty the crowd in one "
        "shot and invent a new crowd in another. Featured cast must stay distinct from extras."
    )
    visual = _strip_markdown_tables(brief)
    if visual:
        return f"{lead}\nOverall visual style:\n{visual}"
    return lead


def _publish_shot_image(src: Path, *, stem: str) -> Path:
    dest = handler_io.get_agent_workspace_dir() / f"{stem}{src.suffix or '.png'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        copy2(src, dest)
    return dest.resolve()


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
    _IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    clean_refs: list[str] = []
    for raw in refs:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in _IMG:
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
            trim_prompt_to_limit,
        )

        lim = resolve_prompt_limit("image")
        prompt, _ = trim_prompt_to_limit(str(prompt or ""), lim)
    except Exception:  # noqa: BLE001
        pass

    generated = await handler_io.generate_designer_image(
        prompt,
        size=size,
        reference_images=clean_refs or None,
        max_tries=max_tries,
    )
    err = str((generated or {}).get("error") or "")
    # DashScope often rejects ref uploads ("Cannot determine file type") — retry T2I-only.
    # Reference-led stills fail closed instead of dropping the file.
    if (
        not keep_references
        and (not generated or not generated.get("image_path"))
        and clean_refs
        and (
            "file type" in err.lower() or "InvalidParameter" in err or "181001" in err
        )
    ):
        generated = await handler_io.generate_designer_image(
            prompt + " Match the described architecture and cast from text alone.",
            size=size,
            reference_images=None,
            max_tries=max(2, max_tries // 2),
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


def _aligned_source(ctx: NodeExecutionContext, role: str, node: DesignerGraphNode) -> str:
    # Brief is for the storyboard leaf. Character / scene prefer the node prompt,
    # collaboration card, or storyboard — not the full user brief.
    return (
        collaboration_card(ctx.run_id, role)
        or str((node.get("config") or {}).get("prompt") or "").strip()
        or role_output_text(ctx, NODE_ROLE_STORYBOARD)
        or graph_prompt(ctx.graph, node)
    )


def _with_card_ref(result: NodeResult, ctx: NodeExecutionContext, role: str) -> NodeResult:
    card = collaboration_card(ctx.run_id, role)
    if not card:
        return result
    path = write_workspace_text(f"designer_a2a_{ctx.run_id}_{role}", card)
    card_ref = file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown")
    refs = [ref for ref in (result.output_refs or []) if ref]
    primary = result.output_ref
    if primary is not None and primary not in refs:
        refs.insert(0, primary)
    if card_ref not in refs:
        refs.append(card_ref)
    return NodeResult(
        output_ref=primary,
        output_refs=refs or [card_ref],
        message=result.message,
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


class CharacterDesignNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, NODE_ROLE_CHARACTER_DESIGN, node)
        name = str(cfg.get("character_name") or node.get("label") or "Character")
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
        combined = bool(cfg.get("combined_cast"))
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        user_images = [str(path) for path in wired_user_reference_images(ctx, node)]
        task = str(cfg.get("reference_still_task") or "").strip()
        if task in {"identity_sheet", "medium_change"}:
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
                    apply_keyframe_call_locks,
                )

                prompt = apply_keyframe_call_locks(
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
        return _with_card_ref(result, ctx, NODE_ROLE_CHARACTER_DESIGN)


class SceneNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, NODE_ROLE_SCENE, node)
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
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
            refs = [str(path) for path in wired_user_reference_images(ctx, node)]
            char_nids = [
                str(x)
                for x in (
                    cfg.get("character_node_ids")
                    or ((cfg.get("identity_refs") or {}) if isinstance(cfg.get("identity_refs"), dict) else {}).get(
                        "character_node_ids"
                    )
                    or []
                )
                if str(x).strip()
            ]
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
                apply_keyframe_call_locks,
            )

            scene_prompt = apply_keyframe_call_locks(
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
        return _with_card_ref(result, ctx, NODE_ROLE_SCENE)


class FrameNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        shot_index = node_shot_index(node)
        storyboard = role_output_text(ctx, NODE_ROLE_STORYBOARD)
        all_chars = role_output_image_paths(ctx, NODE_ROLE_CHARACTER_DESIGN)
        all_scenes = role_output_image_paths(ctx, NODE_ROLE_SCENE)
        visual = storyboard or graph_prompt(ctx.graph, node)
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        keyframe_strategy = str(
            identity.get("keyframe_strategy") or cfg.get("keyframe_strategy") or ""
        )
        allow_without_scene = keyframe_strategy in {
            "compose_from_solo_refs",
            "edit_prior_keyframe",
        }
        if not all_chars or (not all_scenes and not allow_without_scene):
            missing = []
            if not all_chars:
                missing.append("Character")
            if not all_scenes and not allow_without_scene:
                missing.append("Scene")
            raise RuntimeError(
                "Keyframe generation must send "
                + " and ".join(missing)
                + " with this shot. Finish the Character and Scene nodes first."
            )
        refs_paths = collect_frame_reference_images(ctx, node)
        # Never dump every solo sheet when occupancy is empty/wrong — that paints
        # off-screen cast into the still. Prefer on_screen identity refs only.
        preferred_char_nodes = [
            str(x)
            for x in (
                (cfg.get("identity_refs") or {}).get("character_node_ids")
                if isinstance(cfg.get("identity_refs"), dict)
                else None
            )
            or (cfg.get("character_node_ids") or [])
            if str(x).strip()
        ]
        on_screen_cfg = [
            str(x)
            for x in (
                cfg.get("on_screen")
                or (identity.get("occupancy") or {}).get("must_appear")
                or cfg.get("character_ids")
                or identity.get("character_ids")
                or []
            )
            if str(x).strip()
        ]
        if refs_paths:
            refs = [str(p) for p in refs_paths]
        elif preferred_char_nodes:
            from jiuwenswarm.server.runtime.designer.handlers.common import (
                node_ids_output_image_paths,
            )

            refs = [str(p) for p in node_ids_output_image_paths(ctx, preferred_char_nodes)]
        else:
            # Fail closed: no occupancy → no cast refs (scene specs only if present).
            refs = [str(p) for p in (all_scenes[:1] if all_scenes else [])]
        shots = parse_storyboard_shots(storyboard)
        planned_action = str(cfg.get("shot_action") or generate.get("prompt") or "").strip()
        cast_names = [
            str(x).strip()
            for x in (cfg.get("cast_names") or [])
            if str(x).strip()
        ]
        costume_lock = str(identity.get("costume_lock") or cfg.get("costume_lock") or "")
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
                ensure_cfg_staging_locks,
            )

            analysis_chars = list(
                ((ctx.graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            staging_lock = ensure_cfg_staging_locks(cfg, characters=analysis_chars)
        except Exception:  # noqa: BLE001
            staging_lock = ""
        # Detect combined cast from attached character nodes when available.
        combined_cast_ref = False
        for other in ctx.graph.get("nodes") or []:
            if not isinstance(other, dict):
                continue
            if str(other.get("id") or "") not in preferred_char_nodes:
                continue
            oc = other.get("config") if isinstance(other.get("config"), dict) else {}
            if oc.get("combined_cast"):
                combined_cast_ref = True
                if not cast_names:
                    cast_names = [
                        str(x).strip()
                        for x in (oc.get("character_names") or [])
                        if str(x).strip()
                    ] or ([str(oc.get("character_name") or "").strip()] if oc.get("character_name") else [])
        # Ref count follows on_screen / cast_names, not every attached solo predecessor.
        char_ref_count = max(
            1,
            len(cast_names)
            or len(on_screen_cfg)
            or len(preferred_char_nodes)
            or 1,
        )
        if shot_index > len(shots) and planned_action:
            shot = {
                "shot_no": str(shot_index),
                "timeline": "",
                "camera": str(cfg.get("camera") or "medium / eye-level"),
                "move": "",
                "character_action": planned_action,
                "scene_change": "",
                "comment": planned_action,
            }
        elif shot_index > len(shots):
            raise RuntimeError(
                f"Shot {shot_index} is not in the storyboard ({len(shots)} shots)."
            )
        else:
            shot = dict(shots[shot_index - 1])
        override = handler_io.node_generate_prompt(node)
        planned = str(planned_action or shot.get("character_action") or shot.get("comment") or "").strip()
        if override and not _frame_prompt_looks_contaminated(override):
            shot["comment"] = override
        elif planned and not str(shot.get("comment") or "").strip():
            shot["comment"] = planned
        elif planned and _frame_prompt_looks_contaminated(str(shot.get("comment") or "")):
            shot["comment"] = planned
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                resolve_user_origin_prompt,
            )

            user_frame = resolve_user_origin_prompt(cfg, "")
        except Exception:  # noqa: BLE001
            user_frame = ""
        if user_frame:
            frame_prompt = user_frame
        else:
            frame_prompt = _shot_frame_prompt(
                shot,
                visual,
                has_character=bool(all_chars),
                has_scene=bool(all_scenes),
                cast_names=cast_names,
                character_ref_count=char_ref_count,
                combined_cast_ref=combined_cast_ref,
                keyframe_strategy=keyframe_strategy,
                costume_lock=costume_lock,
                staging_lock=staging_lock,
            )
        from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
            apply_keyframe_call_locks,
        )

        frame_prompt = apply_keyframe_call_locks(
            frame_prompt,
            cfg=cfg,
            graph=ctx.graph if isinstance(ctx.graph, dict) else {},
        )
        roster = prompt_slot_roster(graph_user_references(ctx.graph))
        if roster:
            frame_prompt = (
                f"{frame_prompt}\nUser reference slots (original files are visual authority):\n"
                f"{roster}"
            )
        emit = getattr(ctx, "emit_activity", None)
        if callable(emit):
            try:
                emit("stage", "calling image model", "image_gen")
            except TypeError:
                emit("stage", "calling image model", "image_gen")
        generated = await handler_io.generate_designer_image(
            frame_prompt,
            size=size,
            reference_images=refs,
            max_tries=max_tries,
        )
        if generated and generated.get("image_path"):
            path = _publish_shot_image(
                Path(generated["image_path"]),
                stem=f"designer_frame_{ctx.run_id}_{ctx.node_id}_shot{shot_index}",
            )
            ref = file_output_ref(path, kind=NODE_TYPE_IMAGE, mime_type="image/png")
            return NodeResult(
                output_ref=ref,
                output_refs=[ref],
                message=f"keyframe {shot_index} generated",
            )
        last_error = str((generated or {}).get("error") or "").strip()
        raise RuntimeError(
            f"keyframe {shot_index} image_gen failed: {last_error or 'no image_path'}"
        )
