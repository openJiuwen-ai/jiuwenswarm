# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reference-led graphs: one role list and one binding per uploaded still.

Text-only films never enter this module. A run is reference-led only when
``creative_intent.mode`` is ``reference_led``, which is set from classified
roles on attached stills. No branch reads a scene, product, or example prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerGraphValidationError,
    node_config,
    node_pipeline,
    node_shot_index,
)

ROLE_CHARACTER = "character_identity"
ROLE_SCENE = "scene_source"
ROLE_PRODUCT = "product_hero"
ROLE_MOTION = "still_motion_source"
ROLE_STYLE = "style_source"

ROLES = frozenset(
    {ROLE_CHARACTER, ROLE_SCENE, ROLE_PRODUCT, ROLE_MOTION, ROLE_STYLE}
)

BINDING_VERBATIM = "verbatim"
BINDING_CONDITION = "condition"
BINDINGS = frozenset({BINDING_VERBATIM, BINDING_CONDITION})

MODE_TEXT = "text_film"
MODE_REFERENCE = "reference_led"

# How clips call the video model. Roles describe what a still *is*;
# video_binding alone decides I2V vs R2V (never role presence alone).
VIDEO_MULTI_REF = "multi_ref_story"
VIDEO_ANIMATE_KEYFRAME = "animate_keyframe"
VIDEO_BINDINGS = frozenset({VIDEO_MULTI_REF, VIDEO_ANIMATE_KEYFRAME})
WAN_REF_CAP = 5

# When an authority still owns the look but no vision labelled its medium, the
# lock points back at the attached pixels instead of a guessed art-style name.
MATCH_REFERENCE_MEDIUM = "match_reference_still"
MATCH_REFERENCE_LOOK = (
    "same rendering, line, shading, and palette as the attached reference image(s)"
)

_SUBJECT_ROLE = {
    "character": ROLE_CHARACTER,
    "scene": ROLE_SCENE,
    "object": ROLE_PRODUCT,
}
_ROLE_SUBJECT = {
    ROLE_CHARACTER: "character",
    ROLE_SCENE: "scene",
    ROLE_PRODUCT: "object",
}
# Use-as-is is the default for every reference role. An image-gen sheet /
# restyle / invented plate is created only when the LLM returns
# ``binding=condition`` (user asked to restyle / redraw / decompose).
_DEFAULT_BINDING = {
    ROLE_CHARACTER: BINDING_VERBATIM,
    ROLE_SCENE: BINDING_VERBATIM,
    ROLE_PRODUCT: BINDING_VERBATIM,
    ROLE_MOTION: BINDING_VERBATIM,
    ROLE_STYLE: BINDING_VERBATIM,
}


class ReferenceIntentError(ValueError):
    """Attached stills could not be given a role."""


def reference_led_active(analysis: dict[str, Any] | None) -> bool:
    intent = (analysis or {}).get("creative_intent") if isinstance(analysis, dict) else None
    if not isinstance(intent, dict):
        return False
    return str(intent.get("mode") or "").strip() == MODE_REFERENCE


def skips_final_frame_reverse(graph: dict[str, Any] | None) -> bool:
    """A motion-source still is the frame that moves, not an ending pose."""
    meta = (graph or {}).get("metadata") if isinstance(graph, dict) else None
    analysis = (meta or {}).get("script_analysis") if isinstance(meta, dict) else None
    if not isinstance(analysis, dict):
        analysis = {}
    intent = analysis.get("creative_intent") if isinstance(analysis.get("creative_intent"), dict) else {}
    if str(intent.get("mode") or "") != MODE_REFERENCE:
        return False
    return any(ROLE_MOTION in (slot.get("roles") or []) for slot in _slots(intent))


def absorb_reference_read(
    item: dict[str, Any],
    index: int,
    subject: str,
) -> dict[str, Any] | None:
    """Keep a classifier row. Legacy subjects gain a role; explicit roles win."""
    roles = _role_list(item.get("roles"))
    if not roles and subject:
        mapped = _SUBJECT_ROLE.get(subject)
        if mapped:
            roles = [mapped]
    if not subject:
        for role in roles:
            subject = _ROLE_SUBJECT.get(role, "")
            if subject:
                break
    if not roles and not subject:
        return None
    try:
        slot = int(item.get("slot") or index)
    except (TypeError, ValueError):
        slot = index
    if slot < 1:
        slot = index
    bindings = _bindings_for(item, roles)
    read = {
        "slot": slot,
        "subject": subject,
        "character_id": str(item.get("character_id") or "").strip(),
        "setting_id": str(item.get("setting_id") or "").strip(),
        "roles": roles,
        "bindings": bindings,
    }
    _carry_intent_fields(read, item)
    return read


def stamp_creative_intent(
    analysis: dict[str, Any],
    reads: list[dict[str, Any]] | None,
    image_refs: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Attach ``creative_intent`` when stills have roles. No stills: leave analysis alone."""
    images = [item for item in (image_refs or []) if isinstance(item, dict)]
    if not images:
        return analysis
    slots: list[dict[str, Any]] = []
    by_slot = {}
    for index, read in enumerate(reads or [], start=1):
        if not isinstance(read, dict):
            continue
        try:
            slot = int(read.get("slot") or index)
        except (TypeError, ValueError):
            slot = index
        by_slot[slot] = read
    for index, image in enumerate(images, start=1):
        read = by_slot.get(index) or {}
        roles = _role_list(read.get("roles"))
        if not roles:
            mapped = _SUBJECT_ROLE.get(str(read.get("subject") or "").strip().lower())
            if mapped:
                roles = [mapped]
        if not roles:
            raise ReferenceIntentError(
                "Attached stills need a reference role before a graph can be built."
            )
        path = str(image.get("path") or image.get("uri") or "").strip()
        slot_entry = {
            "slot": index,
            "path": path,
            "roles": roles,
            "bindings": _bindings_for(read, roles),
            "character_id": str(read.get("character_id") or "").strip(),
            "setting_id": str(read.get("setting_id") or "").strip(),
            "node_id": f"n_ref_{index:02d}",
        }
        _carry_intent_fields(slot_entry, read)
        slots.append(slot_entry)
    out = dict(analysis)
    _reconcile_slot_ids(slots, out)
    out["reference_reads"] = [dict(read) for read in (reads or []) if isinstance(read, dict)]
    intent: dict[str, Any] = {
        "mode": MODE_REFERENCE,
        "slots": slots,
        "video_binding": _video_binding_from_reads(reads),
    }
    out["creative_intent"] = intent
    return out


def video_generation_overrides(
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None,
    fallback_paths: list[str] | None,
) -> dict[str, Any]:
    """Select image-to-video or reference-to-video. Unset mode keeps today's call."""
    cfg = cfg if isinstance(cfg, dict) else {}
    mode = str(cfg.get("reference_call_mode") or "").strip().lower()
    fallback = [str(p) for p in (fallback_paths or []) if str(p).strip()]
    planned = _paths_from_plan(cfg, graph)
    if mode == "i2v":
        frame = str(cfg.get("reference_first_frame") or "").strip()
        if not frame:
            frame = _node_still_path(graph, str(cfg.get("reference_first_frame_node") or ""))
        # Pure keyframe animate: first_frame only. If companions also resolved
        # onto the plan, attach them as identity refs (never hard-null cast).
        companion_refs = [
            p for p in _merge_ref_paths(planned, fallback) if p and p != frame
        ]
        return {
            "first_frame": frame or None,
            "reference_images": companion_refs or None,
            "force_reference_mode": bool(companion_refs),
        }
    if mode == "r2v":
        refs = _merge_ref_paths(planned, fallback)
        return {
            "first_frame": None,
            "reference_images": refs or None,
            "force_reference_mode": True,
        }
    return {
        "first_frame": None,
        "reference_images": _merge_ref_paths([], fallback) or None,
        "force_reference_mode": True,
    }


def still_task_prompt(cfg: dict[str, Any]) -> str:
    """Image prompt for a reference still. The medium and the person come from config."""
    task = str(cfg.get("reference_still_task") or "").strip()
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    medium = str(style.get("medium") or style.get("look") or "").strip()
    if task == "medium_change":
        return (
            "The attached image is the subject. Keep the same subject and layout. "
            f"Render a new still in this medium: {medium or 'the approved style'}."
        )
    name = str(cfg.get("character_name") or "the person").strip()
    return (
        f"Image 1 is {name}. Same face, body, and wardrobe. "
        "Plain studio backdrop. Do not invent a different person. "
        f"Visual style: {medium or 'the approved style'}."
    )


def reference_prompt_issues(prompt: str, cfg: dict[str, Any] | None) -> list[str]:
    cfg = cfg if isinstance(cfg, dict) else {}
    text = str(prompt or "").strip()
    reasons: list[str] = []
    if not text:
        return ["empty"]
    action = str(cfg.get("shot_action") or "").strip()
    if action and action not in text:
        reasons.append("missing_action")
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or "").strip()
    if look and look not in text:
        reasons.append("missing_style")
    lighting = str(cfg.get("lighting") or "").strip()
    if lighting and lighting not in text:
        reasons.append("missing_lighting")
    crowd = str(cfg.get("crowd") or "").strip()
    if crowd and crowd not in text:
        reasons.append("missing_crowd")
    try:
        index = int(cfg.get("shot_index") or 1)
    except (TypeError, ValueError):
        index = 1
    if index > 1:
        end = str(cfg.get("previous_end_state") or "").strip()
        if "continues from" not in text.lower():
            reasons.append("missing_continuation")
        if end and end not in text:
            reasons.append("missing_previous_end")
        prev = str(cfg.get("previous_action") or "").strip()
        if prev and f"Action: {prev}" in text:
            reasons.append("repeats_previous_action")
    mode = str(cfg.get("reference_call_mode") or "")
    if mode == "i2v" and "first frame" not in text.lower() and "animate this image" not in text.lower():
        reasons.append("missing_motion_source")
    if mode == "r2v" and "image 1" not in text.lower() and "last reference" not in text.lower():
        reasons.append("missing_reference_order")
    return reasons


def compose_reference_clip_prompt(cfg: dict[str, Any] | None) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or "").strip()
    medium = str(style.get("medium") or "").strip()
    try:
        index = int(cfg.get("shot_index") or 1)
    except (TypeError, ValueError):
        index = 1
    action = str(cfg.get("shot_action") or "").strip()
    camera = str(cfg.get("camera") or "medium / eye-level").strip()
    timeline = str(cfg.get("timeline") or "").strip()
    duration = cfg.get("duration_sec")
    lighting = str(cfg.get("lighting") or "").strip()
    crowd = str(cfg.get("crowd") or "").strip()
    lines = [
        f"Shot {index}, timeline {timeline}, duration {duration} seconds.",
        f"Visual style: {look}.",
    ]
    if medium == MATCH_REFERENCE_MEDIUM:
        lines.append(
            "Medium: match the rendering, line, shading, and palette of the "
            "attached reference image(s); do not switch to another medium."
        )
    elif medium:
        lines.append(f"Medium: {medium}.")
    if lighting:
        lines.append(f"Lighting stays {lighting}.")
    if crowd:
        lines.append(f"Crowd stays {crowd}.")
    if index > 1:
        end = str(cfg.get("previous_end_state") or "").strip()
        prev = str(cfg.get("previous_action") or "").strip()
        lines.append(f"This shot continues from the previous end: {end}.")
        if prev:
            lines.append(f"The previous action is finished and must not repeat: {prev}.")
    lines.append(f"Action: {action}. Camera: {camera}.")
    mode = str(cfg.get("reference_call_mode") or "")
    contract = str(cfg.get("reference_prompt_contract") or "")
    if mode == "i2v":
        lines.append("Animate this image. The attached still is the first frame.")
        labels = _image_n_labels(cfg)
        if labels:
            lines.append(" ".join(labels))
    else:
        labels = _image_n_labels(cfg)
        if labels:
            lines.append(" ".join(labels))
        elif contract == "product":
            lines.append(
                "Image 1 is the referenced subject and stays unchanged. "
                "Motion happens around it."
            )
        elif contract == "scene":
            lines.append(
                "The place is the last reference image. Keep its layout and "
                "stage the action inside it."
            )
        elif contract == "character":
            lines.append(
                "Image 1 is the person. The place is the last reference image. "
                "The pose may change. Face and wardrobe stay."
            )
        else:
            lines.append(
                "Image 1 is the leading reference. Later references follow in slot order."
            )
    return " ".join(line for line in lines if line.strip())


def reference_led_graph(graph: DesignerExecutionGraph) -> bool:
    return graph.get("metadata", {}).get("scene_continuity_mode") == MODE_REFERENCE


def reject_removed_reference_stills(graph: DesignerExecutionGraph, removed: set[str]) -> None:
    """A clip's prompt contract names its stills by order; losing one breaks the call."""
    used: set[str] = set()
    for node in graph["nodes"]:
        cfg = node_config(node)
        used.update(str(entry.get("node_id") or "") for entry in cfg.get("reference_image_plan") or [])
        used.add(str(cfg.get("reference_first_frame_node") or ""))
    if missing := sorted(used & removed):
        raise DesignerGraphValidationError(
            f"Reference-led clips still use these stills: {', '.join(missing)}"
        )


def refresh_reference_continuity(
    before: DesignerExecutionGraph, graph: DesignerExecutionGraph
) -> None:
    """Re-derive each clip's handoff from the edited shot order, as the builder does."""
    old_configs = {node["id"]: node_config(node) for node in before["nodes"]}
    clips = sorted(
        (node for node in graph["nodes"] if node_pipeline(node) == "clip"),
        key=node_shot_index,
    )
    previous_action = ""
    previous_end = ""
    done: list[str] = []
    for node in clips:
        cfg = node_config(node)
        old = old_configs.get(node["id"], {})
        action = str(cfg.get("shot_action") or "")
        old_action = str(old.get("shot_action") or "")
        if action != old_action and cfg.get("end_state") == f"completed: {old_action}":
            cfg["end_state"] = f"completed: {action}"
        cfg["previous_action"] = previous_action
        cfg["previous_end_state"] = previous_end
        cfg["already_done"] = list(done)
        generate = cfg.get("generate")
        # Keep an authored prompt; replace only the one the builder composed.
        if isinstance(generate, dict) and generate.get("prompt") == compose_reference_clip_prompt(old):
            generate["prompt"] = compose_reference_clip_prompt(cfg)
        previous_action = action
        previous_end = str(cfg.get("end_state") or "")
        done.append(action)


def build_reference_led_video_graph(
    *,
    project_id: str,
    prompt: str,
    analysis: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    """Materialise a reference-led DAG. Caller runs normalize / prune / skills."""
    from jiuwenswarm.common.schema.designer_graph import (
        EDGE_KIND_DATA,
        GRAPH_SOURCE_PROMPT,
        NODE_ROLE_BRIEF,
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TEXT,
        NODE_TYPE_VIDEO,
        SCHEMA_VERSION,
        new_graph_id,
        utc_now_ms,
    )
    from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
        ensure_style_lock,
    )

    prompt_text = (prompt or "").strip()
    analysis = dict(analysis or {})
    intent = analysis.get("creative_intent") if isinstance(analysis.get("creative_intent"), dict) else {}
    slots = _slots(intent)
    if not slots:
        raise ReferenceIntentError("Reference-led graph requires at least one still role.")
    _reconcile_slot_ids(slots, analysis)
    if isinstance(intent, dict):
        intent["slots"] = slots
        analysis["creative_intent"] = intent
    style = ensure_style_lock(
        analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else None,
        prompt=prompt_text,
    )
    # Let an authority still own the film medium when the LLM flagged it.
    style = _style_lock_from_references(style, slots)
    analysis["style_lock"] = dict(style)
    shots = _prepare_shots(analysis, prompt_text)
    analysis["shots"] = shots
    analysis["user_prompt"] = prompt_text
    job = _job_plan(slots, analysis)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def add_edge(source: str, target: str) -> None:
        edges.append(
            {
                "id": f"e_{source}_{target}",
                "source": source,
                "target": target,
                "kind": EDGE_KIND_DATA,
            }
        )

    def add_node(node: dict[str, Any]) -> None:
        nodes.append(node)

    add_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "Brief",
            "config": {
                "role": NODE_ROLE_BRIEF,
                "prompt": prompt_text,
                "delegate": "agent",
                "kind": "agent",
                "skill_id": "brief",
                "director_task": (
                    "Author the brief from the user request and the reference roles. "
                    "Do not replace a reference-led subject with an invented cast."
                ),
            },
            "layout": {"x": 40, "y": 40, "width": 240, "height": 140},
        }
    )
    add_node(
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TEXT,
            "label": "Storyboard",
            "config": {
                "role": NODE_ROLE_STORYBOARD,
                "prompt": prompt_text,
                "planned_shots": shots,
                "inputs": ["n_brief"],
                "delegate": "agent",
                "kind": "agent",
                "skill_id": "storyboard",
                "director_task": (
                    "Each shot advances the previous end state. "
                    "Do not repeat a finished action."
                ),
            },
            "layout": {"x": 320, "y": 40, "width": 240, "height": 140},
        }
    )
    add_edge("n_brief", "n_storyboard")

    for slot in slots:
        node_id = str(slot["node_id"])
        path_value = str(slot.get("path") or "")
        card_role = _verbatim_card_role(slot)
        ref_config: dict[str, Any] = {
            "role": NODE_TYPE_IMAGE,
            "delegate": "handler",
            "force_handler": True,
            "skip_llm": True,
            "read_only": True,
            "immutable_source": True,
            "interaction_mode": "upload",
            "user_reference_id": f"ref_{int(slot['slot']):02d}",
            "user_reference_path": path_value,
            "reference_roles": list(slot.get("roles") or []),
            "reference_bindings": dict(slot.get("bindings") or {}),
            "inputs": [],
        }
        # Immediate card fill: a verbatim still is the card the user sees, so it
        # already reads as its cast/scene/product role — never Play-regenerated.
        if card_role:
            ref_config["reference_card_role"] = card_role
        if slot.get("style_authority"):
            ref_config["style_authority"] = True
        if slot.get("set_lock"):
            ref_config["set_lock"] = True
        ref_node: dict[str, Any] = {
            "id": node_id,
            "type": NODE_TYPE_IMAGE,
            "label": f"Reference {slot['slot']}",
            "config": ref_config,
            "layout": {"x": 40, "y": 220 + (int(slot["slot"]) - 1) * 160, "width": 220, "height": 140},
        }
        # Pre-seed output_ref from the upload so the card shows at bootstrap.
        if path_value:
            ref_node["output_ref"] = {
                "kind": NODE_TYPE_IMAGE,
                "uri": _as_file_uri(path_value),
                "label": f"Reference {slot['slot']}",
            }
        add_node(ref_node)
        add_edge(node_id, "n_brief")

    sheet_ids: list[str] = []
    sheet_index = 0
    # Condition-bound stills: identity sheet redrawn from the upload.
    for slot in job.get("character_sheet_slots") or []:
        sheet_index += 1
        cid = str(slot.get("character_id") or f"char_{sheet_index}")
        character = _character(analysis, cid)
        sheet_id = f"n_character_{sheet_index}"
        sheet_ids.append(sheet_id)
        add_node(
            {
                "id": sheet_id,
                "type": NODE_TYPE_IMAGE,
                "label": character.get("name") or f"Character {sheet_index}",
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "character_id": character.get("id") or cid,
                    "character_name": character.get("name") or f"Character {sheet_index}",
                    "reference_still_task": "identity_sheet",
                    "require_reference_images": True,
                    "style_lock": dict(style),
                    "inputs": ["n_storyboard", slot["node_id"]],
                    "delegate": "handler",
                    "prompt": still_task_prompt(
                        {
                            "reference_still_task": "identity_sheet",
                            "character_name": character.get("name") or f"Character {sheet_index}",
                            "style_lock": style,
                        }
                    ),
                },
                "layout": {"x": 320, "y": 220 + (sheet_index - 1) * 160, "width": 240, "height": 140},
            }
        )
        add_edge("n_storyboard", sheet_id)
        add_edge(str(slot["node_id"]), sheet_id)

    # Uncovered analysis cast: companion identity sheets under the same style_lock.
    # No upload input — never regenerate a verbatim still for these ids.
    # On-screen companions fill the Wan cap before off-screen extras.
    on_screen = _on_screen_character_keys(analysis)
    companions = list(job.get("companion_characters") or [])
    companions.sort(
        key=lambda ch: 0
        if (
            _norm_id(ch.get("id")) in on_screen or _norm_id(ch.get("name")) in on_screen
        )
        else 1
    )
    combined_companions = list(job.get("combined_companion_characters") or [])
    sheet_meta: dict[str, dict[str, Any]] = {}
    for character in companions:
        sheet_index += 1
        cid = str(character.get("id") or f"char_{sheet_index}")
        sheet_id = f"n_character_{sheet_index}"
        sheet_ids.append(sheet_id)
        name = str(character.get("name") or f"Character {sheet_index}")
        sheet_meta[sheet_id] = {"names": [name]}
        add_node(
            {
                "id": sheet_id,
                "type": NODE_TYPE_IMAGE,
                "label": name,
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "character_id": cid,
                    "character_name": name,
                    "reference_still_task": "identity_sheet",
                    "require_reference_images": False,
                    "companion_cast": True,
                    "style_lock": dict(style),
                    "inputs": ["n_storyboard"],
                    "delegate": "handler",
                    "prompt": _companion_sheet_prompt(character, style),
                },
                "layout": {"x": 320, "y": 220 + (sheet_index - 1) * 160, "width": 240, "height": 140},
            }
        )
        add_edge("n_storyboard", sheet_id)

    # Cap overflow: one combined secondary card for leftover non-lead cast.
    if combined_companions:
        sheet_index += 1
        sheet_id = f"n_character_{sheet_index}"
        sheet_ids.append(sheet_id)
        names = [
            str(ch.get("name") or ch.get("id") or f"Character").strip()
            for ch in combined_companions
            if isinstance(ch, dict)
        ]
        names = [n for n in names if n]
        ids = [
            str(ch.get("id") or "").strip()
            for ch in combined_companions
            if isinstance(ch, dict) and str(ch.get("id") or "").strip()
        ]
        sheet_meta[sheet_id] = {"names": names}
        add_node(
            {
                "id": sheet_id,
                "type": NODE_TYPE_IMAGE,
                "label": "Supporting cast",
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "character_id": ids[0] if len(ids) == 1 else "",
                    "character_ids": ids,
                    "character_name": ", ".join(names) if names else "Supporting cast",
                    "reference_still_task": "identity_sheet",
                    "require_reference_images": False,
                    "companion_cast": True,
                    "combined_cast": True,
                    "style_lock": dict(style),
                    "inputs": ["n_storyboard"],
                    "delegate": "handler",
                    "prompt": _combined_companion_sheet_prompt(combined_companions, style),
                },
                "layout": {"x": 320, "y": 220 + (sheet_index - 1) * 160, "width": 240, "height": 140},
            }
        )
        add_edge("n_storyboard", sheet_id)

    restyle_id = ""
    if job["restyle"] and job.get("motion_slots"):
        restyle_id = "n_restyle_01"
        motion = job["motion_slots"][0]
        inputs = ["n_storyboard", motion["node_id"]]
        for slot in job["style_slots"]:
            inputs.append(str(slot["node_id"]))
        add_node(
            {
                "id": restyle_id,
                "type": NODE_TYPE_IMAGE,
                "label": "Restyle still",
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "reference_still_task": "medium_change",
                    "require_reference_images": True,
                    "style_lock": dict(style),
                    "inputs": inputs,
                    "delegate": "handler",
                    "prompt": still_task_prompt(
                        {"reference_still_task": "medium_change", "style_lock": style}
                    ),
                },
                "layout": {"x": 320, "y": 220, "width": 240, "height": 140},
            }
        )
        for source in inputs:
            add_edge(source, restyle_id)

    plate_ids: list[str] = []
    if job["plates"]:
        scenes = [
            item
            for item in (job.get("companion_scenes") or [])
            if isinstance(item, dict)
        ]
        for index, scene in enumerate(scenes, start=1):
            plate_id = f"n_scene_{index}"
            plate_ids.append(plate_id)
            description = str(scene.get("description") or scene.get("name") or "Environment for this story.")
            add_node(
                {
                    "id": plate_id,
                    "type": NODE_TYPE_IMAGE,
                    "label": str(scene.get("name") or f"Setting {index}"),
                    "config": {
                        "role": NODE_ROLE_SCENE,
                        "setting_id": str(scene.get("id") or f"set_{index}"),
                        "prompt": description,
                        "style_lock": dict(style),
                        "inputs": ["n_storyboard"],
                        "delegate": "handler",
                    },
                    "layout": {"x": 600, "y": 220 + (index - 1) * 160, "width": 240, "height": 140},
                }
            )
            add_edge("n_storyboard", plate_id)

    plan = _reference_plan(
        job, sheet_ids=sheet_ids, plate_ids=plate_ids, sheet_meta=sheet_meta
    )
    clip_ids: list[str] = []
    previous_action = ""
    previous_end = ""
    for shot in shots:
        index = int(shot["shot_index"])
        clip_id = f"n_clip_{index}"
        clip_ids.append(clip_id)
        lighting = str(shot.get("lighting") or "").strip()
        crowd = str(shot.get("crowd") or "").strip()
        cfg: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": index,
            "shot_action": shot.get("action"),
            "camera": shot.get("camera"),
            "timeline": shot.get("timeline"),
            "duration_sec": shot.get("duration_sec"),
            "lighting": lighting,
            "crowd": crowd,
            "style_lock": dict(style),
            "previous_action": previous_action,
            "previous_end_state": previous_end,
            "already_done": [str(s.get("action") or "") for s in shots[: index - 1]],
            "end_state": shot.get("end_state"),
            "reference_call_mode": job["call_mode"],
            "reference_prompt_contract": job["contract"],
            "reference_video_binding": job.get("video_binding") or VIDEO_MULTI_REF,
            "reference_image_plan": plan,
            "inputs": ["n_storyboard"],
            "delegate": "agent",
            "kind": "agent",
            "skill_id": "clip",
            "max_video_calls": 1,
        }
        if job["call_mode"] == "i2v" and job.get("motion_slots"):
            if restyle_id:
                cfg["reference_first_frame_node"] = restyle_id
                cfg["inputs"].append(restyle_id)
            else:
                cfg["reference_first_frame"] = str(job["motion_slots"][0].get("path") or "")
                cfg["inputs"].append(str(job["motion_slots"][0]["node_id"]))
            # Companion sheets/plates must reach compose or prune drops them.
            for extra_id in list(sheet_ids) + list(plate_ids):
                if extra_id not in cfg["inputs"]:
                    cfg["inputs"].append(extra_id)
        else:
            for entry in plan:
                node_id = str(entry.get("node_id") or "")
                if node_id and node_id not in cfg["inputs"]:
                    cfg["inputs"].append(node_id)
            for extra_id in list(sheet_ids) + list(plate_ids):
                if extra_id not in cfg["inputs"]:
                    cfg["inputs"].append(extra_id)
            # Animate-upgraded R2V: keep keyframe node as an explicit input too.
            if restyle_id and restyle_id not in cfg["inputs"]:
                cfg["inputs"].append(restyle_id)
            elif job.get("motion_slots"):
                mid = str(job["motion_slots"][0].get("node_id") or "")
                if mid and mid not in cfg["inputs"]:
                    cfg["inputs"].append(mid)
        cfg["generate"] = {"prompt": compose_reference_clip_prompt(cfg)}
        cfg["director_task"] = (
            "Write the reference-led shot prompt from this clip config, then call_video_model."
        )
        add_node(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": f"Clip {index}",
                "config": cfg,
                "layout": {"x": 920, "y": 40 + (index - 1) * 160, "width": 240, "height": 140},
            }
        )
        for source in cfg["inputs"]:
            add_edge(str(source), clip_id)
        previous_action = str(shot.get("action") or "")
        previous_end = str(shot.get("end_state") or "")

    add_node(
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "Compose",
            "config": {
                "role": NODE_ROLE_COMPOSE,
                "inputs": clip_ids,
                "delegate": "agent",
                "kind": "agent",
                "skill_id": "compose",
                "director_task": "Concatenate the clips in shot order.",
            },
            "layout": {"x": 1200, "y": 40, "width": 240, "height": 140},
        }
    )
    for clip_id in clip_ids:
        add_edge(clip_id, "n_compose")

    now = utc_now_ms()
    total = sum(int(shot.get("duration_sec") or 0) for shot in shots)
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_id": new_graph_id(),
        "project_id": project_id,
        "title": (title or prompt_text[:80] or "Reference-led"),
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "bootstrap": "designer.graph.reference_led.v1",
            "scenario": "video",
            "script_analysis": analysis,
            "creative_intent": analysis.get("creative_intent"),
            "style_lock": dict(style),
            "film_duration_sec": total,
            "target_shot_count": len(shots),
            "freeze_shot_topology": True,
            "scene_continuity_mode": "reference_led",
        },
        "created_at": now,
        "updated_at": now,
    }


def _slots(intent: dict[str, Any]) -> list[dict[str, Any]]:
    raw = intent.get("slots") if isinstance(intent, dict) else None
    return [dict(item) for item in raw or [] if isinstance(item, dict)]


def _role_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        role = str(item or "").strip()
        if role in ROLES and role not in out:
            out.append(role)
    return out


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _style_read(item: dict[str, Any]) -> dict[str, str]:
    """Pull a vision medium/look/palette read off a classifier row, if present."""
    out: dict[str, str] = {}
    nested = item.get("style_read") if isinstance(item.get("style_read"), dict) else {}
    for source in (item, nested):
        for key in ("medium", "look", "palette"):
            value = str(source.get(key) or "").strip()
            if value and key not in out:
                out[key] = value[:280]
    return out


def _carry_intent_fields(target: dict[str, Any], item: dict[str, Any]) -> None:
    """Persist classifier style/lock flags. Topology never reads leftover suppress JSON."""
    if _as_bool(item.get("style_authority")):
        target["style_authority"] = True
    if _as_bool(item.get("set_lock")):
        target["set_lock"] = True
    if _as_bool(item.get("motion_source")):
        target["motion_source"] = True
    vb = str(item.get("video_binding") or "").strip().lower()
    if vb in VIDEO_BINDINGS:
        target["video_binding"] = vb
    style_read = _style_read(item)
    if style_read:
        target["style_read"] = style_read
    rationale = str(item.get("rationale") or "").strip()
    if rationale:
        target["rationale"] = rationale[:280]


def _video_binding_from_reads(reads: list[dict[str, Any]] | None) -> str:
    """Job-level video binding from classifier rows. Default is multi-ref story."""
    for read in reads or []:
        if not isinstance(read, dict):
            continue
        vb = str(read.get("video_binding") or "").strip().lower()
        if vb in VIDEO_BINDINGS:
            return vb
    return VIDEO_MULTI_REF


def _resolve_video_binding(
    analysis: dict[str, Any] | None, slots: list[dict[str, Any]]
) -> str:
    intent = (
        (analysis or {}).get("creative_intent")
        if isinstance((analysis or {}).get("creative_intent"), dict)
        else {}
    )
    vb = str((intent or {}).get("video_binding") or "").strip().lower()
    if vb in VIDEO_BINDINGS:
        return vb
    for slot in slots:
        slot_vb = str(slot.get("video_binding") or "").strip().lower()
        if slot_vb in VIDEO_BINDINGS:
            return slot_vb
    # Default: never force I2V from still_motion_source role alone.
    return VIDEO_MULTI_REF


def _bindings_for(item: dict[str, Any], roles: list[str]) -> dict[str, str]:
    raw = item.get("bindings") if isinstance(item.get("bindings"), dict) else {}
    single = str(item.get("binding") or "").strip().lower()
    out: dict[str, str] = {}
    for role in roles:
        chosen = str(raw.get(role) or "").strip().lower()
        if chosen not in BINDINGS:
            chosen = single if single in BINDINGS else _DEFAULT_BINDING[role]
        out[role] = chosen
    return out


def _norm_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return "_".join(text.replace("-", "_").split())


def _character(analysis: dict[str, Any], character_id: str) -> dict[str, Any]:
    needle = _norm_id(character_id)
    for item in analysis.get("characters") or []:
        if not isinstance(item, dict):
            continue
        if needle and (
            _norm_id(item.get("id")) == needle or _norm_id(item.get("name")) == needle
        ):
            return item
    characters = [item for item in (analysis.get("characters") or []) if isinstance(item, dict)]
    if len(characters) == 1:
        return characters[0]
    return {
        "id": character_id or "char_1",
        "name": "Reference subject",
        "description": "The person in the reference image.",
    }


def _analysis_characters(analysis: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for item in ((analysis or {}).get("characters") or []) if isinstance(item, dict)]


def _analysis_scenes(analysis: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for item in ((analysis or {}).get("scenes") or []) if isinstance(item, dict)]


def _collect_term_keys(item: dict[str, Any], *fields: str) -> set[str]:
    keys: set[str] = set()
    for field in fields:
        raw = item.get(field) or []
        if isinstance(raw, list):
            terms = raw
        elif raw:
            terms = [raw]
        else:
            terms = []
        for term in terms:
            n = _norm_id(term)
            if n:
                keys.add(n)
    return keys


def _identity_keys(item: dict[str, Any]) -> set[str]:
    """Canonical character/scene identity: id, name, aliases — not wardrobe match_terms."""
    keys: set[str] = set()
    for field in ("id", "name"):
        n = _norm_id(item.get(field))
        if n:
            keys.add(n)
    keys.update(_collect_term_keys(item, "aliases"))
    return keys


def _entity_keys(item: dict[str, Any]) -> set[str]:
    """Identity plus match_terms — used to *resolve* slot ids onto roster rows."""
    keys = _identity_keys(item)
    keys.update(_collect_term_keys(item, "match_terms"))
    return keys


def _resolve_roster_id(
    raw: Any, roster: list[dict[str, Any]], *, single_ok: bool
) -> str:
    """Map a slot id onto analysis id, name, match_terms, or a sole roster row."""
    needle = _norm_id(raw)
    if needle:
        for item in roster:
            if needle == _norm_id(item.get("id")):
                return str(item.get("id") or "").strip()
        for item in roster:
            if needle == _norm_id(item.get("name")):
                return str(item.get("id") or "").strip()
        for item in roster:
            if needle in _entity_keys(item):
                return str(item.get("id") or "").strip()
    if single_ok and len(roster) == 1:
        return str(roster[0].get("id") or "").strip()
    return str(raw or "").strip()


def _reconcile_slot_ids(
    slots: list[dict[str, Any]], analysis: dict[str, Any] | None
) -> None:
    """Write canonical analysis ids onto slots so coverage and sheets share one key."""
    characters = _analysis_characters(analysis)
    scenes = _analysis_scenes(analysis)
    for slot in slots:
        roles = slot.get("roles") or []
        if ROLE_CHARACTER in roles or ROLE_MOTION in roles:
            has_cid = bool(str(slot.get("character_id") or "").strip())
            if ROLE_CHARACTER in roles or has_cid:
                resolved = _resolve_roster_id(
                    slot.get("character_id"),
                    characters,
                    single_ok=ROLE_CHARACTER in roles,
                )
                if resolved:
                    slot["character_id"] = resolved
        if ROLE_SCENE in roles:
            resolved = _resolve_roster_id(
                slot.get("setting_id"), scenes, single_ok=True
            )
            if resolved:
                slot["setting_id"] = resolved


def _cover_character_keys(
    covered: set[str], slot_key: str, characters: list[dict[str, Any]]
) -> None:
    """Mark a character covered by identity only.

    Slot keys may still *find* a roster row via match_terms (after or without
    reconcile), but only that row's id/name/aliases enter ``covered``. Shared
    generic wardrobe tokens must not flood coverage onto other cast members.
    """
    key = _norm_id(slot_key)
    if not key:
        return
    covered.add(key)
    for item in characters:
        if key in _entity_keys(item):
            covered.update(_identity_keys(item))


def _covered_character_ids(
    slots: list[dict[str, Any]], analysis: dict[str, Any] | None
) -> set[str]:
    """Ids/names already supplied by a character (or tagged motion) still."""
    characters = _analysis_characters(analysis)
    covered: set[str] = set()
    for slot in slots:
        roles = slot.get("roles") or []
        cid = str(slot.get("character_id") or "").strip()
        if ROLE_CHARACTER in roles or (ROLE_MOTION in roles and cid):
            if cid:
                _cover_character_keys(covered, cid, characters)
            elif ROLE_CHARACTER in roles and len(characters) == 1:
                _cover_character_keys(covered, str(characters[0].get("id") or ""), characters)
                _cover_character_keys(covered, str(characters[0].get("name") or ""), characters)
    return covered


def _companion_characters(
    analysis: dict[str, Any] | None, covered: set[str]
) -> list[dict[str, Any]]:
    """Analysis cast members not already covered by a still — never invent beyond analysis."""
    out: list[dict[str, Any]] = []
    for item in _analysis_characters(analysis):
        keys = _identity_keys(item)
        if keys & covered:
            continue
        if not keys:
            continue
        out.append(item)
    return out


def _covered_setting_ids(
    slots: list[dict[str, Any]], analysis: dict[str, Any] | None
) -> set[str]:
    """Settings already provided by a scene still (verbatim, locked, or condition)."""
    scenes = _analysis_scenes(analysis)
    covered: set[str] = set()
    for slot in slots:
        if ROLE_SCENE not in (slot.get("roles") or []):
            continue
        sid = str(slot.get("setting_id") or "").strip()
        if sid:
            _cover_setting_keys(covered, sid, scenes)
        elif len(scenes) == 1:
            _cover_setting_keys(covered, str(scenes[0].get("id") or ""), scenes)
            _cover_setting_keys(covered, str(scenes[0].get("name") or ""), scenes)
    return covered


def _cover_setting_keys(
    covered: set[str], slot_key: str, scenes: list[dict[str, Any]]
) -> None:
    """Mark a setting covered by identity only (same rule as characters)."""
    key = _norm_id(slot_key)
    if not key:
        return
    covered.add(key)
    for item in scenes:
        if key in _entity_keys(item):
            covered.update(_identity_keys(item))


def _companion_scenes(
    analysis: dict[str, Any] | None, covered: set[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _analysis_scenes(analysis):
        keys = _identity_keys(item)
        if keys & covered:
            continue
        if not keys:
            continue
        out.append(item)
    return out


def _companion_sheet_prompt(character: dict[str, Any], style: dict[str, Any]) -> str:
    name = str(character.get("name") or "the person").strip()
    desc = str(character.get("description") or "").strip()
    medium = str(style.get("medium") or style.get("look") or "the approved style").strip()
    bits = [f"Create an identity sheet for {name}."]
    if desc:
        bits.append(desc if desc.endswith(".") else f"{desc}.")
    bits.append("Plain studio backdrop. Do not invent a different person.")
    bits.append(f"Visual style: {medium}.")
    return " ".join(bits)


def _combined_companion_sheet_prompt(
    characters: list[dict[str, Any]], style: dict[str, Any]
) -> str:
    names = [
        str(ch.get("name") or ch.get("id") or "person").strip()
        for ch in characters
        if isinstance(ch, dict)
    ]
    names = [n for n in names if n]
    label = ", ".join(names) if names else "the supporting cast"
    medium = str(style.get("medium") or style.get("look") or "the approved style").strip()
    return (
        f"Create one identity sheet showing {label} as distinct people side by side. "
        "Keep each face and wardrobe recognisable. Plain studio backdrop. "
        f"Visual style: {medium}."
    )


def _partition_companions_for_cap(
    companions: list[dict[str, Any]],
    *,
    reserved_slots: int,
    cap: int = WAN_REF_CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split companions into solo sheets vs one combined overflow card.

    Returns ``(solo_companions, combined_members)``. Combined members share one
    Wan slot when solos would exceed the remaining budget.
    """
    room = max(0, int(cap) - max(0, int(reserved_slots)))
    if len(companions) <= room:
        return list(companions), []
    if room <= 1:
        return [], list(companions)
    solos = list(companions[: room - 1])
    combined = list(companions[room - 1 :])
    return solos, combined


def _image_n_labels(cfg: dict[str, Any]) -> list[str]:
    """Label every plan entry as Image N with role / name hints."""
    plan = cfg.get("reference_image_plan")
    if not isinstance(plan, list) or not plan:
        return []
    labels: list[str] = []
    for index, entry in enumerate(plan, start=1):
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip()
        node_id = str(entry.get("node_id") or "").strip()
        names = entry.get("names") if isinstance(entry.get("names"), list) else []
        name_bits = [str(n).strip() for n in names if str(n).strip()]
        if name_bits:
            who = " and ".join(name_bits)
        elif role == ROLE_PRODUCT:
            who = "the product"
        elif role == ROLE_SCENE:
            who = "the place"
        elif role == ROLE_MOTION:
            who = "the keyframe subject"
        elif role == ROLE_CHARACTER:
            who = "the person"
        else:
            who = "the reference"
        extra = f" ({node_id})" if node_id else ""
        labels.append(f"Image {index} is {who}{extra}.")
    if labels and any(
        str(e.get("role") or "") == ROLE_SCENE
        for e in plan
        if isinstance(e, dict)
    ):
        labels.append("The place is the last reference image when a scene is attached.")
    return labels


def _slot_binding(slot: dict[str, Any], role: str) -> str:
    value = str((slot.get("bindings") or {}).get(role) or "").strip().lower()
    return value if value in BINDINGS else _DEFAULT_BINDING[role]


def _verbatim_card_role(slot: dict[str, Any]) -> str:
    """Design-card label for an upload the LLM bound verbatim (no regen node).

    The label lets the canvas show the upload as its cast / scene / product /
    motion card right away; an empty string means the still still feeds a
    generated sheet / plate / restyle node instead.
    """
    roles = slot.get("roles") or []
    if ROLE_MOTION in roles:
        return "motion" if _slot_binding(slot, ROLE_MOTION) == BINDING_VERBATIM else ""
    if ROLE_CHARACTER in roles and _slot_binding(slot, ROLE_CHARACTER) == BINDING_VERBATIM:
        return "character_design"
    if ROLE_SCENE in roles and (
        _slot_binding(slot, ROLE_SCENE) == BINDING_VERBATIM or bool(slot.get("set_lock"))
    ):
        return "scene"
    if ROLE_PRODUCT in roles:
        return "product"
    return ""


def _as_file_uri(path: str) -> str:
    text = str(path or "").strip()
    if not text or "://" in text or text.startswith("file:"):
        return text
    try:
        return Path(text).as_uri()
    except (ValueError, OSError):
        return text


def _style_lock_from_references(
    style: dict[str, str], slots: list[dict[str, Any]]
) -> dict[str, str]:
    """Inherit the film medium from an authority still when the LLM flagged it.

    The classifier sets ``style_authority`` / ``set_lock`` only when the still
    should own the look; if the user named a competing medium it leaves them
    unset and the text lock is kept as-is. A vision read (medium/look/palette)
    wins; without vision the lock points back at the attached pixels rather than
    guessing an art-style name from topic words such as "advertise".
    """
    authority = [
        slot for slot in slots if slot.get("style_authority") or slot.get("set_lock")
    ]
    if not authority:
        return style
    merged = dict(style)
    for slot in authority:
        read = slot.get("style_read") if isinstance(slot.get("style_read"), dict) else {}
        medium = str(read.get("medium") or "").strip()
        if not medium:
            continue
        merged["medium"] = medium[:280]
        look = str(read.get("look") or "").strip()
        if look:
            merged["look"] = look[:280]
        palette = str(read.get("palette") or "").strip()
        if palette:
            merged["palette"] = palette[:280]
        return merged
    merged["medium"] = MATCH_REFERENCE_MEDIUM
    merged["look"] = MATCH_REFERENCE_LOOK
    return merged


def _job_plan(
    slots: list[dict[str, Any]],
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def has(role: str) -> list[dict[str, Any]]:
        return [slot for slot in slots if role in (slot.get("roles") or [])]

    binding_of = _slot_binding
    _reconcile_slot_ids(slots, analysis)
    cast = _analysis_characters(analysis)
    settings = _analysis_scenes(analysis)

    motion = has(ROLE_MOTION)
    product = has(ROLE_PRODUCT)
    scene = has(ROLE_SCENE)
    character = has(ROLE_CHARACTER)
    style = has(ROLE_STYLE)
    video_binding = _resolve_video_binding(analysis, slots)

    # Covered = any still that already supplies that cast/set id (verbatim or
    # condition). Companions are always analysis entries minus that covered set.
    covered_chars = _covered_character_ids(slots, analysis)
    # True keyframe animate: a single analysis character with no character still
    # is the keyframe subject — do not mint a redundant identity sheet for them.
    if (
        video_binding == VIDEO_ANIMATE_KEYFRAME
        and motion
        and not character
        and len(cast) == 1
    ):
        _cover_character_keys(covered_chars, str(cast[0].get("id") or ""), cast)
        _cover_character_keys(covered_chars, str(cast[0].get("name") or ""), cast)
    companions = _companion_characters(analysis, covered_chars)
    on_screen = _on_screen_character_keys(analysis)
    companions.sort(
        key=lambda ch: 0
        if (
            _norm_id(ch.get("id")) in on_screen or _norm_id(ch.get("name")) in on_screen
        )
        else 1
    )

    # A character the LLM bound verbatim (the default) is shown from its own
    # file: no sheet for that id. Only an explicit `condition` character
    # regenerates a redrawn identity sheet for its still.
    sheet_characters = [
        slot for slot in character if binding_of(slot, ROLE_CHARACTER) == BINDING_CONDITION
    ]
    verbatim_characters = [
        slot for slot in character if binding_of(slot, ROLE_CHARACTER) == BINDING_VERBATIM
    ]
    # Locked / verbatim scene uploads are the set itself. Condition scene
    # stills cover their setting_id but still mint a generated plate.
    locked_scene = [
        slot
        for slot in scene
        if binding_of(slot, ROLE_SCENE) == BINDING_VERBATIM or bool(slot.get("set_lock"))
    ]
    restyle_scene = [
        slot
        for slot in scene
        if binding_of(slot, ROLE_SCENE) == BINDING_CONDITION and not bool(slot.get("set_lock"))
    ]
    covered_settings = _covered_setting_ids(slots, analysis)

    # Uncovered analysis.scenes become plates for multi-ref stories and for
    # keyframe jobs that also need extra set. Pure solo keyframe keeps no plates.
    plate_scenes: list[dict[str, Any]] = []
    needs_plates = video_binding == VIDEO_MULTI_REF or bool(companions)
    if needs_plates:
        plate_scenes = list(_companion_scenes(analysis, covered_settings))
        if restyle_scene and not locked_scene:
            for slot in restyle_scene:
                sid = _norm_id(slot.get("setting_id"))
                match = next(
                    (s for s in settings if sid in _entity_keys(s)),
                    None,
                )
                if match is None and not sid and settings:
                    match = settings[0]
                if match is not None and match not in plate_scenes:
                    plate_scenes.insert(0, match)

    # Cap overflow: keep solo companions that fit; fold the rest into one card.
    upload_reserve = len(product) + len(verbatim_characters) + len(locked_scene)
    if video_binding == VIDEO_ANIMATE_KEYFRAME and motion:
        upload_reserve += 1  # keyframe still occupies a plan / first-frame slot
    reserved = upload_reserve + len(sheet_characters) + (1 if plate_scenes else 0)
    solo_companions, combined_companions = _partition_companions_for_cap(
        companions, reserved_slots=reserved, cap=WAN_REF_CAP
    )

    # Persist resolved binding on intent for debugging / clip metadata.
    if isinstance(analysis, dict):
        intent = analysis.get("creative_intent")
        if isinstance(intent, dict):
            intent["video_binding"] = video_binding

    # True animate-keyframe alone → I2V. Extra cast/set upgrades to R2V so
    # companions/plates are packed as reference images (keyframe as Image 1).
    if video_binding == VIDEO_ANIMATE_KEYFRAME and motion:
        binding = binding_of(motion[0], ROLE_MOTION)
        has_extra = bool(solo_companions) or bool(combined_companions) or bool(
            sheet_characters
        ) or bool(plate_scenes)
        if has_extra:
            return {
                "call_mode": "r2v",
                "contract": "motion",
                "video_binding": video_binding,
                "sheets": bool(sheet_characters)
                or bool(solo_companions)
                or bool(combined_companions),
                "plates": bool(plate_scenes),
                "restyle": binding == BINDING_CONDITION,
                "motion_slots": motion,
                "product_slots": product,
                "scene_slots": locked_scene,
                "character_slots": character,
                "character_sheet_slots": sheet_characters,
                "character_verbatim_slots": verbatim_characters,
                "companion_characters": solo_companions,
                "combined_companion_characters": combined_companions,
                "companion_scenes": plate_scenes,
                "style_slots": style,
            }
        return {
            "call_mode": "i2v",
            "contract": "motion",
            "video_binding": video_binding,
            "sheets": False,
            "plates": False,
            "restyle": binding == BINDING_CONDITION,
            "motion_slots": motion,
            "product_slots": product,
            "scene_slots": locked_scene,
            "character_slots": character,
            "character_sheet_slots": [],
            "character_verbatim_slots": verbatim_characters,
            "companion_characters": [],
            "combined_companion_characters": [],
            "companion_scenes": [],
            "style_slots": style,
        }

    # Default multi-ref story: R2V. Motion role alone never forces I2V.
    # If a still already has character/scene/product identity, do not also park
    # it as a motion plan lead (avoids duplicate n_ref entries).
    story_motion = [
        slot
        for slot in motion
        if not (
            ROLE_CHARACTER in (slot.get("roles") or [])
            or ROLE_SCENE in (slot.get("roles") or [])
            or ROLE_PRODUCT in (slot.get("roles") or [])
        )
    ]
    contract = (
        "product"
        if product and not character
        else ("character" if character else ("motion" if story_motion else "scene"))
    )
    return {
        "call_mode": "r2v",
        "contract": contract,
        "video_binding": video_binding,
        "sheets": bool(sheet_characters)
        or bool(solo_companions)
        or bool(combined_companions),
        "plates": bool(plate_scenes),
        "restyle": False,
        "motion_slots": story_motion,
        "product_slots": product,
        "scene_slots": locked_scene,
        "character_slots": character,
        "character_sheet_slots": sheet_characters,
        "character_verbatim_slots": verbatim_characters,
        "companion_characters": solo_companions,
        "combined_companion_characters": combined_companions,
        "companion_scenes": plate_scenes,
        "style_slots": style,
    }


def _reference_plan(
    job: dict[str, Any],
    *,
    sheet_ids: list[str],
    plate_ids: list[str],
    sheet_meta: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    meta = sheet_meta or {}
    # Keyframe / motion stills lead when the job is animate-upgraded R2V.
    for slot in job.get("motion_slots") or []:
        plan.append(
            {
                "role": ROLE_MOTION,
                "path": str(slot.get("path") or ""),
                "node_id": str(slot["node_id"]),
            }
        )
    for slot in job.get("product_slots") or []:
        plan.append(
            {
                "role": ROLE_PRODUCT,
                "path": str(slot.get("path") or ""),
                "node_id": str(slot["node_id"]),
            }
        )
    # Verbatim characters ride on their own upload path (Image 1) — no sheet.
    for slot in job.get("character_verbatim_slots") or []:
        plan.append(
            {
                "role": ROLE_CHARACTER,
                "path": str(slot.get("path") or ""),
                "node_id": str(slot["node_id"]),
            }
        )
    for sheet_id in sheet_ids:
        entry: dict[str, Any] = {"role": ROLE_CHARACTER, "path": "", "node_id": sheet_id}
        info = meta.get(sheet_id) or {}
        names = info.get("names")
        if isinstance(names, list) and names:
            entry["names"] = [str(n) for n in names if str(n).strip()]
        plan.append(entry)
    # Locked / verbatim scene uploads stay on the plan even when companion
    # plates cover other settings.
    for slot in job.get("scene_slots") or []:
        plan.append(
            {
                "role": ROLE_SCENE,
                "path": str(slot.get("path") or ""),
                "node_id": str(slot["node_id"]),
            }
        )
    for plate_id in plate_ids:
        plan.append({"role": ROLE_SCENE, "path": "", "node_id": plate_id})
    return _cap_plan(plan)


def _on_screen_character_keys(analysis: dict[str, Any] | None) -> set[str]:
    keys: set[str] = set()
    for shot in ((analysis or {}).get("shots") or []):
        if not isinstance(shot, dict):
            continue
        for field in ("on_screen", "character_ids", "visible_cast_ids"):
            raw = shot.get(field) or []
            if not isinstance(raw, list):
                continue
            for cid in raw:
                n = _norm_id(cid)
                if n:
                    keys.add(n)
    return keys


def _cap_plan(plan: list[dict[str, str]], cap: int = WAN_REF_CAP) -> list[dict[str, str]]:
    """Prefer product, keyframe, and user uploads; keep combined cast when present."""
    if len(plan) <= cap:
        return plan

    def is_upload(item: dict[str, str]) -> bool:
        return bool(str(item.get("path") or "").strip())

    products = [i for i, item in enumerate(plan) if item.get("role") == ROLE_PRODUCT]
    motion = [i for i, item in enumerate(plan) if item.get("role") == ROLE_MOTION]
    uploads = [
        i
        for i, item in enumerate(plan)
        if i not in products
        and i not in motion
        and is_upload(item)
    ]
    generated = [
        i
        for i in range(len(plan))
        if i not in products and i not in motion and i not in set(uploads)
    ]
    gen_chars = [i for i in generated if plan[i].get("role") == ROLE_CHARACTER]
    gen_scenes = [i for i in generated if plan[i].get("role") == ROLE_SCENE]
    other = [i for i in generated if i not in gen_chars and i not in gen_scenes]

    head = products + motion + uploads
    if len(head) >= cap:
        return [plan[i] for i in head[:cap]]

    room = cap - len(head)
    fill = gen_chars + other
    if gen_scenes and room >= 1:
        last = gen_scenes[-1:]
        fill = fill + gen_scenes[:-1]
        return [plan[i] for i in head + fill[: room - 1] + last]
    return [plan[i] for i in head + (fill + gen_scenes)[:room]]


def _prepare_shots(analysis: dict[str, Any], prompt: str) -> list[dict[str, Any]]:
    raw = [dict(item) for item in (analysis.get("shots") or []) if isinstance(item, dict)]
    if not raw:
        raw = [{"action": prompt[:500], "setting_id": "set_1"}]
    shots: list[dict[str, Any]] = []
    cursor = 0.0
    for index, shot in enumerate(raw, start=1):
        action = str(shot.get("action") or f"beat {index}").strip()
        duration = shot.get("duration_sec")
        if duration in (None, ""):
            duration = _duration_from_timeline(str(shot.get("timeline") or "")) or 5
        try:
            duration_sec = max(1, int(duration))
        except (TypeError, ValueError):
            duration_sec = 5
        start = cursor
        cursor += duration_sec
        shots.append(
            {
                **shot,
                "shot_index": index,
                "action": action,
                "camera": str(shot.get("camera") or "medium / eye-level"),
                "duration_sec": duration_sec,
                "timeline": f"{start:.1f}-{cursor:.1f}s",
                "end_state": str(shot.get("end_state") or f"completed: {action}"),
                "lighting": str(shot.get("lighting") or ""),
                "crowd": str(shot.get("crowd") or ""),
                "setting_id": str(shot.get("setting_id") or "set_1"),
            }
        )
    return shots


def _duration_from_timeline(timeline: str) -> int | None:
    import re

    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", timeline or "")
    if not match:
        return None
    span = float(match.group(2)) - float(match.group(1))
    if span <= 0:
        return None
    return max(1, int(round(span)))


def _node_still_path(graph: dict[str, Any] | None, node_id: str) -> str:
    if not node_id or not isinstance(graph, dict):
        return ""
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("id") or "") != node_id:
            continue
        output = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
        uri = str(output.get("uri") or output.get("path") or "").strip()
        if uri:
            return uri
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        return str(cfg.get("user_reference_path") or "").strip()
    return ""


def _paths_from_plan(cfg: dict[str, Any], graph: dict[str, Any] | None) -> list[str]:
    plan = cfg.get("reference_image_plan")
    if not isinstance(plan, list):
        return []
    paths: list[str] = []
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            path = _node_still_path(graph, str(entry.get("node_id") or ""))
        if path:
            paths.append(path)
    return paths


def _merge_ref_paths(planned: list[str], fallback: list[str]) -> list[str]:
    """Keep plan order, then append any edge-collected paths the plan missed."""
    out: list[str] = []
    seen: set[str] = set()
    for path in list(planned or []) + list(fallback or []):
        text = str(path or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
