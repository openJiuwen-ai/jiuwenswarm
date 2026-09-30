# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reference-led graphs: one role list and one binding per uploaded still.

Text-only films never enter this module. A run is reference-led only when
``creative_intent.mode`` is ``reference_led``, which is set from classified
roles on attached stills. No branch reads a scene, product, or example prompt.
"""

from __future__ import annotations

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
_DEFAULT_BINDING = {
    ROLE_CHARACTER: BINDING_CONDITION,
    ROLE_SCENE: BINDING_VERBATIM,
    ROLE_PRODUCT: BINDING_VERBATIM,
    ROLE_MOTION: BINDING_VERBATIM,
    ROLE_STYLE: BINDING_CONDITION,
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
    return {
        "slot": slot,
        "subject": subject,
        "character_id": str(item.get("character_id") or "").strip(),
        "setting_id": str(item.get("setting_id") or "").strip(),
        "roles": roles,
        "bindings": bindings,
    }


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
        slots.append(
            {
                "slot": index,
                "path": path,
                "roles": roles,
                "bindings": _bindings_for(read, roles),
                "character_id": str(read.get("character_id") or "").strip(),
                "setting_id": str(read.get("setting_id") or "").strip(),
                "node_id": f"n_ref_{index:02d}",
            }
        )
    out = dict(analysis)
    out["reference_reads"] = [dict(read) for read in (reads or []) if isinstance(read, dict)]
    out["creative_intent"] = {"mode": MODE_REFERENCE, "slots": slots}
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
    if mode == "i2v":
        frame = str(cfg.get("reference_first_frame") or "").strip()
        if not frame:
            frame = _node_still_path(graph, str(cfg.get("reference_first_frame_node") or ""))
        return {
            "first_frame": frame or None,
            "reference_images": None,
            "force_reference_mode": False,
        }
    if mode == "r2v":
        planned = _paths_from_plan(cfg, graph)
        refs = planned or fallback
        return {
            "first_frame": None,
            "reference_images": refs or None,
            "force_reference_mode": True,
        }
    return {
        "first_frame": None,
        "reference_images": fallback or None,
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
    if medium:
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
    elif contract == "product":
        lines.append("Image 1 is the referenced subject and stays unchanged. Motion happens around it.")
    elif contract == "scene":
        lines.append("The place is the last reference image. Keep its layout and stage the action inside it.")
    elif contract == "character":
        lines.append(
            "Image 1 is the person. The place is the last reference image. "
            "The pose may change. Face and wardrobe stay."
        )
    else:
        lines.append("Image 1 is the leading reference. Later references follow in slot order.")
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
    optimize_for: str = "quality",
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
    style = ensure_style_lock(
        analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else None,
        prompt=prompt_text,
    )
    analysis["style_lock"] = dict(style)
    shots = _prepare_shots(analysis, prompt_text)
    analysis["shots"] = shots
    analysis["user_prompt"] = prompt_text
    job = _job_plan(slots)
    mode = "cost" if str(optimize_for).strip().lower() == "cost" else "quality"
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
                "optimize_for": mode,
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
                "optimize_for": mode,
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
        add_node(
            {
                "id": node_id,
                "type": NODE_TYPE_IMAGE,
                "label": f"Reference {slot['slot']}",
                "config": {
                    "role": NODE_TYPE_IMAGE,
                    "delegate": "handler",
                    "force_handler": True,
                    "skip_llm": True,
                    "read_only": True,
                    "immutable_source": True,
                    "interaction_mode": "upload",
                    "user_reference_id": f"ref_{int(slot['slot']):02d}",
                    "user_reference_path": slot.get("path") or "",
                    "reference_roles": list(slot.get("roles") or []),
                    "reference_bindings": dict(slot.get("bindings") or {}),
                    "inputs": [],
                },
                "layout": {"x": 40, "y": 220 + (int(slot["slot"]) - 1) * 160, "width": 220, "height": 140},
            }
        )
        add_edge(node_id, "n_brief")

    sheet_ids: list[str] = []
    if job["sheets"]:
        for index, slot in enumerate(job["character_slots"], start=1):
            cid = str(slot.get("character_id") or f"char_{index}")
            character = _character(analysis, cid)
            sheet_id = f"n_character_{index}"
            sheet_ids.append(sheet_id)
            add_node(
                {
                    "id": sheet_id,
                    "type": NODE_TYPE_IMAGE,
                    "label": character.get("name") or f"Character {index}",
                    "config": {
                        "role": NODE_ROLE_CHARACTER_DESIGN,
                        "character_id": character.get("id") or cid,
                        "character_name": character.get("name") or f"Character {index}",
                        "reference_still_task": "identity_sheet",
                        "require_reference_images": True,
                        "style_lock": dict(style),
                        "inputs": ["n_storyboard", slot["node_id"]],
                        "delegate": "handler",
                        "optimize_for": mode,
                        "prompt": still_task_prompt(
                            {
                                "reference_still_task": "identity_sheet",
                                "character_name": character.get("name") or f"Character {index}",
                                "style_lock": style,
                            }
                        ),
                    },
                    "layout": {"x": 320, "y": 220 + (index - 1) * 160, "width": 240, "height": 140},
                }
            )
            add_edge("n_storyboard", sheet_id)
            add_edge(str(slot["node_id"]), sheet_id)

    restyle_id = ""
    if job["restyle"]:
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
                    "optimize_for": mode,
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
        scenes = [s for s in (analysis.get("scenes") or []) if isinstance(s, dict)] or [
            {"id": "set_1", "name": "Setting", "description": "Environment for this story."}
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
                        "optimize_for": mode,
                    },
                    "layout": {"x": 600, "y": 220 + (index - 1) * 160, "width": 240, "height": 140},
                }
            )
            add_edge("n_storyboard", plate_id)

    plan = _reference_plan(job, sheet_ids=sheet_ids, plate_ids=plate_ids)
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
            "reference_image_plan": plan,
            "inputs": ["n_storyboard"],
            "delegate": "agent",
            "kind": "agent",
            "skill_id": "clip",
            "optimize_for": mode,
            "max_video_calls": 1,
        }
        if job["call_mode"] == "i2v":
            if restyle_id:
                cfg["reference_first_frame_node"] = restyle_id
                cfg["inputs"].append(restyle_id)
            else:
                cfg["reference_first_frame"] = str(job["motion_slots"][0].get("path") or "")
                cfg["inputs"].append(str(job["motion_slots"][0]["node_id"]))
        else:
            for entry in plan:
                node_id = str(entry.get("node_id") or "")
                if node_id and node_id not in cfg["inputs"]:
                    cfg["inputs"].append(node_id)
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
                "optimize_for": mode,
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
            "optimize_for": mode,
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


def _character(analysis: dict[str, Any], character_id: str) -> dict[str, Any]:
    for item in analysis.get("characters") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == character_id:
            return item
    characters = [item for item in (analysis.get("characters") or []) if isinstance(item, dict)]
    if len(characters) == 1:
        return characters[0]
    return {"id": character_id, "name": "Reference subject", "description": "The person in the reference image."}


def _job_plan(slots: list[dict[str, Any]]) -> dict[str, Any]:
    def has(role: str) -> list[dict[str, Any]]:
        return [slot for slot in slots if role in (slot.get("roles") or [])]

    motion = has(ROLE_MOTION)
    product = has(ROLE_PRODUCT)
    scene = has(ROLE_SCENE)
    character = has(ROLE_CHARACTER)
    style = has(ROLE_STYLE)
    # A still that is itself the frame wins over sheet/plate generation.
    if motion:
        binding = str((motion[0].get("bindings") or {}).get(ROLE_MOTION) or BINDING_VERBATIM)
        return {
            "call_mode": "i2v",
            "contract": "motion",
            "sheets": False,
            "plates": False,
            "restyle": binding == BINDING_CONDITION,
            "motion_slots": motion,
            "product_slots": [],
            "scene_slots": [],
            "character_slots": [],
            "style_slots": style,
        }
    verbatim_scene = [
        slot
        for slot in scene
        if str((slot.get("bindings") or {}).get(ROLE_SCENE) or "") == BINDING_VERBATIM
    ]
    return {
        "call_mode": "r2v",
        "contract": "product" if product and not character else ("character" if character else "scene"),
        "sheets": bool(character),
        "plates": bool(character) and not verbatim_scene,
        "restyle": False,
        "motion_slots": [],
        "product_slots": product,
        "scene_slots": verbatim_scene or scene,
        "character_slots": character,
        "style_slots": style,
    }


def _reference_plan(
    job: dict[str, Any],
    *,
    sheet_ids: list[str],
    plate_ids: list[str],
) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []
    for slot in job.get("product_slots") or []:
        plan.append({"role": ROLE_PRODUCT, "path": str(slot.get("path") or ""), "node_id": str(slot["node_id"])})
    for sheet_id in sheet_ids:
        plan.append({"role": ROLE_CHARACTER, "path": "", "node_id": sheet_id})
    if job.get("plates"):
        for plate_id in plate_ids:
            plan.append({"role": ROLE_SCENE, "path": "", "node_id": plate_id})
    else:
        for slot in job.get("scene_slots") or []:
            plan.append({"role": ROLE_SCENE, "path": str(slot.get("path") or ""), "node_id": str(slot["node_id"])})
    return _cap_plan(plan)


def _cap_plan(plan: list[dict[str, str]], cap: int = 5) -> list[dict[str, str]]:
    if len(plan) <= cap:
        return plan
    locked_first = [item for item in plan if item.get("role") == ROLE_PRODUCT][:1]
    locked_last = [item for item in plan if item.get("role") == ROLE_SCENE][-1:]
    middle = [item for item in plan if item not in locked_first and item not in locked_last]
    room = cap - len(locked_first) - len(locked_last)
    if room < 0:
        return (locked_first + locked_last)[:cap]
    return locked_first + middle[:room] + locked_last


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
