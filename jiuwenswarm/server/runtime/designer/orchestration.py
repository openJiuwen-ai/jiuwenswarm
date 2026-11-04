# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Director for Designer runs."""

from __future__ import annotations

import json
import logging
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerGraphNode,
    is_comfyui_node,
    node_pipeline,
)
from jiuwenswarm.server.runtime.designer.continuity import (
    infer_continuity_lock as _infer_continuity_lock,
    merge_lock_with_previous,
)
from jiuwenswarm.server.runtime.designer.feedback import suggestion_for_node
from jiuwenswarm.server.runtime.designer.model_tools import (
    call_model_tool,
    list_configured_models,
)
from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import CLOTHING_PARTS_RULE
from jiuwenswarm.server.runtime.designer.pipeline.content_language import (
    POSITIVE_VISUAL_RULE,
    authoring_language_rule,
    current_content_language,
    node_task,
)
from jiuwenswarm.server.runtime.designer.user_references import carry_user_references

logger = logging.getLogger(__name__)


def _llm_character_card(
    raw: dict[str, Any],
    index: int,
    prior: list[dict[str, Any]],
) -> dict[str, Any]:
    """Identity card from Director JSON; keeps garment slots the LLM authored earlier."""
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        normalize_clothing_parts,
    )

    cid = str(raw.get("id") or f"char_{index}").strip() or f"char_{index}"
    name = str(raw.get("name") or cid).strip() or cid
    card: dict[str, Any] = {
        "id": cid,
        "name": name,
        "description": str(raw.get("description") or name)[:600],
    }
    parts = normalize_clothing_parts(raw.get("clothing_parts"))
    if not parts:
        earlier = next(
            (c for c in prior if isinstance(c, dict) and str(c.get("id") or "") == cid),
            {},
        )
        parts = normalize_clothing_parts(earlier.get("clothing_parts"))
    if parts:
        card["clothing_parts"] = parts
    return card


def _language_rule() -> str:
    """Keep every user-editable value the Director writes in the content language."""
    return authoring_language_rule(current_content_language())


# Role → tool set for one-pass node agents (no feedback loop).
_ROLE_TOOLS: dict[str, list[str]] = {
    "brief": ["call_model", "read_upstream"],
    "character": ["call_model", "read_upstream", "call_image_model"],
    "scene": ["call_model", "read_upstream", "call_image_model"],
    "storyboard": ["call_model", "read_upstream"],
    "clip": ["call_model", "read_upstream", "call_video_model"],
    "speech": ["call_model", "read_upstream", "call_speech_model"],
    "music": ["call_model", "read_upstream", "call_music_model"],
    "audio": ["call_model", "read_upstream", "call_speech_model", "call_music_model"],
    "compose": ["call_model", "read_upstream", "ffmpeg_compose", "mix_audio"],
}


def _role_key(node: DesignerGraphNode) -> str:
    pipeline = str(node_pipeline(node) or "").strip().lower()
    if pipeline:
        return pipeline
    cfg = node.get("config") or {}
    role = str(cfg.get("role") or node.get("type") or "").strip().lower()
    return role


def _tools_for_node(node: DesignerGraphNode) -> list[str]:
    cfg = node.get("config") or {}
    existing = cfg.get("tools")
    if isinstance(existing, list) and existing:
        return [str(t) for t in existing]
    role = _role_key(node)
    if role in _ROLE_TOOLS:
        return list(_ROLE_TOOLS[role])
    ntype = str(node.get("type") or "").lower()
    if ntype == "image":
        return ["call_model", "read_upstream", "call_image_model"]
    if ntype == "video":
        return ["call_model", "read_upstream", "call_video_model"]
    if "speech" in role or "tts" in role:
        return list(_ROLE_TOOLS["speech"])
    if "music" in role or "bgm" in role:
        return list(_ROLE_TOOLS["music"])
    if "compose" in role or "mix" in role or "film" in role:
        return list(_ROLE_TOOLS["compose"])
    return ["call_model", "read_upstream"]


def _edges_avoiding(edges: list[Any], drop: set[str]) -> list[Any]:
    kept = []
    for e in edges:
        if str(e.get("source") or "") in drop or str(e.get("target") or "") in drop:
            continue
        kept.append(e)
    return kept


def _str_ids(raw: Any) -> list[str]:
    return [str(x) for x in raw if str(x)]


def _is_prop_cast(character: dict[str, Any]) -> bool:
    return bool(character.get("is_prop")) or str(character.get("cast_kind") or "") in {"brand_mascot", "prop"}


def _is_prune_protected(node: dict[str, Any]) -> bool:
    nid = str(node.get("id") or "")
    if nid.startswith(("n_clip", "n_frame")) or nid in {"n_scene", "n_compose", "n_speech", "n_music"}:
        return True
    return _role_key(node) in {"clip", "compose", "speech", "music"}


def _shot_time_of_day(clips: Any, clip_index: int) -> dict[str, Any] | None:
    for shot in clips or []:
        if not isinstance(shot, dict) or int(shot.get("clip_index") or 0) != clip_index:
            continue
        if isinstance(shot.get("time_of_day_lock"), dict):
            return shot["time_of_day_lock"]
    return None


def _spatial_continuity_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Director gate: stamp consistency locks so clip/shot prompts share blocking."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    shot_locks: dict[int, dict[str, str]] = {}

    for shot in analysis.get("clips") or []:
        if not isinstance(shot, dict):
            continue
        try:
            idx = int(shot.get("clip_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 1:
            continue
        action = str(shot.get("action") or shot.get("clip_prompt") or "")
        carried = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
        lock = merge_lock_with_previous(
            _infer_continuity_lock(action), shot_locks.get(idx - 1)
        )
        for key in ("brief", "story"):
            value = str(carried.get(key) or "").strip()
            if value:
                lock[key] = value
        shot["continuity_lock"] = lock
        shot_locks[idx] = lock

    if analysis.get("clips"):
        meta["script_analysis"] = analysis
        graph["metadata"] = meta

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"clip", "storyboard"}:
            continue
        idx = int(cfg.get("clip_index") or 0) or 0
        action = str(cfg.get("clip_action") or "")
        lock = dict(cfg.get("continuity_lock") or {}) if isinstance(cfg.get("continuity_lock"), dict) else {}
        if idx and idx in shot_locks:
            lock = shot_locks[idx]
        elif action and not lock:
            lock = _infer_continuity_lock(action)
        if not lock:
            continue
        cfg["continuity_lock"] = lock
        if role == "storyboard":
            notes.append(f"{node.get('id')}: consistency locks available for planned shots")
        node["config"] = cfg

    # Keep the storyboard agent's structured shot plan current.
    if shot_locks:
        shots = list(analysis.get("clips") or [])
        if shots:
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if _role_key(node) != "storyboard":
                    continue
                cfg["planned_shots"] = shots
                node["config"] = cfg

    meta = dict(graph.get("metadata") or {})
    meta["continuity_locks"] = {str(k): v for k, v in shot_locks.items()}
    graph["metadata"] = meta
    return notes


def _spatial_geography_lock_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Stamp / refresh spatial_lock on scene/clip so architecture stays faithful."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    lock = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    if not lock and isinstance(analysis.get("spatial_lock"), dict):
        lock = dict(analysis.get("spatial_lock") or {})
    if not lock:
        scenes = list(analysis.get("scenes") or [])
        scene0 = scenes[0] if scenes and isinstance(scenes[0], dict) else {}
        lock = {
            "setting": str(scene0.get("name") or "Primary setting"),
            "architecture": str(scene0.get("description") or "one coherent interior"),
            "static_rule": (
                "STATIC OBJECTS LOCKED across shots: landmarks, terrain, buildings, props, and "
                "light direction must match the master scene specs — only camera may change."
            ),
            "crowd_rule": (
                "Scene specs define environment; clips keep the SAME extras layout "
                "across shots; never clone a featured person into two places at once."
            ),
        }
    # Ensure required keys
    lock.setdefault(
        "static_rule",
        "Keep landmarks, layout, and lighting identical to the master plate.",
    )
    lock.setdefault(
        "crowd_rule",
        "Do not invent a new extras layout per shot.",
    )
    meta["spatial_lock"] = lock
    if isinstance(analysis, dict):
        analysis = dict(analysis)
        analysis["spatial_lock"] = lock
        meta["script_analysis"] = analysis
    graph["metadata"] = meta

    lock_clause = (
        " SPATIAL LOCK: "
        + "; ".join(f"{k}={v}" for k, v in lock.items() if str(v).strip())
    )[:500]

    ids = {
        str(item.get("id") or "")
        for item in (graph.get("nodes") or [])
        if isinstance(item, dict)
    }
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        nid = str(node.get("id") or "")
        if role not in {"scene", "clip", "brief", "storyboard"}:
            continue
        cfg["spatial_lock"] = lock
        is_sub_scene = role == "scene" and nid != "n_scene"
        if is_sub_scene and "n_scene" in ids and not cfg.get("master_scene_node_id"):
            cfg["master_scene_node_id"] = "n_scene"
            cfg.setdefault("scene_strategy", "edit_master_view")
            notes.append(f"{nid}: master_scene_node_id=n_scene")
        # Keep spatial_lock as structured cfg only. Do not append SPATIAL LOCK
        # essays onto prompts (truncates mid-sentence and poisons the toolbar).
        if role in {"clip", "scene"} and lock:
            notes.append(f"{nid}: spatial_lock cfg only")
        node["config"] = cfg
    return notes


def _director_prune_and_cohere(graph: DesignerExecutionGraph) -> list[str]:
    """Prune unused nodes, rewire spatial edges, keep every kept node useful for final film."""
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        ensure_combined_cast_reach_compose,
        prune_non_contributing_nodes,
    )

    notes: list[str] = []
    # Drop unused combined cast sheets that never feed a clip.
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    outs: dict[str, set[str]] = {}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s and t:
            outs.setdefault(s, set()).add(t)
    drop: list[str] = []
    for n in nodes:
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        nid = str(n.get("id") or "")
        if not nid:
            continue
        if cfg.get("combined_cast") and not any(
            str(t).startswith("n_clip") or t == "n_compose"
            for t in (outs.get(nid) or [])
        ):
            drop.append(nid)
    if drop:
        drop_set = set(drop)
        graph["nodes"] = [n for n in nodes if str(n.get("id")) not in drop_set]
        graph["edges"] = _edges_avoiding(edges, drop_set)
        notes.extend([f"drop_unused_combined:{x}" for x in drop])

    # Coherence: master plate → shot views; frames/shots include master + shot scene.
    # Skipped when compose-first / no empty plates.
    ids = {
        str(n.get("id"))
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in (graph.get("edges") or [])
        if isinstance(e, dict)
    }
    master_id = "n_scene" if "n_scene" in ids else ""
    if master_id:
        for n in list(graph.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            cfg = dict(n.get("config") or {})
            nid = str(n.get("id") or "")
            role = _role_key(n)
            if role == "scene" and nid != master_id:
                cfg.setdefault("master_scene_node_id", master_id)
                cfg.setdefault("scene_strategy", "edit_master_view")
                inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
                if master_id not in inputs:
                    inputs.append(master_id)
                    cfg["inputs"] = inputs
                    notes.append(f"cohere_inputs:{nid}+{master_id}")
                if (master_id, nid) not in edge_pairs:
                    graph.setdefault("edges", []).append(
                        {
                            "id": f"e_mgr_{master_id}_{nid}",
                            "source": master_id,
                            "target": nid,
                            "kind": "data",
                            "label": "spatial_ref",
                        }
                    )
                    edge_pairs.add((master_id, nid))
                    notes.append(f"cohere_edge:{master_id}->{nid}")
                n["config"] = cfg
            elif role in {"clip"}:
                shot_idx = int(cfg.get("clip_index") or 0)
                shot_scene = f"n_scene_{shot_idx}" if shot_idx >= 1 else ""
                inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
                changed = False
                for need in (master_id, shot_scene):
                    if need and need in ids and need not in inputs:
                        inputs.append(need)
                        changed = True
                    if need and need in ids and (need, nid) not in edge_pairs:
                        graph.setdefault("edges", []).append(
                            {
                                "id": f"e_mgr_{need}_{nid}",
                                "source": need,
                                "target": nid,
                                "kind": "data",
                                "label": "spatial_ref",
                            }
                        )
                        edge_pairs.add((need, nid))
                        notes.append(f"cohere_edge:{need}->{nid}")
                if changed:
                    cfg["inputs"] = inputs
                    notes.append(f"cohere_inputs:{nid}")
                n["config"] = cfg

    pruned = prune_non_contributing_nodes(graph)
    notes.extend([f"pruned:{x}" for x in pruned])
    notes.extend(ensure_combined_cast_reach_compose(graph))
    # Final structural prune after rewires (orphans must not remain).
    pruned2 = prune_non_contributing_nodes(graph)
    notes.extend([f"pruned:{x}" for x in pruned2])
    notes.extend(_director_reedit_artifacts_after_prune(graph, pruned=list(pruned) + list(pruned2)))
    ids = {str(n.get("id")) for n in (graph.get("nodes") or []) if isinstance(n, dict)}
    if any(
        str((n.get("config") or {}).get("scene_strategy") or "") == "edit_master_view"
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict)
    ) and "n_scene" not in ids:
        notes.append("warn:edit_master_view_without_n_scene")
    return notes


def _director_reedit_artifacts_after_prune(
    graph: DesignerExecutionGraph,
    *,
    pruned: list[str] | None = None,
) -> list[str]:
    """After graph prune, re-edit Brief / Storyboard / locks to match surviving nodes."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = dict(meta.get("script_analysis") or {}) if isinstance(meta.get("script_analysis"), dict) else {}
    kept_shot_idxs: list[int] = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        role = _role_key(n)
        nid = str(n.get("id") or "")
        if role in {"clip"} or nid.startswith("n_clip_"):
            idx = int(cfg.get("clip_index") or 0)
            if idx >= 1 and idx not in kept_shot_idxs:
                kept_shot_idxs.append(idx)
    kept_shot_idxs.sort()
    shots = [s for s in (analysis.get("clips") or []) if isinstance(s, dict)]
    if kept_shot_idxs and shots:
        kept = {
            int(s.get("clip_index") or 0)
            for s in shots
            if int(s.get("clip_index") or 0) in set(kept_shot_idxs)
        }
        if kept and kept != {int(s.get("clip_index") or 0) for s in shots}:
            analysis["clips"] = [
                s for s in shots if int(s.get("clip_index") or 0) in kept
            ]
            notes.append(f"reedit_shots_keep:{sorted(kept)}")
        # Rebuild already_done chains per setting (never leak office beats into a new room).
        already_by_setting: dict[str, list[str]] = {}
        exited_by_setting: dict[str, set[str]] = {}
        revised: list[dict[str, Any]] = []
        for s in analysis.get("clips") or []:
            if not isinstance(s, dict):
                continue
            shot = dict(s)
            sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
            already = list(already_by_setting.get(sid) or [])
            shot["already_done"] = list(already)
            action = str(shot.get("action") or shot.get("clip_prompt") or "").strip()
            exits = [
                str(x)
                for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
                if str(x)
            ]
            idx = int(shot.get("clip_index") or 0)
            if action:
                already.append(f"shot{idx}: {action[:120]}")
            carried = set(exited_by_setting.get(sid) or ())
            for cid in exits:
                already.append(
                    f"{cid} exited by shot{idx} — omit from later same-setting prompts until returned"
                )
                carried.add(cid)
            # Storyboard return clears exit for anyone listed on_screen again.
            visible = _str_ids(
                shot.get("on_screen") or shot.get("visible_cast_ids") or shot.get("character_ids") or []
            )
            returned = {c for c in visible if c in carried}
            carried -= returned
            exited_by_setting[sid] = set(carried)
            shot["exited_ids"] = sorted(carried)
            offscreen = [
                str(x)
                for x in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
                if str(x) and str(x) not in visible
            ]
            # Exited stay out of on_screen unless just returned above.
            visible = [c for c in visible if c not in carried]
            shot["on_screen"] = list(visible)
            shot["character_ids"] = list(visible)
            ensemble = _str_ids(shot.get("ensemble_cast_ids") or shot.get("compose_cast_ids") or visible or [])
            occ = dict(shot.get("occupancy") or {}) if isinstance(shot.get("occupancy"), dict) else {}
            occ["must_appear"] = list(visible)
            occ["offscreen"] = list(offscreen)
            occ["exited"] = sorted(carried)
            occ["featured"] = [
                str(x)
                for x in (shot.get("featured_cast_ids") or visible or ensemble)
                if str(x) and str(x) not in carried
            ]
            if isinstance(shot.get("cast_actions"), dict):
                occ["cast_actions"] = {
                    k: v
                    for k, v in shot["cast_actions"].items()
                    if str(k) not in carried
                }
            occ.setdefault(
                "rule",
                "Draw must_appear only; omit exited until storyboard returns them; keep offscreen out of frame.",
            )
            shot["occupancy"] = occ
            already_by_setting[sid] = already
            revised.append(shot)
        analysis["clips"] = revised
        meta["script_analysis"] = analysis
        notes.append("reedit_already_done_occupancy")

    chars = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    surviving = [s for s in (analysis.get("clips") or []) if isinstance(s, dict)]
    if surviving:
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
            render_storyboard_table,
        )

        meta["approved_storyboard"] = render_storyboard_table(surviving, chars, current_content_language())
        graph["metadata"] = meta
        notes.append("reedit_approved_storyboard")

    # Brief: keep detail, stamp counts for surviving topology.
    brief = str(meta.get("approved_brief") or "").strip()
    cast_n = len(chars)
    scene_n = len([s for s in (analysis.get("scenes") or []) if isinstance(s, dict)])
    shot_n = len(surviving)
    stamp = (
        f"\n\n## Director prune sync\n"
        f"- Cast count: {cast_n}\n"
        f"- Scene count: {scene_n}\n"
        f"- Surviving shots: {shot_n} (indices {kept_shot_idxs})\n"
        f"- Pruned nodes: {', '.join(pruned or []) or 'none'}\n"
        f"- Audio routing: {meta.get('audio_routing') or {}}\n"
    )
    if brief:
        # Replace prior sync block if present.
        if "## Director prune sync" in brief:
            brief = brief.split("## Director prune sync")[0].rstrip()
        meta["approved_brief"] = (brief + stamp).strip() + "\n"
        graph["metadata"] = meta
        notes.append("reedit_approved_brief")

    # Stamp occupancy / already_done onto surviving clip nodes.
    by_idx = {
        int(s.get("clip_index") or 0): s
        for s in (analysis.get("clips") or [])
        if isinstance(s, dict) and int(s.get("clip_index") or 0) >= 1
    }
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = dict(n.get("config") or {})
        role = _role_key(n)
        if role not in {"clip"}:
            continue
        idx = int(cfg.get("clip_index") or 0)
        shot = by_idx.get(idx)
        if not shot:
            continue
        if isinstance(shot.get("occupancy"), dict):
            cfg["occupancy"] = shot["occupancy"]
        if shot.get("already_done") is not None:
            cfg["already_done"] = list(shot.get("already_done") or [])
        n["config"] = cfg

    graph["metadata"] = meta
    if pruned:
        notes.append(f"prune_count:{len(pruned)}")
    return notes


def _ensure_audio_nodes_for_intent(graph: DesignerExecutionGraph) -> list[str]:
    """Audio intent currently stays on clip prompts; it never creates canvas nodes."""
    del graph
    return []


def assign_audio_node_agents(graph: DesignerExecutionGraph) -> dict[str, Any]:
    """Record routing for existing audio nodes without creating new ones."""
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends
    from jiuwenswarm.server.runtime.designer.smart_graph import prune_non_contributing_nodes
    from jiuwenswarm.server.runtime.designer.user_references import (
        is_uploaded_media_node,
    )

    backends = detect_audio_backends()
    can_speech = bool(backends.get("can_speech"))
    can_music = bool(backends.get("can_music"))
    meta = dict(graph.get("metadata") or {})
    routing = dict(meta.get("audio_routing") or {})
    clip_embedded = bool(routing.get("clip_embedded")) or (not can_speech and not can_music)
    if clip_embedded:
        # Only dialogue folds into the shots; the BGM node stays either way.
        drop: set[str] = set()
        if not can_speech:
            for n in graph.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                if str(n.get("id") or "") == "n_speech" or _role_key(n).lower() in {"speech", "tts"}:
                    drop.add(str(n.get("id") or ""))
        if drop:
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if str(n.get("id") or "") not in drop
            ]
            graph["edges"] = _edges_avoiding(graph.get("edges") or [], drop)
            for n in graph.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                cfg = dict(n.get("config") or {})
                if str(cfg.get("role") or "") != "clip":
                    continue
                from jiuwenswarm.server.runtime.designer.audio_locks import (
                    stamp_audio_fields_on_clip_config,
                )

                analysis = (
                    meta.get("script_analysis")
                    if isinstance(meta.get("script_analysis"), dict)
                    else {}
                )
                idx = int(cfg.get("clip_index") or 0)
                shot_row = next(
                    (
                        s
                        for s in (analysis.get("clips") or [])
                        if isinstance(s, dict) and int(s.get("clip_index") or 0) == idx
                    ),
                    {},
                )
                cfg = stamp_audio_fields_on_clip_config(
                    cfg,
                    shot=shot_row if isinstance(shot_row, dict) else {},
                    analysis=analysis,
                    meta=meta,
                    clip_embedded=True,
                )
                n["config"] = cfg
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                    chain_prior_speech_across_clips,
                )

                chain_prior_speech_across_clips(graph)
            except Exception:  # noqa: BLE001
                logger.debug("chain_prior_speech_across_clips failed", exc_info=True)
            prune_non_contributing_nodes(graph)
        embedded_assigned = ["clip_embedded"]
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            if is_uploaded_media_node(node):
                continue
            nid = str(node.get("id") or "")
            if nid != "n_music" and _role_key(node).lower() not in {"music", "audio_bed"}:
                continue
            cfg = dict(node.get("config") or {})
            cfg["role"] = "music"
            cfg["skill_id"] = cfg.get("skill_id") or "audio_bed"
            cfg["tools"] = ["call_music_model", "read_upstream", "call_model"]
            if can_music:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["director_task"] = (
                    cfg.get("director_task")
                    or "Compose ONE non-vocal BGM bed from the Brief / bgm_lock for the "
                    "full concatenated film. call_music_model. Never generate per-shot scores. "
                    "Keep headroom so shot dialogue stays intelligible."
                )
                cfg["placeholder_until_api"] = False
                embedded_assigned.append(f"{nid}:music_agent")
            else:
                cfg["force_handler"] = True
                cfg["delegate"] = "handler"
                cfg["placeholder_until_api"] = True
                cfg["director_task"] = (
                    "No music API yet. Output a silent/empty placeholder file only. "
                    "Do not invent a score. When MUSIC_API_KEY / models.music is "
                    "configured, this node will call_music_model instead."
                )
                embedded_assigned.append(f"{nid}:music_placeholder")
            node["config"] = cfg
        routing["clip_embedded"] = True
        routing["can_speech"] = can_speech
        routing["can_music"] = can_music
        routing["speech_nodes"] = False
        routing["music_nodes"] = any(
            str(n.get("id") or "") == "n_music"
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict)
        )
        routing["can_video_audio"] = bool(backends.get("can_video_audio", True))
        routing["video_audio_model"] = str(backends.get("video_audio_model") or "")
        meta["audio_routing"] = routing
        meta["prefer_clip_native_audio"] = True
        meta["prefer_wan3_clip_audio"] = True  # legacy alias
        if isinstance(meta.get("script_analysis"), dict):
            from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

            meta["script_analysis"] = ensure_audio_locks_on_analysis(
                meta["script_analysis"],
                str(graph.get("description") or meta.get("user_prompt") or ""),
            )
            meta["language_lock"] = str(
                meta["script_analysis"].get("language_lock")
                or meta.get("language_lock")
                or "en"
            )
            if isinstance(meta["script_analysis"].get("bgm_lock"), dict):
                meta["bgm_lock"] = meta["script_analysis"]["bgm_lock"]
        graph["metadata"] = meta
        meta["director_audio_assignment"] = {
            "can_speech": can_speech,
            "can_music": can_music,
            "can_video_audio": bool(backends.get("can_video_audio", True)),
            "assigned": embedded_assigned,
            "ensured_nodes": [],
            "backends": backends,
            "clip_embedded": True,
            "prefer_clip_native_audio": True,
            "prefer_wan3_clip_audio": True,  # legacy alias
        }
        return meta["director_audio_assignment"]

    ensured = _ensure_audio_nodes_for_intent(graph)
    assigned: list[str] = []

    for node in graph.get("nodes") or []:
        if is_uploaded_media_node(node):
            continue
        cfg = dict(node.get("config") or {})
        role = _role_key(node).lower()
        nid = str(node.get("id") or "")
        if role in {"speech", "tts"} or nid == "n_speech":
            cfg["role"] = "speech"
            cfg["skill_id"] = cfg.get("skill_id") or "speech_tts"
            cfg["tools"] = ["call_speech_model", "read_upstream"]
            if can_speech:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["director_task"] = (
                    cfg.get("director_task")
                    or "Write concise spoken lines from brief/storyboard, then call_speech_model "
                    "to synthesize TTS. Keep under 8s; sync to film beats. Skip if silent policy."
                )
                assigned.append(f"{nid}:speech_agent")
            else:
                cfg.pop("force_handler", None)
                cfg["delegate"] = "agent"
                cfg["director_task"] = (
                    "No TTS backend — as Speech Agent, write speech timing into shot briefs "
                    "via graph patch / notes; compose will use audible bed."
                )
                assigned.append(f"{nid}:speech_agent_no_backend")
            node["config"] = cfg
        elif role in {"music", "audio", "audio_bed"} or nid == "n_music":
            cfg["role"] = "music"
            cfg["skill_id"] = cfg.get("skill_id") or "audio_bed"
            cfg["tools"] = ["call_music_model", "read_upstream", "call_model"]
            if can_music:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["director_task"] = (
                    cfg.get("director_task")
                    or "Compose a short non-vocal BGM bed matching mood; call_music_model. "
                    "Keep headroom for speech; ≤8s unless compose needs longer."
                )
                assigned.append(f"{nid}:music_agent")
            else:
                cfg.pop("force_handler", None)
                cfg["delegate"] = "agent"
                cfg["director_task"] = (
                    "No music backend — as Music Agent, stamp mood/BGM style onto shot "
                    "prompts; compose will mux an audible bed."
                )
                assigned.append(f"{nid}:music_agent_no_backend")
            node["config"] = cfg

    meta = dict(graph.get("metadata") or {})
    meta["director_audio_assignment"] = {
        "can_speech": can_speech,
        "can_music": can_music,
        "assigned": assigned,
        "ensured_nodes": ensured,
        "backends": backends,
        "clip_embedded": False,
    }
    graph["metadata"] = meta
    return meta["director_audio_assignment"]


def _pick_model(models: list[dict[str, Any]], *, prefer_image: bool = False) -> str:
    if not models:
        return ""
    ordered = list(models)
    if prefer_image:
        for m in ordered:
            name = f"{m.get('id') or ''} {m.get('model_name') or ''}".lower()
            if any(k in name for k in ("image", "qwen-image", "flux", "sdxl", "wan")):
                return str(m.get("id") or "")
    for m in ordered:
        if m.get("is_default"):
            return str(m.get("id") or "")
    return str(ordered[0].get("id") or "")


def _clamp_score(value: Any, default: int = 5) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(10, score))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Delegate to shared robust parser (fences + first object + trailing text)."""
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _extract_json_object as _shared_extract_json_object,
    )

    return _shared_extract_json_object(text)


_JSON_RETRY_RULE = (
    " Your previous reply could not be parsed. Reply with exactly one JSON object: "
    "no reasoning, no markdown fences, and every newline or quote inside a string escaped."
)


async def _model_json_object(
    *, phase: str, prompt: str, system: str, max_tokens: int
) -> dict[str, Any] | None:
    """Ask the chat model for a JSON object, re-asking once when the reply does not parse."""
    from jiuwenswarm.server.runtime.designer.model_tools import model_text_or_raise

    for attempt in (1, 2):
        result = await call_model_tool(
            prompt=prompt,
            system=system if attempt == 1 else system + _JSON_RETRY_RULE,
            max_tokens=max_tokens,
        )
        parsed = _extract_json_object(model_text_or_raise(result))
        if parsed is not None:
            return parsed
        logger.warning(
            "Director %s reply was not a JSON object (attempt %d, finish=%s)",
            phase,
            attempt,
            result.get("finish_reason"),
        )
    return None


def validate_plan_occupancy(analysis: dict[str, Any]) -> dict[str, Any]:
    """Code validators after Director patch (no LLM): setting_id, on_screen, scene_locks, ids."""
    errors: list[str] = []
    warnings: list[str] = []
    characters = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (analysis.get("clips") or []) if isinstance(s, dict)]
    scenes = [s for s in (analysis.get("scenes") or []) if isinstance(s, dict)]
    scene_locks = (
        analysis.get("scene_locks")
        if isinstance(analysis.get("scene_locks"), dict)
        else {}
    )
    valid_ids = {str(c.get("id") or "") for c in characters if str(c.get("id") or "")}
    if not characters:
        errors.append("no_characters")
    if not shots:
        errors.append("no_shots")
    for i, shot in enumerate(shots, start=1):
        idx = int(shot.get("clip_index") or i)
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if not sid:
            errors.append(f"shot{idx}_missing_setting_id")
        on_screen = _str_ids(
            shot.get("on_screen") or shot.get("visible_cast_ids") or shot.get("featured_cast_ids") or []
        )
        if not on_screen:
            errors.append(f"shot{idx}_empty_on_screen")
        for cid in on_screen:
            if cid not in valid_ids:
                errors.append(f"shot{idx}_unknown_on_screen:{cid}")
        if sid and sid not in scene_locks and scenes:
            # Prefer explicit locks; warn if missing (builder may synthesize).
            warnings.append(f"shot{idx}_missing_scene_lock:{sid}")
        # Phase 3 light check: multi-shot same setting should diversify views when present.
    by_setting: dict[str, list[str]] = {}
    for shot in shots:
        sid = str(shot.get("setting_id") or "").strip()
        vk = str(shot.get("view_key") or "").strip()
        if sid and vk:
            by_setting.setdefault(sid, []).append(vk)
    for sid, views in by_setting.items():
        if len(views) >= 2 and len(set(views)) < 2:
            warnings.append(f"setting_{sid}_views_not_diverse")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def note_user_canvas_edits(graph: DesignerExecutionGraph) -> list[str]:
    """Record add/remove actions so Director treats the user's canvas as fact."""
    meta = dict(graph.get("metadata") or {})
    raw = meta.get("user_canvas_edits")
    edits = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    added = [item for item in edits if str(item.get("op") or "") == "add"]
    removed = [item for item in edits if str(item.get("op") or "") == "remove"]
    connected = [item for item in edits if str(item.get("op") or "") == "connect"]
    disconnected = [item for item in edits if str(item.get("op") or "") == "disconnect"]
    replaced = [item for item in edits if str(item.get("op") or "") == "replace"]
    if not edits:
        if "director_canvas_awareness" in meta:
            meta.pop("director_canvas_awareness", None)
            graph["metadata"] = meta
        return []

    def _brief(item: dict[str, Any]) -> dict[str, str]:
        brief = {
            "node_id": str(item.get("node_id") or ""),
            "label": str(item.get("label") or ""),
            "role": str(item.get("role") or ""),
            "type": str(item.get("type") or ""),
        }
        peer = str(item.get("peer_id") or "").strip()
        if peer:
            brief["peer_id"] = peer
        return brief

    meta["director_canvas_awareness"] = {
        "added": [_brief(item) for item in added[-20:]],
        "removed": [_brief(item) for item in removed[-20:]],
        "connected": [_brief(item) for item in connected[-20:]],
        "disconnected": [_brief(item) for item in disconnected[-20:]],
        "replaced": [_brief(item) for item in replaced[-20:]],
    }
    graph["metadata"] = meta
    notes: list[str] = []
    if added:
        notes.append(
            "canvas_added:" + ",".join(str(item.get("node_id") or "") for item in added[-8:])
        )
    if removed:
        notes.append(
            "canvas_removed:" + ",".join(str(item.get("node_id") or "") for item in removed[-8:])
        )
    if connected:
        notes.append(
            "canvas_connected:"
            + ",".join(
                f"{item.get('node_id') or ''}>{item.get('peer_id') or ''}" for item in connected[-8:]
            )
        )
    if disconnected:
        notes.append(
            "canvas_disconnected:"
            + ",".join(
                f"{item.get('node_id') or ''}>{item.get('peer_id') or ''}"
                for item in disconnected[-8:]
            )
        )
    if replaced:
        notes.append(
            "canvas_replaced:" + ",".join(str(item.get("node_id") or "") for item in replaced[-8:])
        )
    return notes


class Director:
    """Single overseeing agent: brief, storyboard, graph, locks, prompt gate, and ratings.

    team.design replaced the Director conversation with the design leader.
    Live entry points: ``design.director_chain`` (authoring for
    ``designer_graph_autorun``) and :meth:`onboard_user_added_nodes`, called by
    ``designer_adapter._save_graph`` to promote user-drawn canvas nodes.
    """

    def onboard_user_added_nodes(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """When the user adds canvas nodes: promote to LLM agents,
        decide tools, and let Director lock-check media prompts.

        Edges stay as the user drew them. Never deletes user_added orphans.
        Chat credentials are gated at Enter/chat/Play entry — never demote to
        handler when the model is missing.
        """
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            find_non_contributing_node_ids,
        )

        notes: list[str] = []
        onboarded: list[str] = []
        notes.extend(note_user_canvas_edits(graph))

        from jiuwenswarm.server.runtime.designer.user_references import (
            is_uploaded_media_node,
        )

        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            # Uploaded references are routed by image classification, not by
            # the "user drew this node, do not invent edges" rule.
            if not nid or is_uploaded_media_node(node) or not cfg.get("user_added"):
                continue
            onboarded.append(nid)
            if is_comfyui_node(node):
                # Imported ComfyUI params go to vLLM-Omni verbatim; no agent rewrites them.
                cfg["force_handler"] = True
                cfg["skip_llm"] = True
                cfg["delegate"] = "handler"
                node["config"] = cfg
                notes.append(f"comfyui:{nid}")
                continue
            role = _role_key(node)
            tools = _tools_for_node(node)
            cfg["kind"] = "agent"
            cfg["tools"] = tools
            cfg["user_added"] = True
            cfg.pop("force_handler", None)
            cfg.pop("skip_llm", None)
            cfg["delegate"] = "agent"
            if not str(cfg.get("director_task") or "").strip():
                cfg["director_task"] = (
                    f"User-added {role or node.get('type') or 'node'} agent. "
                    f"Use tools {', '.join(tools)}. "
                    "Respect film-wide style_lock, spatial_lock, and "
                    "costume/identity locks. Use only the edges the user connected. "
                    "Do not invent upstream or downstream links."
                )[:800]
            notes.append(f"agent:{nid}")
            notes.append(f"unwired_until_user:{nid}")

            node["config"] = cfg

        # Director lock-gates every user-added media leaf.
        lock_notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
            if (
                not cfg.get("user_added")
                or is_uploaded_media_node(node)
                or is_comfyui_node(node)
            ):
                continue
            if _role_key(node) not in {
                "clip",
                "character",
                "scene",
                "image",
                "video",
            }:
                continue
            gate = self.review_leaf_media_prompt(graph, node)
            if gate.get("patched"):
                lock_notes.append(f"locks:{node.get('id')}")
            notes.extend(
                [f"mgr:{x}" for x in (gate.get("notes") or []) if isinstance(x, str)][:4]
            )

        user_added_ids: set[str] = set()
        for n in graph.get("nodes") or []:
            if isinstance(n, dict) and isinstance(n.get("config"), dict) and n["config"].get("user_added"):
                user_added_ids.add(str(n.get("id") or ""))
        orphans = [nid for nid in find_non_contributing_node_ids(graph) if nid in user_added_ids]
        meta = dict(graph.get("metadata") or {})
        if orphans:
            meta["non_contributing_user_nodes"] = orphans
            meta["contribution_warning"] = (
                "User-added nodes do not feed the final shot/compose: "
                + ", ".join(orphans)
                + ". Connect them into the pipeline if you want them in the film."
            )
        elif onboarded:
            meta.pop("non_contributing_user_nodes", None)
            meta.pop("contribution_warning", None)
        result = {
            "ok": True,
            "onboarded": onboarded,
            "orphans": orphans,
            "notes": notes[:80],
            "lock_notes": lock_notes[:40],
        }
        meta["director_user_node_onboard"] = result
        graph["metadata"] = meta
        return result

    async def plan(
        self,
        graph: DesignerExecutionGraph,
        *, prior_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        models = list_configured_models()
        model_ids = [str(m.get("id")) for m in models]
        nodes = graph.get("nodes") or []
        global_summary = ""
        if prior_feedback:
            final = prior_feedback.get("final") or {}
            global_summary = str(final.get("summary") or final.get("improvement_plan") or "")

        system = (
            "You are the Designer Director Agent. "
            "One forward pass only (no loops). "
            "From the user prompt, ensure a detailed brief covering character consistency, "
            "scene consistency (spatial lock: landmarks/layout/light must not drift), "
            "motion consistency, and consistency. "
            "Assign each leaf node a concrete task + tools so agents produce real media "
            "(images/video), not markdown stubs. Never create audio nodes. "
            "Scene master plate must be authored first; later scene views must EDIT that plate. "
            "Coordinate node agents in a ComfyUI-like pipeline. "
            "Follow the scenario skill and audio policy. "
            "For each node, choose a preferred_model from the configured Settings model list. "
            "On rerun, incorporate prior feedback suggestions. "
            "Respond with JSON only: "
            '{"brief_notes":"...",'
            '"spatial_lock":{"setting":"...","landmarks":"...","light":"...","static_rule":"..."},'
            '"node_directives":{"<node_id>":{"preferred_model":"...","task":"..."}},'
            '"notes":"..."}'
        ) + _language_rule()
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "available_models": model_ids,
                "scenario_skill": str((graph.get("metadata") or {}).get("scenario_skill_excerpt") or "")[
                    :2500
                ],
                "director_skill": str(
                    (graph.get("metadata") or {}).get("director_skill_excerpt")
                    or (graph.get("metadata") or {}).get("active_director_skill")
                    or ""
                )[:2000],
                "audio_intent": (graph.get("metadata") or {}).get("audio_intent"),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "label": n.get("label"),
                        "type": n.get("type"),
                        "agent": (n.get("config") or {}).get("agent_name"),
                        "prior_suggestion": suggestion_for_node(
                            prior_feedback, str(n.get("id") or "")
                        ),
                    }
                    for n in nodes
                ],
                "prior_global_summary": global_summary,
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            max_tokens=32768,
        )
        text = model_text_or_raise(result)
        parsed = _extract_json_object(text) or {}
        if not parsed:
            raise DesignerLlmError(
                "Chat model did not return a usable director plan.",
                code=LLM_API_ERROR,
            )
        directives: dict[str, Any] = {}
        raw_dirs = parsed.get("node_directives") if isinstance(parsed, dict) else None
        if isinstance(raw_dirs, dict):
            directives = raw_dirs
        for node in nodes:
            nid = str(node.get("id") or "")
            if not nid:
                continue
            entry = directives.get(nid) if isinstance(directives.get(nid), dict) else {}
            preferred = str(entry.get("preferred_model") or "")
            if preferred and preferred not in model_ids and model_ids:
                preferred = model_ids[0]
            elif not preferred and model_ids:
                preferred = model_ids[0]
            tools = _tools_for_node(node)
            directives[nid] = {
                "preferred_model": preferred,
                "task": str(entry.get("task") or node_task("execute", label=node.get("label") or nid)),
                "tools": tools,
            }
        plan = {
            "node_directives": directives,
            "notes": str(parsed.get("notes") or result.get("text") or "")[:2000],
            "brief_notes": str(parsed.get("brief_notes") or "")[:2000],
            "spatial_lock": parsed.get("spatial_lock")
            if isinstance(parsed.get("spatial_lock"), dict)
            else {},
            "planner_model": result.get("model"),
        }
        for node in nodes:
            nid = str(node.get("id") or "")
            cfg = dict(node.get("config") or {})
            d = directives.get(nid) or {}
            cfg["preferred_model"] = d.get("preferred_model", "")
            cfg["director_task"] = d.get("task", "")
            cfg["tools"] = d.get("tools") or _tools_for_node(node)
            cfg["kind"] = "agent"
            node["config"] = cfg
        meta = dict(graph.get("metadata") or {})
        meta["director_plan"] = plan
        if plan.get("brief_notes"):
            meta["director_brief_notes"] = plan["brief_notes"]
        if plan.get("spatial_lock"):
            meta["spatial_lock"] = {
                str(k): str(v)[:400] for k, v in plan["spatial_lock"].items() if str(v).strip()
            }
            analysis = dict(meta.get("script_analysis") or {})
            analysis["spatial_lock"] = meta["spatial_lock"]
            meta["script_analysis"] = analysis
            _spatial_geography_lock_patch(graph)
        graph["metadata"] = meta
        audio_assign = assign_audio_node_agents(graph)
        plan["audio_assignment"] = audio_assign
        meta = dict(graph.get("metadata") or {})
        meta["director_plan"] = plan
        graph["metadata"] = meta
        return plan

    async def author_creative_brief(
        self, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        """LLM-author a detailed brief onto n_brief. Failures raise ``DesignerLlmError``."""
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        scenes = list(analysis.get("scenes") or [])
        audio = (
            dict(analysis.get("audio") or meta.get("audio_intent") or {})
            if isinstance(analysis.get("audio") or meta.get("audio_intent"), dict)
            else {}
        )
        user_prompt = str(graph.get("description") or "")
        brief_md = ""
        source = "llm"
        notes = "Director LLM authored creative brief."
        try:
            system = (
                "You are the Designer Director. Author a detailed creative brief "
                "for a short-form video. Explicit user facts and constraints are authoritative. "
                "When the request is sparse, creatively develop unspecified content into a "
                "specific, coherent concept instead of restating or stretching the premise. "
                "The brief MUST include: Creative concept; Narrative/content arc; a timed shot "
                "plan spanning the full requested duration; and Script/speech plan. "
                "For narrative or celebration content, build setup → development/turn → payoff. "
                "For advertising, build hook → desire/problem → demonstration/proof → payoff/CTA. "
                "Each timed shot must add new action, information, or emotion—no filler, repeated "
                "action, or duplicate camera coverage. In Script/speech plan, write concise exact "
                "dialogue or voiceover lines with speaker and timing. Speech is the default. "
                "Do not choose visual-only storytelling, silent lip-sync, or 无声 unless the "
                "user explicitly asked for mime, a silent film, or no dialogue. Never add "
                "speech when the user requested that. Also cover: character identity locks "
                "(face/hair/body/costume) for every on-screen person, including unnamed groups "
                "that share one look; list that character on every shot where they are visible, "
                "scene geography (spatial lock), motion consistency, time-coherent continuity "
                "(do not undo a completed shot on a later shot), "
                "shot-view coverage for every named beat, audio policy, and one explicit "
                "Visual Style section. Preserve the user's exact visual medium and rendering "
                "details; use the provided style_lock when present. "
                "Preserve all explicit people, places, brand facts, claims, and requested events; "
                "creative enrichment may fill only details the user left unspecified. "
                "Respond with markdown brief only (no JSON wrapper)."
            ) + _language_rule()
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "characters": characters,
                        "scenes": scenes,
                        "clips": analysis.get("clips"),
                        "style_lock": analysis.get("style_lock"),
                        "audio": audio,
                        "spatial_lock": meta.get("spatial_lock"),
                        "director_brief_notes": meta.get("director_brief_notes")
                        or ((meta.get("director_plan") or {}).get("brief_notes")),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                max_tokens=32768,
            )
            text = model_text_or_raise(result)
            if len(text) <= 80:
                raise DesignerLlmError(
                    "Chat model returned an empty or too-short creative brief.",
                    code=LLM_API_ERROR,
                )
            brief_md = text if text.lstrip().startswith("#") else f"# Brief\n\n{text}"
            source = "llm"
            notes = "Director LLM authored creative brief."
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director author_creative_brief LLM failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while authoring the brief: {exc}",
                code=LLM_API_ERROR,
            ) from exc
        if not brief_md:
            raise DesignerLlmError(
                "Designer requires LLM to author the creative brief.",
                code=LLM_API_ERROR,
            )
        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            ensure_visual_style_statement,
            synchronize_graph_style_from_brief,
        )

        brief_md = ensure_visual_style_statement(
            brief_md,
            analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {},
        )

        stamped = False
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "brief" and str(node.get("id") or "") != "n_brief":
                continue
            cfg.pop("skip_llm", None)
            cfg["kind"] = "agent"
            node["config"] = cfg
            stamped = True
            break
        meta["approved_brief"] = brief_md
        graph["metadata"] = meta
        synchronize_graph_style_from_brief(graph, brief_md)
        meta = dict(graph.get("metadata") or {})
        meta["director_brief_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "stamped": stamped,
            "chars": len(brief_md),
        }
        graph["metadata"] = meta
        return dict(meta["director_brief_ack"])

    async def author_storyboard(
        self, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        """LLM-author planned_shots; the storyboard table is rendered from them. Failures raise."""
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import render_storyboard_table
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        shots = list(analysis.get("clips") or [])
        user_prompt = str(graph.get("description") or "")
        # Keep the enriched timed shot and speech sections available to storyboard authoring.
        approved_brief = str(meta.get("approved_brief") or "")[:12000]
        source = ""
        notes = "Director LLM authored storyboard."
        try:
            system = (
                "You are the Designer Director. Author the storyboard as structured "
                "shots grouped by setting_id. FIRST list every "
                "on-screen human as characters[] (id, name, description) — one identity card "
                "each, including unnamed groups that share one look. Put that id in on_screen "
                "on every shot where they are visible. NOT every character appears in every scene. "
                + CLOTHING_PARTS_RULE
                + "Copy the approved Brief's visual style exactly into style_lock; never "
                "substitute a model or leaf default. "
                "Different setting_id = DIFFERENT place (distinct architecture). "
                "Group shots by setting_id. Shots are consecutive TIME windows that "
                "concatenate to the film — each action is THAT window in FULL DETAIL "
                "(blocking, speech, wardrobe, camera); do not paste the entire user "
                "prompt into every shot; do not restage the whole story from a new "
                "camera unless the user asked for same-moment coverage. "
                "Materialize the Brief's entire narrative/content arc and timed shot plan into "
                "these structured shots. Every shot must advance the story, message, product "
                "demonstration, or emotional state; no filler, repeated action, or cosmetic "
                "coverage used to consume runtime. Ensure the timelines span the requested "
                "duration and preserve setup/hook, development, and payoff/CTA as applicable. "
                "A shot duration already written in the approved brief is fixed: copy that "
                "length exactly and do not shorten it. Character names, wardrobe, and body "
                "locks written there stay on those characters. Place names and geography "
                "written there stay on those scenes. Continuity notes and per-shot story "
                "beats written there stay in the shot; do not rename people, move the scene, "
                "or replace the beat. "
                "Materialize the Brief's Script/speech plan as exact speech_by_character and "
                "speech_line values so later clips can speak them. Speech is the default: "
                "every shot where someone talks needs character_id→exact line in "
                "speech_by_character and the joined speech_line, in language_lock. "
                "Copy lines the Brief already wrote. If the Brief left them out and the user "
                "did not ask for mime, a silent film, or no dialogue, write short exact lines "
                "and put them in those fields. Do not leave speaking shots empty, and do not "
                "substitute silent lip-sync, (silent), or 无声 for words. "
                "Empty speech fields only when nobody speaks in that window, or the user asked "
                "for no dialogue. "
                "First shot of each setting: "
                "clip_strategy=compose_from_character_specs — composer places ONLY "
                "on_screen cast with cast_actions (who is doing what). "
                "Later same setting: edit_prior_image (architecture locked); "
                "storyboard updates on_screen / offscreen / cast_actions. "
                "offscreen = in this scene but not in frame; never draw them. "
                "Cast absent from a setting must not appear there. "
                "NO scene specs. Crowd/extras persist across same-setting shots "
                "unless they exit. Each shot needs timeline, camera, action, "
                "on_screen, offscreen, cast_actions, featured_cast_ids, setting_id, "
                "start_state {pose,seats,facing,on_screen,offscreen}, "
                "end_state {pose,seats,facing,exited,speech_done,on_screen}, "
                "continuity_lock, clip_prompt, exiting_character_ids, "
                "speech_by_character (map character_id→exact spoken line for this shot; "
                "empty {} only when nobody speaks in this window), speech_line (joined fallback). "
                "Same setting_id: next shot start_state MUST match prior end_state. "
                "Exactly one shot has emotion=climax. Each shot includes irreversible "
                "(what is newly true at the last frame) and cast_states "
                "{id:{wardrobe,emotion,presence}}. Face stays the character sheet; "
                "change wardrobe or emotion only when this shot's story changes them. "
                "Shots are self-contained continuity windows — do not rely on prior "
                "clip media. "
                + "Film-wide locks: language_lock (e.g. en/zh — ALL dialogue in that "
                "language), bgm_lock {mood,style,instruments,continuity,rule}, "
                "include_speech, include_music. "
                "Respond JSON only: "
                '{"style_lock":{"look":"...","medium":"..."},'
                '"characters":[{"id":"char_1","name":"...","description":"...",'
                '"clothing_parts":{"top":"...","bottom":"...","outerwear":"","footwear":"...",'
                '"accessories":"","outfit":""}}],'
                '"clips":[{"clip_index":1,"timeline":"<copy this shot timeline from the approved brief>",'
                '"camera":"...",'
                '"action":"...","on_screen":["char_1"],"offscreen":["char_2"],'
                '"featured_cast_ids":["char_1"],"cast_actions":{"char_1":"..."},'
                '"ensemble_cast_ids":["char_1","char_2"],"setting_id":"set_1",'
                '"clip_strategy":"compose_from_character_specs",'
                '"start_state":{"pose":"...","seats":{},"facing":"..."},'
                '"end_state":{"pose":"...","exited":[],"speech_done":"..."},'
                '"continuity_lock":{"forbid":"..."},"clip_prompt":"...",'
                '"exiting_character_ids":[],'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"language_lock":"<spoken language code, e.g. zh or en>",'
                '"bgm_lock":{"mood":"...","style":"...","instruments":"...",'
                '"continuity":"same bed","rule":"non-vocal underscore"},'
                '"include_speech":true,"include_music":true,'
                '"notes":"...","target_clip_count":N}'
            ) + _language_rule() + POSITIVE_VISUAL_RULE
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": approved_brief,
                        "style_lock": analysis.get("style_lock"),
                        "characters": characters,
                        "clips": shots,
                        "spatial_lock": meta.get("spatial_lock"),
                        "rule": (
                            "Multi-shot storyboard required when multiple beats exist. "
                            "Write exact speech_by_character and speech_line on every shot "
                            "where someone talks, so later clips can speak those lines."
                        ),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                max_tokens=65536,
            )
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text) or {}
            llm_chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
            if llm_chars:
                cleaned_chars: list[dict[str, Any]] = []
                for i, raw in enumerate(llm_chars, start=1):
                    if not isinstance(raw, dict):
                        continue
                    cleaned_chars.append(_llm_character_card(raw, i, characters))
                if cleaned_chars:
                    characters = cleaned_chars
                    analysis["characters"] = characters
                    notes = "Director LLM authored cast + storyboard."
            llm_shots = parsed.get("clips") if isinstance(parsed.get("clips"), list) else []
            if llm_shots:
                cleaned: list[dict[str, Any]] = []
                for i, raw in enumerate(llm_shots, start=1):
                    if not isinstance(raw, dict):
                        continue
                    shot = dict(raw)
                    shot["clip_index"] = int(shot.get("clip_index") or i)
                    if not str(shot.get("timeline") or "").strip():
                        shot["timeline"] = f"{(i - 1) * 5:.1f}-{i * 5:.1f}s"
                    if isinstance(shot.get("continuity_lock"), dict):
                        shot["continuity_lock"] = {
                            str(k): str(v) for k, v in shot["continuity_lock"].items()
                        }
                    elif str(shot.get("action") or "").strip():
                        shot["continuity_lock"] = _infer_continuity_lock(
                            str(shot.get("action") or "")
                        )
                    if isinstance(shot.get("speech_by_character"), dict):
                        shot["speech_by_character"] = {
                            str(k): str(v)
                            for k, v in shot["speech_by_character"].items()
                            if str(v).strip()
                        }
                    if shot.get("speech_line"):
                        shot["speech_line"] = str(shot.get("speech_line"))
                    cleaned.append(shot)
                if cleaned:
                    shots = cleaned
                    analysis["clips"] = cleaned
                    source = "llm"
                    analysis["source"] = "llm"
                    notes = str(parsed.get("notes") or "Director LLM authored storyboard.")[
                        :1000
                    ]
            # Film-wide audio locks from Director storyboard JSON.
            audio = dict(analysis.get("audio") or {})
            if parsed.get("language_lock"):
                analysis["language_lock"] = str(parsed.get("language_lock"))[:16]
                audio["language_lock"] = analysis["language_lock"]
            if isinstance(parsed.get("bgm_lock"), dict):
                analysis["bgm_lock"] = {
                    str(k): str(v)[:280] for k, v in parsed["bgm_lock"].items()
                }
                audio["bgm_lock"] = analysis["bgm_lock"]
            if "include_speech" in parsed:
                audio["include_speech"] = bool(parsed.get("include_speech"))
            if "include_music" in parsed:
                audio["include_music"] = bool(parsed.get("include_music"))
            if audio.get("include_speech") and audio.get("include_music"):
                audio["policy"] = "speech_and_music"
            elif audio.get("include_speech"):
                audio["policy"] = "speech"
            elif audio.get("include_music"):
                audio["policy"] = audio.get("policy") or "optional_music"
            analysis["audio"] = audio
            if source != "llm":
                raise DesignerLlmError(
                    "Chat model did not return a usable storyboard.",
                    code=LLM_API_ERROR,
                )
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director author_storyboard LLM failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while authoring the storyboard: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        from jiuwenswarm.server.runtime.designer.audio_locks import (
            ensure_audio_locks_on_analysis,
            is_spoken_line,
            user_declined_speech,
        )
        from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import apply_shot_scope

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        analysis = apply_shot_scope(analysis, user_prompt)
        shots = list(analysis.get("clips") or shots)
        if shots and not user_declined_speech(user_prompt):
            has_line = any(
                is_spoken_line(str(s.get("speech_line") or ""))
                or (
                    isinstance(s.get("speech_by_character"), dict)
                    and any(is_spoken_line(str(v)) for v in s["speech_by_character"].values())
                )
                for s in shots
                if isinstance(s, dict)
            )
            if not has_line:
                raise DesignerLlmError(
                    "Chat model did not write spoken lines into the storyboard.",
                    code=LLM_API_ERROR,
                )
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import (
                ensure_clip_start_end_states,
                validate_storyboard_state_chain,
            )

            shots = ensure_clip_start_end_states(shots)
            analysis["clips"] = shots
            chain_notes = validate_storyboard_state_chain(shots)
            if chain_notes:
                meta["storyboard_state_notes"] = chain_notes[:20]
        except Exception:  # noqa: BLE001
            pass
        characters = list(analysis.get("characters") or characters)
        meta["language_lock"] = str(analysis.get("language_lock") or "en")
        if isinstance(analysis.get("bgm_lock"), dict):
            meta["bgm_lock"] = analysis["bgm_lock"]

        if shots:
            analysis["clips"] = shots
            meta["script_analysis"] = analysis
        if not shots:
            raise DesignerLlmError(
                "Chat model did not return a usable storyboard.",
                code=LLM_API_ERROR,
            )
        sb_md = render_storyboard_table(shots, characters, current_content_language())

        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            synchronize_graph_style_from_brief,
        )

        stamped = False
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "storyboard" and str(node.get("id") or "") != "n_storyboard":
                continue
            cfg.pop("skip_llm", None)
            cfg["planned_shots"] = shots
            node["config"] = cfg
            stamped = True
            break
        if shots:
            analysis["target_clip_count"] = len(shots)
            meta["script_analysis"] = analysis
        # Propagate timelines/consistency/audio locks into clip configs once.
        from jiuwenswarm.server.runtime.designer.audio_locks import stamp_audio_fields_on_clip_config

        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) not in {"clip"}:
                continue
            idx = int(cfg.get("clip_index") or 0)
            for shot in shots:
                if int(shot.get("clip_index") or 0) != idx:
                    continue
                if shot.get("action"):
                    cfg["clip_action"] = str(shot["action"])
                if shot.get("camera"):
                    cfg["camera"] = str(shot["camera"])
                if shot.get("timeline"):
                    cfg["timeline"] = str(shot["timeline"])
                if isinstance(shot.get("continuity_lock"), dict):
                    cfg["continuity_lock"] = shot["continuity_lock"]
                if isinstance(shot.get("character_ids"), list):
                    cfg["character_ids"] = [str(x) for x in shot["character_ids"] if str(x)]
                if _role_key(node) == "clip":
                    routing = meta.get("audio_routing") if isinstance(meta.get("audio_routing"), dict) else {}
                    cfg = stamp_audio_fields_on_clip_config(
                        cfg,
                        shot=shot,
                        analysis=analysis,
                        meta=meta,
                        clip_embedded=bool(routing.get("clip_embedded")),
                    )
                else:
                    if shot.get("speech_line"):
                        cfg["speech_line"] = str(shot.get("speech_line"))
                    if isinstance(shot.get("speech_by_character"), dict):
                        cfg["speech_by_character"] = shot["speech_by_character"]
                    cfg["language_lock"] = str(
                        shot.get("language_lock") or analysis.get("language_lock") or "en"
                    )
                node["config"] = cfg
                break

        # Chain prior speech across consecutive shots so N+1 does not restate N.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                chain_prior_speech_across_clips,
            )

            chain_prior_speech_across_clips(graph)
        except Exception:  # noqa: BLE001
            logger.debug("chain_prior_speech_across_clips failed", exc_info=True)

        meta["approved_storyboard"] = sb_md
        graph["metadata"] = meta
        synchronize_graph_style_from_brief(graph, str(meta.get("approved_brief") or ""))
        meta = dict(graph.get("metadata") or {})
        meta["director_storyboard_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "stamped": stamped,
            "shot_count": len(shots),
            "language_lock": meta.get("language_lock"),
            "bgm_lock": bool(meta.get("bgm_lock")),
        }
        graph["metadata"] = meta
        return dict(meta["director_storyboard_ack"])

    async def design_execution_graph(
        self,
        graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Director owns flexible multi-shot topology from Brief+Storyboard.

        Not a frozen single-clip template: LLM expands shots from the locked
        storyboard, then materializes one clip agent per shot.
        """
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
        )

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        shots = [s for s in (analysis.get("clips") or []) if isinstance(s, dict)]
        characters = list(analysis.get("characters") or [])
        user_prompt = str(graph.get("description") or meta.get("user_prompt") or "")
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            reference_led_active,
        )

        if reference_led_active(analysis):
            ack = {
                "ok": True,
                "source": "reference_led",
                "notes": "Reference-led graph kept.",
                "shot_count": len(shots),
            }
            meta["director_graph_ack"] = ack
            graph["metadata"] = meta
            return dict(ack)
        approved_brief = str(meta.get("approved_brief") or "")[:12000]
        approved_sb = str(meta.get("approved_storyboard") or "")[:16000]
        source = "storyboard"
        notes = ""

        try:
            system = (
                "You are the Designer Director. Design the execution graph "
                "from the approved Brief + Storyboard. Return JSON only. "
                "Preserve the provided style_lock exactly; it is the approved visual "
                "medium for every character, scene, and clip. "
                "MUST include characters[] — every on-screen human gets one identity "
                "card (id, name, description), including unnamed groups that share one look. "
                "List that id in on_screen wherever they are visible. "
                "NOT every character in every scene. "
                + CLOTHING_PARTS_RULE
                + "MUST include shots[] grouped by setting_id (distinct places). "
                "Shots are consecutive TIME windows that concatenate to the film. "
                "Each shot.action is THAT window only — do not paste the user prompt "
                "into every clip. Preserve every distinct narrative/content shot from the "
                "approved storyboard; each action must advance the story or message, not repeat "
                "an earlier action as filler or alternate coverage. Preserve exact per-shot "
                "speech_by_character and speech_line from the storyboard. Do not drop those "
                "lines. include_speech stays true unless the user asked for mime, a silent "
                "film, or no dialogue. "
                "story window needs, with contiguous timelines. A duration the approved brief "
                "already gave a shot stays that length. Keep the brief's character names and "
                "wardrobe, place names, and continuity notes. Retain every storyboard "
                "clip that provides distinct content. "
                "Keep every distinct shot. Do not drop shots to fit a fixed count. "
                "One clip = one continuous shot of that window's length. New clip on hard cut, new setting, "
                "wardrobe/prop change, or on-screen cast change. "
                "Qwen image: lock identity+wardrobe for every visible person, including a "
                "shared-look group as one reference. One variable per new clip. "
                "Honor explicit N-shot / N分镜 as a HARD ceiling. "
                "First clip of each setting: compose_from_character_specs with on_screen + "
                "cast_actions (composer decides who appears and what they are doing). "
                "Later same setting: edit_prior_image (architecture locked). "
                "offscreen stay out of frame. "
                "Scene specs ARE required — environment-only Qwen images. "
                "Each shot: clip_index, timeline, camera, action, on_screen, offscreen, "
                "cast_actions, featured_cast_ids, ensemble_cast_ids, setting_id, "
                "clip_prompt, exiting_character_ids, clip_strategy, "
                "speech_by_character, speech_line. "
                "Schema: "
                '{"style_lock":{"look":"...","medium":"..."},'
                '"characters":[{"id":"char_1","name":"...","description":"...",'
                '"clothing_parts":{"top":"...","bottom":"...","outerwear":"","footwear":"...",'
                '"accessories":"","outfit":""}}],'
                '"clips":[{"clip_index":1,"timeline":"<copy this shot timeline from the approved brief>",'
                '"action":"...",'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"target_clip_count":N,"include_speech":bool,'
                '"include_music":bool,"notes":"..."}'
            ) + _language_rule() + POSITIVE_VISUAL_RULE
            from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
                infer_shot_budget,
            )

            shot_ceiling = infer_shot_budget(user_prompt, analysis)
            parsed = await _model_json_object(
                phase="execution graph",
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": approved_brief,
                        "approved_storyboard": approved_sb,
                        "style_lock": analysis.get("style_lock"),
                        "characters": characters,
                        "current_shots": shots,
                        "target_clip_count": shot_ceiling or analysis.get("target_clip_count"),
                        "rule": (
                            "Keep every distinct shot from the approved storyboard. "
                            "Do not drop shots to fit a fixed count. Explicit N-shot / "
                            "N分镜 from the user is the only ceiling. All character spec "
                            "cards before clips; compose first clip per setting_id; "
                            "edit_prior only within the same setting_id. Per-shot "
                            "on_screen is authoritative for who appears — not every "
                            "character spec in every clip."
                        ),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                max_tokens=65536,
            )
            if parsed is None:
                raise DesignerLlmError(
                    "Chat model did not return valid execution graph JSON.",
                    code=LLM_API_ERROR,
                )
            llm_chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
            cleaned_chars: list[dict[str, Any]] = []
            for i, raw in enumerate(llm_chars, start=1):
                if not isinstance(raw, dict):
                    continue
                cleaned_chars.append(
                    _llm_character_card(raw, i, list(analysis.get("characters") or []))
                )
            if not cleaned_chars:
                raise DesignerLlmError(
                    "Chat model did not return usable characters while designing "
                    "the execution graph.",
                    code=LLM_API_ERROR,
                )
            characters = cleaned_chars
            analysis["characters"] = characters
            analysis["source"] = "llm"
            source = "llm"
            llm_shots = parsed.get("clips") if isinstance(parsed.get("clips"), list) else []
            cleaned: list[dict[str, Any]] = []
            for i, raw in enumerate(llm_shots, start=1):
                if not isinstance(raw, dict):
                    continue
                shot = dict(raw)
                shot["clip_index"] = int(shot.get("clip_index") or i)
                if not str(shot.get("timeline") or "").strip():
                    shot["timeline"] = f"{(i - 1) * 5:.1f}-{i * 5:.1f}s"
                cleaned.append(shot)
            if not cleaned:
                raise DesignerLlmError(
                    "Chat model did not return usable shots while designing "
                    "the execution graph.",
                    code=LLM_API_ERROR,
                )
            shots = cleaned
            source = "llm"
            analysis["source"] = "llm"
            notes = str(parsed.get("notes") or "")[:1000]
            from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
                _explicit_clip_count_from_prompt,
            )

            explicit_n = int(_explicit_clip_count_from_prompt(user_prompt) or 0)
            # The user's "N shots" is the only ceiling. Otherwise keep every shot.
            if explicit_n >= 1 and len(shots) > explicit_n:
                shots = shots[:explicit_n]
            for i, sh in enumerate(shots, start=1):
                sh["clip_index"] = i
            analysis["target_clip_count"] = len(shots)
            audio = dict(analysis.get("audio") or {})
            if "include_speech" in parsed:
                audio["include_speech"] = bool(parsed.get("include_speech"))
            if "include_music" in parsed:
                audio["include_music"] = bool(parsed.get("include_music"))
            if audio.get("include_speech") and audio.get("include_music"):
                audio["policy"] = "speech_and_music"
            from jiuwenswarm.server.runtime.designer.audio_locks import (
                apply_default_speech_policy,
            )

            audio = apply_default_speech_policy(audio, user_prompt)
            analysis["audio"] = audio
            analysis["scene_consistency_mode"] = "scene_specs_plus_clips"
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director design_execution_graph LLM failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while designing the execution graph: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        # Only an explicit user shot count may shorten the list.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
                _explicit_clip_count_from_prompt,
            )

            explicit_n = int(_explicit_clip_count_from_prompt(user_prompt) or 0)
            if explicit_n >= 1 and len(shots) > explicit_n:
                shots = shots[:explicit_n]
                for i, sh in enumerate(shots, start=1):
                    if isinstance(sh, dict):
                        sh["clip_index"] = i
        except Exception:  # noqa: BLE001
            pass
        if not shots:
            from jiuwenswarm.server.runtime.designer.model_tools import (
                DesignerLlmError,
                LLM_API_ERROR,
            )

            raise DesignerLlmError(
                "Chat model did not return any usable shots while designing "
                "the execution graph.",
                code=LLM_API_ERROR,
            )
        analysis["clips"] = shots
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import (
                apply_shot_scope,
            )

            analysis = apply_shot_scope(analysis, user_prompt)
            shots = list(analysis.get("clips") or shots)
            analysis["clips"] = shots
        except Exception:  # noqa: BLE001
            pass
        analysis["target_clip_count"] = len(shots)
        meta["script_analysis"] = analysis

        # Cast shrink guard: never replace a richer character spec cast with a thinner redesign.
        old_char_specs = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        new_humans = [c for c in characters if isinstance(c, dict) and c.get("id") and not _is_prop_cast(c)]
        if old_char_specs > 1 and len(new_humans) < old_char_specs:
            logger.info(
                "design_execution_graph rejected cast shrink %s → %s; keeping current graph",
                old_char_specs,
                len(new_humans),
            )
            meta["director_composed_on_bootstrap"] = True
            meta["director_graph_ack"] = {
                "ok": True,
                "source": "kept_prior_cast",
                "notes": "Rejected redesign that would shrink character cast.",
                "shot_count": len(shots),
            }
            graph["metadata"] = meta
            return {
                "ok": True,
                "source": "kept_prior_cast",
                "notes": meta["director_graph_ack"]["notes"],
                "shot_count": len(shots),
            }

        old_id = str(graph.get("graph_id") or "")
        project_id = str(graph.get("project_id") or "project")
        rebuilt = build_smart_video_graph(
            project_id=project_id,
            prompt=user_prompt,
            analysis=analysis,
            title=str(graph.get("title") or "") or None,
        )
        rebuilt["graph_id"] = old_id or rebuilt.get("graph_id")
        rebuilt["project_id"] = project_id
        rmeta = dict(rebuilt.get("metadata") or {})
        for key in (
            "approved_brief",
            "approved_storyboard",
            "user_prompt",
            "capability_plan",
            "use_prior_feedback",
            "prior_feedback",
            "last_improvement_plan",
            "director_skill_excerpt",
            "active_director_skill",
            "director_lock_ack",
        ):
            if key in meta and meta.get(key) is not None:
                rmeta[key] = meta.get(key)
        rmeta["script_analysis"] = analysis
        # This replacement is the final workflow. Later storyboard passes stay locked.
        rmeta["freeze_clip_topology"] = True
        rmeta["director_graph_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "shot_count": len(shots),
        }
        rebuilt["metadata"] = rmeta
        carry_user_references(meta, rebuilt)
        rebuilt = apply_runtime_delegate(rebuilt)
        # Replace caller's graph contents in-place-friendly: return rebuilt via ack
        # and let executor assign graph = result.
        # Mutate graph dict so callers holding the same reference also see updates.
        graph.clear()
        graph.update(rebuilt)
        return dict(rmeta["director_graph_ack"])

    @staticmethod
    def decide_capabilities(graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Inspect chat/vision backends and assign per-agent tools + rating modality."""
        from jiuwenswarm.server.runtime.designer.capabilities import decide_modality_plan

        plan = decide_modality_plan(graph)
        meta = dict(graph.get("metadata") or {})
        meta["director_capability_decision"] = {
            "global_rating_modality": plan.get("global_rating_modality"),
            "can_vision": plan.get("can_vision"),
            "can_video": plan.get("can_video"),
            "reason": plan.get("reason"),
            "vision_backend": plan.get("vision_backend"),
        }
        graph["metadata"] = meta
        return plan

    @staticmethod
    def review_leaf_media_prompt(
        graph: DesignerExecutionGraph,
        node: DesignerGraphNode,
    ) -> dict[str, Any]:
        """Gate every clip media prompt against locks + prior handoff / already_done."""
        from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
            collect_prior_clip_prompts,
        )
        from jiuwenswarm.server.runtime.designer.pipeline.character_consistency import (
            architecture_clause_from_bible,
            strip_prior_prompt_pastes,
        )

        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"clip", "character", "scene"}:
            return {"patched": False, "notes": "skip_non_media"}
        clip_index = int(cfg.get("clip_index") or 0)
        gen = dict(cfg.get("generate") or {})
        prompt = strip_prior_prompt_pastes(str(gen.get("prompt") or cfg.get("prompt") or "").strip())
        notes: list[str] = []
        changed = False
        if prompt != str(gen.get("prompt") or cfg.get("prompt") or "").strip():
            notes.append("strip_prior_prompt_pastes")
            changed = True
        # Lock clauses below are for the leaf agent; images persist only this text.
        visible_prompt = prompt

        setting_id = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        strategy = str(
            cfg.get("clip_strategy")
            or (cfg.get("identity_refs") or {}).get("clip_strategy")
            or ""
        ).strip()
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        costume_lock = str(cfg.get("costume_lock") or identity.get("costume_lock") or "").strip()
        spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
        if not spatial:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            spatial = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
        character_spec_ids = _str_ids(identity.get("character_node_ids") or cfg.get("character_node_ids") or [])
        prior_clip_id = str(
            cfg.get("prior_clip_node_id") or identity.get("prior_clip_node_id") or ""
        ).strip()


        if costume_lock and "Costume lock" not in prompt and "costume lock" not in prompt.lower():
            prompt = prompt + f"\nCostume lock (must keep): {costume_lock}"
            notes.append("inject_costume_lock")
            changed = True
        # Always reinforce garment-level clothing lock for frames + shots.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                clothing_lock_clause,
                ensure_cfg_clothing_lock,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            costume_lock = ensure_cfg_clothing_lock(cfg, characters=analysis_chars) or costume_lock
            cloth = clothing_lock_clause(
                costume_lock,
                for_clip=(role == "clip"),
            )
            if cloth and "CLOTHING LOCK" not in prompt and "CLOTHING HOLD" not in prompt:
                prompt = prompt + "\n" + cloth
                notes.append("inject_clothing_lock")
                changed = True
        except Exception:  # noqa: BLE001
            pass

        if spatial and "SPATIAL LOCK" not in prompt and role != "scene":
            prompt = (
                prompt
                + "\nSPATIAL LOCK: "
                + "; ".join(f"{k}={v}" for k, v in spatial.items() if str(v).strip())
            )
            notes.append("inject_spatial_lock")
            changed = True
            cfg["spatial_lock"] = spatial

        if character_spec_ids and "IDENTITY" not in prompt and "character spec" not in prompt.lower():
            prompt = prompt + f"\nIDENTITY character specs (do not invent faces): {', '.join(character_spec_ids)}"
            notes.append("inject_identity_character_specs")
            changed = True

        # Prior clip: short consistency note only (never paste full prior generate.prompt).
        prior_clip_action = str(cfg.get("previous_clip_action") or "").strip()
        prior_prompt = str(cfg.get("previous_clip_prompt") or "").strip()
        if not prior_clip_action and clip_index > 1:
            prev_id = prior_clip_id or f"n_clip_{clip_index - 1}"
            for n in graph.get("nodes") or []:
                if str(n.get("id") or "") != prev_id:
                    continue
                pcfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                prior_clip_action = str(
                    pcfg.get("clip_action")
                    or pcfg.get("character_action")
                    or ""
                ).strip()
                prior_prompt = str(
                    pcfg.get("last_approved_prompt")
                    or (pcfg.get("generate") or {}).get("prompt")
                    or ""
                ).strip()
                if prior_clip_action or prior_prompt:
                    if prior_clip_action:
                        cfg["previous_clip_action"] = prior_clip_action
                    # Readiness marker only; the gate note carries the prior beat.
                    if prior_prompt and not str(cfg.get("previous_clip_prompt") or "").strip():
                        cfg["previous_clip_prompt"] = prior_prompt
                    cfg["previous_clip_node_id"] = prev_id
                    notes.append("pull_prior_clip_action")
                    changed = True
                break
        has_prior = prior_clip_action or prior_prompt
        prior_noted = "PREVIOUS CLIP HAD" in prompt or "PRIOR CLIP PROMPT" in prompt
        if has_prior and not prior_noted:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
                clip_continuity_note,
            )

            prior_note = clip_continuity_note(
                clip_index=clip_index,
                this_action=str(cfg.get("clip_action") or cfg.get("character_action") or ""),
                this_camera=str(cfg.get("camera") or ""),
                prior_action=prior_clip_action or prior_prompt,
                prior_clip_index=clip_index - 1 if clip_index > 1 else None,
            )
            if prior_note:
                prompt = prompt + "\n\n" + prior_note
                notes.append("inject_prior_clip_note")
                changed = True

        already_done = [str(x) for x in (cfg.get("already_done") or []) if str(x)]
        if not already_done:
            analysis = (graph.get("metadata") or {}).get("script_analysis") or {}
            for s in analysis.get("clips") or []:
                if isinstance(s, dict) and int(s.get("clip_index") or 0) == clip_index:
                    already_done = [str(x) for x in (s.get("already_done") or []) if str(x)]
                    cfg["already_done"] = already_done
                    break
        if already_done:
            cfg["already_done"] = already_done

        occupancy = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
        if not occupancy and isinstance(identity.get("occupancy"), dict):
            occupancy = dict(identity["occupancy"])
        config_on_screen = _str_ids(
            cfg.get("on_screen") or occupancy.get("must_appear") or cfg.get("character_ids") or []
        )
        if occupancy and "OCCUPANCY:" not in prompt:
            prompt = (
                prompt
                + f"\nOCCUPANCY: must_appear={occupancy.get('must_appear') or config_on_screen}; "
                f"featured={occupancy.get('featured')}; "
                f"offscreen={occupancy.get('offscreen') or cfg.get('offscreen') or []}."
            )
            notes.append("inject_occupancy")
            changed = True
        # Reject/rewrite when prompt names cast outside config on_screen.
        if role in {"clip"} and config_on_screen:
            analysis_cast = (
                (graph.get("metadata") or {}).get("script_analysis") or {}
            )
            id_to_name = {
                str(c.get("id")): str(c.get("name") or c.get("id"))
                for c in (analysis_cast.get("characters") or [])
                if isinstance(c, dict) and c.get("id")
            }
            allowed_ids = set(config_on_screen)
            prompt_l = prompt.lower()
            leaked: list[str] = []
            for cid, name in id_to_name.items():
                if cid in allowed_ids:
                    continue
                nlow = name.lower()
                if len(nlow) < 3:
                    continue
                if nlow in prompt_l or cid.lower() in prompt_l:
                    leaked.append(name)
            if leaked:
                prompt = (
                    prompt
                    + "\nOCCUPANCY ENFORCE: draw ONLY "
                    + ", ".join(id_to_name.get(c, c) for c in config_on_screen)
                    + f". Do NOT draw or mention: {', '.join(leaked)}."
                )
                notes.append(f"reject_offscreen_in_prompt:{','.join(leaked[:6])}")
                changed = True
            # Keep character_spec_ids aligned to on_screen only.
            spec_by_cid = {
                str(c): sid for c, sid in zip(cfg.get("character_ids") or [], character_spec_ids) if str(c)
            }
            # Prefer resolving from graph character spec nodes when available.
            for other in graph.get("nodes") or []:
                if not isinstance(other, dict):
                    continue
                oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                if _role_key(other) not in {"character"}:
                    continue
                if oc.get("combined_cast"):
                    continue
                cids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                if len(cids) == 1:
                    spec_by_cid[cids[0]] = str(other.get("id") or "")
            aligned = [spec_by_cid[c] for c in config_on_screen if c in spec_by_cid]
            if aligned and aligned != character_spec_ids:
                character_spec_ids = aligned
                cfg["character_node_ids"] = list(aligned)
                if isinstance(identity, dict):
                    identity = dict(identity)
                    identity["character_node_ids"] = list(aligned)
                    identity["character_ids"] = list(config_on_screen)
                    cfg["identity_refs"] = identity
                notes.append("align_refs_to_on_screen")
                changed = True

        crowd = cfg.get("crowd_lock") if isinstance(cfg.get("crowd_lock"), dict) else {}
        if not crowd and isinstance(occupancy.get("crowd_lock"), dict):
            crowd = occupancy["crowd_lock"]
        if not crowd:
            meta0 = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            analysis0 = (
                meta0.get("script_analysis")
                if isinstance(meta0.get("script_analysis"), dict)
                else {}
            )
            for s in analysis0.get("clips") or []:
                if isinstance(s, dict) and int(s.get("clip_index") or 0) == clip_index:
                    crowd = s.get("crowd_lock") if isinstance(s.get("crowd_lock"), dict) else {}
                    if crowd:
                        cfg["crowd_lock"] = crowd
                    break
        if crowd and role != "clip" and "CROWD LOCK" not in prompt:
            prompt = (
                prompt
                + f"\nCROWD LOCK: present={crowd.get('present')}; "
                f"density={crowd.get('density')}. {str(crowd.get('rule') or '')[:280]}"
            )
            notes.append("inject_crowd_lock")
            changed = True
        elif role == "clip" and crowd:
            # Shots: stamp structured crowd_state for story-form coverage (no LOCK banner).
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
                    infer_crowd_state,
                    stamp_continuity_story_fields,
                )

                cfg["crowd_lock"] = crowd
                cfg = stamp_continuity_story_fields(
                    cfg,
                    events=list(cfg.get("previous_clip_finished_events") or []),
                    prior_text=str(cfg.get("previous_clip_wan_prompt") or ""),
                )
                if not isinstance(cfg.get("crowd_state"), dict):
                    cfg["crowd_state"] = infer_crowd_state(cfg, events=[])
                notes.append("stamp_crowd_state")
                changed = True
            except Exception:  # noqa: BLE001
                pass

        detail_needles = ("gaze", "screen-left", "screen left", "motion direction", "looks at")
        if not any(n in prompt.lower() for n in detail_needles):
            prompt = (
                prompt
                + "\nDETAIL LOCK: specify motion direction, who each person looks at, "
                "relative screen L/R positioning, and prop/landmark anchors for this shot."
            )
            notes.append("inject_detail_lock")
            changed = True

        # Per-shot staging locks (positioning / action / relationships) — equal to clothing.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_staging_lock import (
                ensure_cfg_staging_locks,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            analysis_clips = ((graph.get("metadata") or {}).get("script_analysis") or {}).get("clips") or []
            shot_row = next(
                (s for s in analysis_clips if isinstance(s, dict) and int(s.get("clip_index") or 0) == clip_index),
                None,
            )
            staging_clause = ensure_cfg_staging_locks(
                cfg,
                shot=shot_row if isinstance(shot_row, dict) else None,
                characters=analysis_chars,
            )
            if staging_clause and "STAGING LOCK" not in prompt:
                prompt = prompt + "\n" + staging_clause
                notes.append("inject_staging_lock")
                changed = True
        except Exception:  # noqa: BLE001
            pass

        # Film-wide style lock (Director enforces; leaves cannot drop).
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
        style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
        if not style:
            style = meta.get("style_lock") if isinstance(meta.get("style_lock"), dict) else {}
        if not style:
            style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}

        if style:
            cfg["style_lock"] = style
            # Hard STYLE LOCK always — soft STYLE HOLD must not block film-wide lock.
            if "STYLE LOCK" not in prompt:
                try:
                    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
                        style_lock_clause,
                    )

                    clause = (style_lock_clause(style) or "").strip()
                except Exception:  # noqa: BLE001
                    clause = ""
                if not clause:
                    look = str(style.get("look") or style.get("medium") or "").strip()
                    clause = f"STYLE LOCK (film-wide): {look}" if look else ""
                if not clause:
                    try:
                        from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
                            ensure_style_lock,
                        )

                        style = ensure_style_lock(
                            style,
                            prompt=str(graph.get("description") or meta.get("user_prompt") or ""),
                        )
                        cfg["style_lock"] = style
                        look = str(style.get("look") or style.get("medium") or "").strip()
                        clause = f"STYLE LOCK (film-wide): {look}" if look else ""
                    except Exception:  # noqa: BLE001
                        clause = (
                            "STYLE LOCK (film-wide): match the visual medium specified "
                            "by the brief and storyboard."
                        )
                if clause:
                    prompt = prompt + "\n" + clause
                    notes.append("inject_style_lock")
                    changed = True
        # Leaf nodes never invent a visual medium. The brief/storyboard style is
        # propagated through analysis/metadata above; a missing style stays missing
        # here instead of silently becoming a model-specific default.

        if role == "clip":
            # Scene specs + setting isolation for shots (same locks as other clips).
            specs = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
            if not specs:
                meta_b = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
                locks_b = meta_b.get("scene_locks") if isinstance(meta_b.get("scene_locks"), dict) else {}
                if isinstance(locks_b.get(setting_id), dict):
                    specs = dict(locks_b[setting_id])
                    cfg["scene_specs"] = specs
            if specs and "SCENE SPECS" not in prompt:
                tod_b = (
                    cfg.get("time_of_day_lock")
                    if isinstance(cfg.get("time_of_day_lock"), dict)
                    else {}
                )
                tod_bit = ""
                if tod_b.get("time_of_day") and str(tod_b.get("time_of_day")) != "unspecified":
                    tod_bit = (
                        f"; time_of_day={tod_b.get('time_of_day')}; "
                        f"lighting={tod_b.get('lighting') or specs.get('lighting')}"
                    )
                prompt = (
                    prompt
                    + f"\nSCENE SPECS: scene={specs.get('scene_name') or specs.get('place')}; "
                    f"lighting={specs.get('lighting')}; "
                    f"objects={', '.join(str(x) for x in (specs.get('objects') or [])[:6])}; "
                    f"crowd={specs.get('crowd')}; coherence={specs.get('coherence_rule')}"
                    f"{tod_bit}."
                )
                notes.append("enforce_scene_specs_shot")
                changed = True
            if setting_id and f"setting={setting_id}" not in prompt.lower() and "Setting lock" not in prompt:
                prompt = (
                    prompt
                    + f"\nSETTING LOCK: animate only `{setting_id}` from THIS shot's scene spec; "
                    "do not import architecture or cast from another scene."
                )
                notes.append("enforce_setting_lock_shot")
                changed = True
            # Storyboard start/end owns consistency — do not pull prior Wan into cfg.
            if isinstance(cfg.get("start_state"), dict) and cfg.get("start_state"):
                cfg.pop("continuity_clip_node_id", None)
            else:
                cont_shot = str(cfg.get("continuity_clip_node_id") or cfg.get("previous_clip_node_id") or "").strip()
                if cont_shot:
                    try:
                        from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
                            ensure_prior_clip_story_on_cfg,
                        )

                        cfg = ensure_prior_clip_story_on_cfg(cfg, graph if isinstance(graph, dict) else {})
                    except Exception:  # noqa: BLE001
                        pass
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                    scrub_restated_speech,
                )

                cfg = scrub_restated_speech(cfg)
            except Exception:  # noqa: BLE001
                pass
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
                    contact_anti_penetration_clause,
                )

                if "ANTI-PENETRATION" not in prompt and "CONTACT / ANTI-PENETRATION" not in prompt:
                    prompt = prompt + "\n" + contact_anti_penetration_clause(for_clip=True)
                    notes.append("inject_contact_anti_penetration")
                    changed = True
            except Exception:  # noqa: BLE001
                pass
            # Character consistency / costume still injected below for the leaf;
            # the video API body is rewritten by director_prepare_video_prompt.
            prior_shots = collect_prior_clip_prompts(graph, clip_index=clip_index)
            if not prior_shots and str(cfg.get("previous_clip_action") or "").strip():
                prior_shots = [
                    {
                        "node_id": str(cfg.get("previous_clip_node_id") or ""),
                        "clip_index": int(
                            cfg.get("previous_clip_clip_index") or max(1, clip_index - 1)
                        ),
                        "clip_action": str(cfg.get("previous_clip_action") or ""),
                        "speech_line": str(cfg.get("previous_clip_speech") or ""),
                    }
                ]
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.shot_consistency_contract import (
                    apply_consistency_contract,
                    prompt_violates_consistency,
                )

                cfg, prompt, cont_notes = apply_consistency_contract(
                    cfg,
                    graph=graph if isinstance(graph, dict) else {},
                    prompt=prompt,
                )
                for n in cont_notes:
                    if n and n not in notes:
                        notes.append(n)
                        changed = True
                # Hard reject: blank contaminated draft so director rewrite uses
                # this storyboard row + structured holds (not prior Wan prose).
                if prompt_violates_consistency(prompt, cfg=cfg):
                    prompt = ""
                    notes.append("reject_continuity_violation_blank_prompt")
                    changed = True
            except Exception:  # noqa: BLE001
                pass
            # Keep prior_shots available for debug / regenerate packets only.
            if prior_shots:
                cfg["prior_storyboard_shots"] = [
                    {
                        "clip_index": p.get("clip_index"),
                        "clip_action": p.get("clip_action"),
                        "speech_line": p.get("speech_line"),
                        "camera": p.get("camera"),
                    }
                    for p in prior_shots
                    if isinstance(p, dict)
                ][-6:]
            if "CONSISTENCY STATE" not in prompt:
                try:
                    from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
                        positive_continuity_clause,
                    )

                    positive = positive_continuity_clause(cfg)
                    if positive:
                        prompt = prompt + "\n\n" + positive
                        notes.append("inject_positive_continuity")
                        changed = True
                except Exception:  # noqa: BLE001
                    pass
            # Character consistency on every shot call (Director enforce).
            cast_who = ", ".join(
                _str_ids(
                    cfg.get("on_screen")
                    or (cfg.get("occupancy") or {}).get("must_appear")
                    or cfg.get("character_ids")
                    or []
                )
            )
            if "CHARACTER CONSISTENCY" not in prompt:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_reference_binding import (
                    clip_uses_scene_specs,
                )

                if clip_uses_scene_specs(cfg, graph if isinstance(graph, dict) else {}):
                    prompt = (
                        prompt
                        + "\nCHARACTER CONSISTENCY LOCK: animate ONLY on-screen people from "
                        "character specs bound as character1/character2… (attach order). "
                        "The LAST reference is the Scene specs (environment) — not a person. "
                        f"Keep faces, body types, ages, and costumes"
                        f"{(' for ' + cast_who) if cast_who else ''}. "
                        "Do not recast, redesign wardrobe, or invent unlabeled extras."
                    )
                else:
                    prompt = (
                        prompt
                        + "\nCHARACTER CONSISTENCY LOCK: animate ONLY people already in Image 1 "
                        "(this shot's scene spec); keep the same faces, body types, ages, and "
                        f"costumes{(' for ' + cast_who) if cast_who else ''}. "
                        "Do not recast, redesign wardrobe, or invent a different hero. "
                        "IDENTITY character specs remain the face authority."
                    )
                notes.append("inject_clip_character_consistency")
                changed = True
            if costume_lock and "Costume lock" not in prompt and "costume lock" not in prompt.lower():
                prompt = prompt + f"\nCostume lock (must keep on shot): {costume_lock}"
                notes.append("inject_shot_costume_lock")
                changed = True
            if costume_lock and "CLOTHING LOCK" not in prompt:
                from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                    clothing_lock_clause,
                )

                cloth = clothing_lock_clause(costume_lock, for_clip=True)
                if cloth:
                    prompt = prompt + "\n" + cloth
                    notes.append("inject_shot_clothing_lock")
                    changed = True
            # Storyboard shot for THIS shot only (avoid full-board mix).
            action = str(cfg.get("clip_action") or "").strip()
            if action and f"Primary action for shot {clip_index}" not in prompt:
                prompt = prompt + f"\nPrimary action for shot {clip_index}: {action}"
                notes.append("inject_this_clip_action")
                changed = True

            # Language / speech / BGM locks — Director enforces on every shot.
            from jiuwenswarm.server.runtime.designer.audio_locks import (
                audio_lock_prompt_block,
                resolve_audio_intent_flags,
                stamp_audio_fields_on_clip_config,
            )

            analysis_a = (
                meta.get("script_analysis")
                if isinstance(meta.get("script_analysis"), dict)
                else {}
            )
            shot_row = next(
                (
                    s
                    for s in (analysis_a.get("clips") or [])
                    if isinstance(s, dict) and int(s.get("clip_index") or 0) == clip_index
                ),
                {},
            )
            routing = meta.get("audio_routing") if isinstance(meta.get("audio_routing"), dict) else {}
            cfg = stamp_audio_fields_on_clip_config(
                cfg,
                shot=shot_row if isinstance(shot_row, dict) else {},
                analysis=analysis_a,
                meta=meta,
                clip_embedded=bool(
                    routing.get("clip_embedded")
                    or cfg.get("clip_embedded_audio")
                    or meta.get("prefer_wan3_clip_audio")
                ),
            )
            # stamp_audio re-copies storyboard speech — re-enforce uniqueness after.
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.shot_consistency_contract import (
                    enforce_speech_uniqueness,
                )

                cfg, speech_notes = enforce_speech_uniqueness(
                    cfg, graph=graph if isinstance(graph, dict) else {}
                )
                for n in speech_notes:
                    if n and n not in notes:
                        notes.append(n)
                        changed = True
            except Exception:  # noqa: BLE001
                pass
            flags = resolve_audio_intent_flags(meta, cfg)
            speech_line_now = str(cfg.get("speech_line") or flags.get("speech_line") or "").strip()
            by_char_now = (
                cfg.get("speech_by_character")
                if isinstance(cfg.get("speech_by_character"), dict)
                else {}
            )
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
            missing_lock = any(lock not in prompt for lock in ("LANGUAGE LOCK", "SPEECH LOCK", "BGM LOCK"))
            if block and missing_lock:
                # Replace soft fragments with full lock block once.
                if "LANGUAGE LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("inject_audio_locks")
                    changed = True
                elif "SPEECH LOCK" not in prompt or "BGM LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("reinforce_audio_locks")
                    changed = True

        # Soft anti-repeat for images only. Shot video calls stay positive — director
        # omits exited cast instead of appending forbid lines.
        if role != "clip":
            for item in already_done:
                low = item.lower()
                if "exited" in low or "leaving" in low or "walked out" in low:
                    if "do not show them leaving again" not in prompt.lower():
                        prompt = (
                            prompt
                            + "\nDo not show characters leaving again if already_done says they exited."
                        )
                        notes.append("enforce_no_repeat_exit")
                        changed = True
                        break

        if role == "clip":
            from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                ensure_story_lock_coverage,
                director_approve_video_prompt,
            )

            # Stamp film-wide / shot ToD onto cfg before story-form coverage.
            if not isinstance(cfg.get("time_of_day_lock"), dict) or not (
                cfg.get("time_of_day_lock") or {}
            ).get("time_of_day"):
                meta_tod = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
                analysis_tod = (
                    meta_tod.get("script_analysis")
                    if isinstance(meta_tod.get("script_analysis"), dict)
                    else {}
                )
                try:
                    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                        infer_time_of_day_lock,
                    )

                    scene_specs = cfg.get("scene_specs") or {}
                    tod = infer_time_of_day_lock(
                        str(graph.get("description") or meta_tod.get("user_prompt") or ""),
                        str(
                            scene_specs.get("scene_name")
                            or scene_specs.get("place")
                            or cfg.get("clip_action")
                            or ""
                        ),
                    )
                    shot_tod = _shot_time_of_day(analysis_tod.get("clips"), clip_index)
                    if isinstance(shot_tod, dict):
                        tod = {**tod, **{k: v for k, v in shot_tod.items() if str(v).strip()}}
                    elif isinstance(analysis_tod.get("time_of_day_lock"), dict):
                        tod = {
                            **tod,
                            **{
                                k: v
                                for k, v in analysis_tod["time_of_day_lock"].items()
                                if str(v).strip()
                            },
                        }
                    specs = (
                        cfg.get("scene_specs")
                        if isinstance(cfg.get("scene_specs"), dict)
                        else {}
                    )
                    if specs.get("time_of_day") and (
                        not tod.get("time_of_day") or tod.get("time_of_day") == "unspecified"
                    ):
                        tod["time_of_day"] = str(specs.get("time_of_day"))
                    if specs.get("lighting") and not tod.get("lighting"):
                        tod["lighting"] = str(specs.get("lighting"))
                    if tod:
                        cfg["time_of_day_lock"] = tod
                except Exception:  # noqa: BLE001
                    pass

            approved, reasons = director_approve_video_prompt(
                prompt,
                cfg=cfg,
                graph=graph if isinstance(graph, dict) else {},
                clip_index=clip_index,
                action=str(cfg.get("clip_action") or ""),
                camera=str(cfg.get("camera") or ""),
            )
            if approved != str(prompt or "").strip():
                notes.append("director_rewrote_video_prompt")
                prompt = approved
                changed = True
            cfg["director_video_prompt_notes"] = reasons
            cfg["director_video_prompt_notes"] = reasons
            cfg["director_video_prompt_approved"] = True
            # After story-form rewrite: fill missing language / ToD / style as prose
            # (never LOCK essays — those get scrubbed on the next pass).
            covered, cov_notes = ensure_story_lock_coverage(
                prompt,
                cfg,
                graph=graph if isinstance(graph, dict) else {},
            )
            if covered != str(prompt or "").strip():
                prompt = covered
                notes.extend(cov_notes or ["ensure_story_lock_coverage"])
                changed = True

        # Always stamp Director lock gate; patched=True when prompt text changed.
        # Images + clips: positive practice prompts only (no LOCK essays on tool body).
        if role in {"scene", "character"}:
            meta_l = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            analysis_l = (
                meta_l.get("script_analysis")
                if isinstance(meta_l.get("script_analysis"), dict)
                else {}
            )
            tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
            if not tod:
                shot_tod = _shot_time_of_day(analysis_l.get("clips"), clip_index)
                tod = shot_tod if isinstance(shot_tod, dict) else (
                    analysis_l.get("time_of_day_lock")
                    if isinstance(analysis_l.get("time_of_day_lock"), dict)
                    else {}
                )
            if not tod:
                try:
                    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                        infer_time_of_day_lock,
                    )

                    scene_specs = cfg.get("scene_specs") or {}
                    tod = infer_time_of_day_lock(
                        str(graph.get("description") or meta_l.get("user_prompt") or ""),
                        str(
                            scene_specs.get("scene_name")
                            or scene_specs.get("place")
                            or cfg.get("clip_action")
                            or ""
                        ),
                    )
                except Exception:  # noqa: BLE001
                    tod = {}
            if tod:
                cfg["time_of_day_lock"] = tod
            style_l = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
            if not style_l:
                style_l = meta_l.get("style_lock") if isinstance(meta_l.get("style_lock"), dict) else {}
            if style_l and not isinstance(cfg.get("style_lock"), dict):
                cfg["style_lock"] = style_l
            # Image: user toolbar text wins; otherwise keep the LLM prompt.
            # No hard-coded ensure_still rewrite — the leaf LLM authors the body.
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
                    resolve_user_origin_prompt,
                )

                user_still = resolve_user_origin_prompt(cfg, "")
                if user_still and user_still != str(prompt or "").strip():
                    prompt = user_still
                    visible_prompt = user_still
                    notes.append("image_user_authority")
                    changed = True
                    cfg["director_image_prompt_notes"] = ["image_user_authority"]
                else:
                    cfg.setdefault(
                        "director_image_prompt_notes",
                        ["image_llm_authority"],
                    )
                cfg["director_image_prompt_approved"] = True
            except Exception:  # noqa: BLE001
                pass

        # Stamp configured backend prompt budgets onto the leaf (advisory only — never truncates).
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
                media_prompt_limit_packet,
                resolve_prompt_limit,
            )

            packet = media_prompt_limit_packet()
            cfg["media_prompt_limits"] = packet
            kind = "video" if role == "clip" else "image"
            lim = resolve_prompt_limit(kind)
            cfg["prompt_char_limit"] = lim.max_chars
            cfg["prompt_limit_guidance"] = lim.guidance()
            if lim.max_chars and len(prompt) > lim.max_chars:
                notes.append(
                    f"prompt_over_limit:{len(prompt)}>{lim.max_chars}"
                )
        except Exception:  # noqa: BLE001
            packet = {}
            lim = None

        cfg["director_prompt_reviewed"] = True
        cfg["director_lock_gate"] = {
            "setting_id": setting_id,
            "clip_strategy": strategy or cfg.get("clip_strategy"),
            "costume_lock": bool(costume_lock),
            "spatial_lock": bool(spatial),
            "style_lock": bool(style),
            "character_spec_ids": character_spec_ids,
            "prior_clip_node_id": prior_clip_id or None,
            "language_lock": cfg.get("language_lock") or meta.get("language_lock"),
            "speech_lock": bool(cfg.get("speech_line") or cfg.get("speech_by_character")),
            "bgm_lock": bool(cfg.get("bgm_lock") or meta.get("bgm_lock")),
            "clip_embedded_audio": bool(cfg.get("clip_embedded_audio")),
            "video_audio": bool(cfg.get("video_audio") or cfg.get("prefer_wan3_clip_audio")),
            "time_of_day_lock": (
                (cfg.get("time_of_day_lock") or {}).get("time_of_day")
                if isinstance(cfg.get("time_of_day_lock"), dict)
                else None
            ),
            "story_lock_coverage": True,
            "prompt_char_limit": cfg.get("prompt_char_limit"),
            "prompt_limit_known": bool(getattr(lim, "known", False)),
            "prompt_limit_source": str(getattr(lim, "source", "") or ""),
            "media_prompt_limits": packet or cfg.get("media_prompt_limits"),
        }
        persisted = prompt.strip()
        lock_notes = ""
        if role != "clip":
            visible = visible_prompt.strip()
            if persisted.startswith(visible):
                lock_notes = persisted[len(visible):].strip()
                persisted = visible
        if lock_notes:
            cfg["director_lock_notes"] = lock_notes
        else:
            cfg.pop("director_lock_notes", None)
        gen["prompt"] = persisted
        cfg["generate"] = gen
        # Character/scene leaves read cfg.prompt; keep both in sync after Director gate.
        if role in {"character", "scene"} or not str(cfg.get("prompt") or "").strip():
            cfg["prompt"] = persisted
        if changed:
            cfg["last_approved_prompt"] = persisted
        else:
            cfg.setdefault("last_approved_prompt", persisted)
        node["config"] = cfg
        nid = str(node.get("id") or "")
        for n in graph.get("nodes") or []:
            if str(n.get("id") or "") == nid:
                n["config"] = cfg
                break
        return {
            "patched": changed,
            "notes": notes,
            "clip_index": clip_index,
            "lock_gate": cfg.get("director_lock_gate"),
        }

    def validate_plan_structure(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        meta = dict(graph.get("metadata") or {})
        modality = meta.get("modality_plan")
        if not isinstance(modality, dict) or not modality.get("global_rating_modality"):
            modality = self.decide_capabilities(graph)
            meta = dict(graph.get("metadata") or {})
        cast_notes = _cast_focus_alignment_patch(graph)
        notes = _shot_distinctness_patch(graph)
        consistency_notes = _spatial_continuity_patch(graph)
        identity_notes = _identity_consistency_patch(graph)
        spatial_notes = _spatial_geography_lock_patch(graph)
        # Film-wide style/axis locks on every image + clip node.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                apply_axis_locks_to_graph,
            )

            axis_notes = apply_axis_locks_to_graph(graph)
        except Exception:  # noqa: BLE001
            axis_notes = []
        # User-added canvas nodes → LLM agents + tools + contribution wiring.
        try:
            user_onboard = self.onboard_user_added_nodes(graph)
            axis_notes.extend(
                [f"user:{x}" for x in (user_onboard.get("notes") or []) if isinstance(x, str)][:20]
            )
        except Exception:  # noqa: BLE001
            pass
        # Prune unused nodes + keep graph coherent for compose; stamp agents.
        prune_notes = _director_prune_and_cohere(graph)
        agent_notes = self._enforce_leaf_agents(graph)
        # Re-apply audio agent policy after modality (backends may clear force_handler).
        audio_assign = assign_audio_node_agents(graph)
        # Second prune after audio assign (speech/music may be omitted).
        prune_notes.extend(_director_prune_and_cohere(graph))
        ensure_notes = self.ensure_agents_and_prune(graph)
        rating_mod = str(
            (modality or {}).get("global_rating_modality")
            or meta.get("rating_modality")
            or "text_only"
        )
        ack = {
            "ok": True,
            "patched": notes
            + cast_notes
            + consistency_notes
            + identity_notes
            + spatial_notes
            + axis_notes
            + prune_notes
            + agent_notes
            + list(ensure_notes.get("notes") or []),
            "cast_focus_fixes": cast_notes,
            "consistency_fixes": consistency_notes,
            "identity_fixes": identity_notes,
            "spatial_lock_fixes": spatial_notes,
            "axis_lock_fixes": axis_notes,
            "pruned_nodes": list(
                dict.fromkeys(
                    [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
                    + list(ensure_notes.get("pruned") or [])
                )
            ),
            "agent_enforcement": agent_notes,
            "ensure_agents": ensure_notes,
            "audio_assignment": audio_assign,
            "rating_modality": rating_mod,
            "can_vision": bool((modality or {}).get("can_vision")),
            "can_video": bool((modality or {}).get("can_video")),
            "can_speech": bool((modality or {}).get("can_speech")),
            "can_music": bool((modality or {}).get("can_music")),
            "notes": (
                f"Director start validation: rating_modality={rating_mod}. "
                f"Cast-focus={len(cast_notes)}. Consistency={len(consistency_notes)}. "
                f"Identity={len(identity_notes)}. Spatial={len(spatial_notes)}. "
                f"Prune/cohere={len(prune_notes)}. Agents={len(agent_notes)}. "
                f"Ensure={len(ensure_notes.get('notes') or [])}. "
                f"{str((modality or {}).get('reason') or '')[:400]}"
            ),
            "source": "structural",
        }
        meta = dict(graph.get("metadata") or {})
        meta["director_plan_ack"] = ack
        graph["metadata"] = meta
        return ack

    @staticmethod
    def ensure_agents_and_prune(graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Stamp creative nodes as agents; force_handler uploads stay handlers.

        Chat credentials are gated at Enter/chat/Play. This only stamps topology —
        missing credentials surface on the first ``call_model_tool``.
        """
        from jiuwenswarm.server.runtime.designer.smart_graph import prune_non_contributing_nodes

        notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            if not nid:
                continue
            cfg["kind"] = "agent"
            tools = _tools_for_node(node)
            if list(cfg.get("tools") or []) != tools:
                notes.append(f"tools:{nid}")
            cfg["tools"] = tools
            if cfg.get("force_handler"):
                cfg["delegate"] = "handler"
                cfg["skip_llm"] = True
                notes.append(f"force_handler:{nid}")
            else:
                cfg.pop("force_handler", None)
                cfg.pop("skip_llm", None)
                cfg["delegate"] = "agent"
            node["config"] = cfg

        pruned = prune_non_contributing_nodes(graph)
        notes.extend([f"pruned:{x}" for x in pruned])

        # Ensure compose is a sink: every shot (and audio) edge into compose when present.
        ids = {str(n.get("id") or "") for n in (graph.get("nodes") or []) if isinstance(n, dict)}
        if "n_compose" in ids:
            edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
            existing = {
                (str(e.get("source") or ""), str(e.get("target") or "")) for e in edges
            }
            for node in graph.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                nid = str(node.get("id") or "")
                cfg_node = node.get("config") if isinstance(node.get("config"), dict) else {}
                if cfg_node.get("user_added"):
                    continue
                role = _role_key(node)
                if role in {"clip", "speech", "music"} or nid.startswith("n_clip"):
                    key = (nid, "n_compose")
                    if key not in existing and nid != "n_compose":
                        edges.append(
                            {
                                "id": f"e_{nid}_compose",
                                "source": nid,
                                "target": "n_compose",
                                "kind": "data",
                            }
                        )
                        existing.add(key)
                        notes.append(f"compose_sink:{nid}")
            graph["edges"] = edges
            # Compose inputs list stays in sync.
            for node in graph.get("nodes") or []:
                if str(node.get("id") or "") != "n_compose":
                    continue
                cfg = dict(node.get("config") or {})
                inputs = [
                    str(e.get("source") or "")
                    for e in (graph.get("edges") or [])
                    if str(e.get("target") or "") == "n_compose"
                ]
                cfg["inputs"] = [x for x in inputs if x]
                node["config"] = cfg
                break

        result = {
            "ok": True,
            "notes": notes[:60],
            "pruned": list(pruned),
        }
        meta = dict(graph.get("metadata") or {})
        meta["director_ensure_agents"] = result
        graph["metadata"] = meta
        return result

    def audit_contribution_for_run(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Warn (do not block) when user-added nodes never reach the final compose/shot."""
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            find_non_contributing_node_ids,
        )

        # Re-onboard + soft-wire before auditing so Run sees latest Director decisions.
        try:
            self.onboard_user_added_nodes(graph)
        except Exception:  # noqa: BLE001
            logger.debug("user node onboard during run audit failed", exc_info=True)
        orphans = []
        for nid in find_non_contributing_node_ids(graph):
            for n in graph.get("nodes") or []:
                if not isinstance(n, dict) or str(n.get("id") or "") != nid:
                    continue
                cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                if cfg.get("user_added"):
                    orphans.append(nid)
                break
        warning = ""
        if orphans:
            warning = (
                "Warning: user-added nodes do not contribute to the final shot/compose ("
                + ", ".join(orphans)
                + "). Connect them into the pipeline if you want them in the film. "
                "Running anyway."
            )
        meta = dict(graph.get("metadata") or {})
        if orphans:
            meta["non_contributing_user_nodes"] = orphans
            meta["contribution_warning"] = warning
        else:
            meta.pop("non_contributing_user_nodes", None)
            meta.pop("contribution_warning", None)
        meta["director_contribution_audit"] = {
            "ok": True,
            "orphans": orphans,
            "warning": warning,
            "blocked": False,
        }
        graph["metadata"] = meta
        return dict(meta["director_contribution_audit"])

    @staticmethod
    def _enforce_leaf_agents(graph: DesignerExecutionGraph) -> list[str]:
        """Every leaf is an LLM agent with tools (uploads stay force_handler)."""
        notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            if not nid:
                continue
            cfg["kind"] = "agent"
            tools = _tools_for_node(node)
            if list(cfg.get("tools") or []) != tools:
                notes.append(f"tools:{nid}")
            cfg["tools"] = tools
            if cfg.get("force_handler"):
                cfg["delegate"] = "handler"
                cfg["skip_llm"] = True
                notes.append(f"force_handler:{nid}")
            else:
                cfg.pop("force_handler", None)
                cfg.pop("skip_llm", None)
                cfg["delegate"] = "agent"
            try:
                role_key = _role_key(node)
                if role_key in {"clip", "video", "scene", "character"}:
                    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
                        media_prompt_limit_packet,
                        resolve_prompt_limit,
                    )

                    kind = "video" if role_key in {"clip", "video"} else "image"
                    lim = resolve_prompt_limit(kind)
                    cfg["prompt_char_limit"] = lim.max_chars
                    cfg["prompt_limit_guidance"] = lim.guidance()
                    cfg.setdefault("media_prompt_limits", media_prompt_limit_packet())
            except Exception:  # noqa: BLE001
                pass
            node["config"] = cfg
        return notes

    async def review_brief(
        self, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        """One-pass LLM fidelity check of approved brief vs user prompt; patch if needed."""
        meta = dict(graph.get("metadata") or {})
        user_prompt = str(graph.get("description") or "")
        brief = str(meta.get("approved_brief") or "")
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        ack: dict[str, Any] = {
            "ok": True,
            "source": "llm",
            "notes": "Brief fidelity pass.",
            "patched": [],
        }
        patched: list[str] = []
        # Structural pre-check: ensure each character name appears in the brief.
        missing: list[str] = []
        low = brief.lower()
        for c in characters:
            name = str(c.get("name") or "").strip()
            if name and name.lower() not in low:
                missing.append(name)
        if missing:
            extra = "\n".join(f"- **{n}:** must appear with identity lock" for n in missing)
            brief = (brief.rstrip() + "\n\n**Director cast fidelity:**\n" + extra + "\n")[
                :12000
            ]
            patched.append("cast_names")
        # Heuristic: mention multi-view / shot coverage when prompt is long.
        if len(user_prompt) > 120 and "shot" not in low and "view" not in low:
            brief = (
                brief.rstrip()
                + "\n\n**Shot views:** cover establishing, mid, reaction close-ups "
                "for every major prompt beat.\n"
            )[:12000]
            patched.append("shot_views")

        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
        )

        try:
            system = (
                "You are the Designer Director. Review the creative brief once for fidelity "
                "to explicit user facts and constraints. The authored brief is allowed to "
                "creatively fill details that a sparse request left unspecified. Preserve its "
                "creative concept, narrative/content arc, timed shot plan, and script/speech "
                "plan; do not remove an enriched shot merely because it was not stated verbatim "
                "in the user prompt. Flag missing characters, insufficient shot views, or content "
                "that contradicts explicit user facts."
                "Preserve the user's visual style and the existing Visual Style section exactly. "
                "Patch only to repair those issues; do not "
                "introduce a conflicting or unrelated plot, cast, claim, or geography. "
                "Respond JSON only: "
                '{"ok":true,"patched_brief_markdown":"...","notes":"...","issues":["..."]}'
            ) + _language_rule()
            parsed = await _model_json_object(
                phase="brief review",
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "brief": brief[:12000],
                        "characters": characters,
                        "clips": analysis.get("clips"),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                max_tokens=32768,
            )
            if parsed is None:
                ack["review_skipped"] = "invalid_json"
                parsed = {"notes": "Brief review reply was not valid JSON; kept the authored brief."}
            patched_md = str(parsed.get("patched_brief_markdown") or "").strip()
            if patched_md and len(patched_md) > 80:
                brief = patched_md[:12000]
                patched.append("llm_brief")
                ack["source"] = "llm"
            ack["notes"] = str(parsed.get("notes") or ack["notes"])[:1000]
            ack["issues"] = list(parsed.get("issues") or [])[:20]
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director review_brief LLM failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while reviewing the brief: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            ensure_visual_style_statement,
            synchronize_graph_style_from_brief,
        )

        brief = ensure_visual_style_statement(
            brief,
            analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {},
        )
        meta["approved_brief"] = brief
        graph["metadata"] = meta
        synchronize_graph_style_from_brief(graph, brief)
        meta = dict(graph.get("metadata") or {})
        ack["patched"] = patched[:20]
        meta["director_brief_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def review_storyboard(
        self, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        """Pre-run one-pass storyboard fidelity + enhancements (crowd, beauty, duration, consistency)."""
        meta = dict(graph.get("metadata") or {})
        if meta.get("storyboard_pre_reviewed"):
            return dict(meta.get("director_storyboard_pre_ack") or {"ok": True, "skipped": True})
        # Clear mid-run once-flag so review_storyboard_once applies patches now.
        meta.pop("storyboard_reviewed", None)
        graph["metadata"] = meta
        ack = await self.review_storyboard_once(
            graph, node_states=None
        )
        meta = dict(graph.get("metadata") or {})
        meta["storyboard_pre_reviewed"] = True
        meta["director_storyboard_pre_ack"] = ack
        # Allow a second pass after the storyboard leaf completes during the ready-queue.
        meta["storyboard_reviewed"] = False
        graph["metadata"] = meta
        return ack

    async def review_storyboard_once(
        self,
        graph: DesignerExecutionGraph,
        *, node_states: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """After storyboard completes: fidelity + enhancement + consistency (one shot, no loop)."""
        meta = dict(graph.get("metadata") or {})
        if meta.get("storyboard_reviewed"):
            return dict(meta.get("director_storyboard_ack") or {"ok": True, "skipped": True})
        analysis = dict(meta.get("script_analysis") or {}) if isinstance(meta.get("script_analysis"), dict) else {}
        shots = list(analysis.get("clips") or [])
        user_prompt = str(graph.get("description") or "")
        ack: dict[str, Any] = {
            "ok": True,
            "source": "llm",
            "notes": "Storyboard continuity + duration pass.",
            "patched": [],
        }
        # Structural continuity: mark leave/stand forbids on later shots when earlier action implies it.
        leave_markers = ("leave", "leaves", "stood", "stands up", "gets up", "rising")
        left_chars: list[str] = []
        for shot in shots:
            action = str(shot.get("action") or shot.get("clip_prompt") or "").lower()
            if any(m in action for m in leave_markers):
                left_chars.extend([str(x) for x in (shot.get("character_ids") or [])])
        left_chars = list(dict.fromkeys(left_chars))
        patched: list[str] = []
        for shot in shots:
            idx = int(shot.get("clip_index") or 0)
            timeline = str(shot.get("timeline") or "").strip()
            if not timeline:
                shot["timeline"] = f"{(idx - 1) * 5:.1f}-{idx * 5:.1f}s"
                patched.append(f"duration:shot{idx}")
            if left_chars and idx > 1:
                lock = dict(shot.get("continuity_lock") or {}) if isinstance(shot.get("continuity_lock"), dict) else {}
                lock.setdefault(
                    "forbid",
                    "do not reseat or re-show a character who already stood and left earlier",
                )
                lock.setdefault("time", "forward-only consistency with prior shots")
                shot["continuity_lock"] = lock
                patched.append(f"consistency:shot{idx}")
            # Never paste the full user_prompt into short actions — that injects
            # later-meet cast into early beats. Sparse actions stay sparse;
            # LLM shot_fixes below may enrich without copying the whole brief.

        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
        )

        try:
            system = (
                "You are the Designer Director. Review the storyboard once for best quality "
                "using explicit user facts plus the approved enriched Brief and story shots as "
                "joint authority. Preserve the Brief's developed arc, distinct timed shots, and "
                "exact speech plan; do not reject approved enrichment merely because a sparse "
                "user prompt did not state it verbatim. Do not introduce a conflicting or "
                "unrelated plot, cast, claim, or geography. Fix missing characters/views, "
                "enhance sparse shots "
                "(crowd, atmosphere), keep each shot duration the approved brief already wrote, "
                "fill a duration only when that shot was left untimed, keep the brief's character "
                "names, wardrobe, places, and continuity notes, enforce time-coherent continuity, and "
                "keep geography locked (same landmarks/layout/light across views). "
                "Also approve/enforce film audio locks: language_lock (one language for all "
                "speech), per-shot speech_by_character (exact lines for every speaking shot; "
                "{} only when that shot has no speaker or the user asked for mime or no dialogue), and "
                "film-wide bgm_lock. "
                + "Respond JSON only: "
                '{"ok":true,"shot_fixes":[{"clip_index":1,"action":"...","camera":"...",'
                '"timeline":"<copy this shot timeline from the approved brief>",'
                '"continuity_lock":{"forbid":"..."},'
                '"character_ids":["char_1"],'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"language_lock":"<spoken language code, e.g. zh or en>",'
                '"bgm_lock":{"mood":"...","style":"...","rule":"..."},'
                '"include_speech":true,"include_music":true,"notes":"..."}'
            ) + _language_rule() + POSITIVE_VISUAL_RULE
            parsed = await _model_json_object(
                phase="storyboard review",
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": str(meta.get("approved_brief") or "")[:12000],
                        "approved_storyboard": str(
                            meta.get("approved_storyboard") or ""
                        )[:16000],
                        "clips": shots,
                        "brief_hint": meta.get("director_brief_notes") or "",
                    },
                    ensure_ascii=False,
                ),
                system=system,
                max_tokens=32768,
            )
            if parsed is None:
                ack["review_skipped"] = "invalid_json"
                parsed = {
                    "notes": "Storyboard review reply was not valid JSON; kept the authored storyboard."
                }
            for fix in parsed.get("shot_fixes") or []:
                if not isinstance(fix, dict):
                    continue
                try:
                    idx = int(fix.get("clip_index") or 0)
                except (TypeError, ValueError):
                    continue
                for shot in shots:
                    if int(shot.get("clip_index") or 0) != idx:
                        continue
                    for key in ("action", "camera", "timeline", "clip_prompt", "speech_line"):
                        if fix.get(key):
                            shot[key] = str(fix[key])[:600]
                    if isinstance(fix.get("continuity_lock"), dict):
                        shot["continuity_lock"] = {
                            str(k): str(v) for k, v in fix["continuity_lock"].items()
                        }
                    if isinstance(fix.get("character_ids"), list):
                        shot["character_ids"] = [str(x) for x in fix["character_ids"] if str(x)]
                    if isinstance(fix.get("speech_by_character"), dict):
                        shot["speech_by_character"] = {
                            str(k): str(v)[:280]
                            for k, v in fix["speech_by_character"].items()
                            if str(v).strip()
                        }
                    patched.append(f"llm:shot{idx}")
            if parsed.get("language_lock"):
                analysis["language_lock"] = str(parsed.get("language_lock"))[:16]
                patched.append("language_lock")
            if isinstance(parsed.get("bgm_lock"), dict):
                analysis["bgm_lock"] = {
                    str(k): str(v)[:280] for k, v in parsed["bgm_lock"].items()
                }
                patched.append("bgm_lock")
            audio = dict(analysis.get("audio") or {})
            if "include_speech" in parsed:
                audio["include_speech"] = bool(parsed.get("include_speech"))
            if "include_music" in parsed:
                audio["include_music"] = bool(parsed.get("include_music"))
            if parsed.get("language_lock"):
                audio["language_lock"] = str(parsed.get("language_lock"))[:16]
            if isinstance(parsed.get("bgm_lock"), dict):
                audio["bgm_lock"] = analysis.get("bgm_lock")
            analysis["audio"] = audio
            ack["source"] = "llm"
            ack["notes"] = str(parsed.get("notes") or ack["notes"])[:1000]
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director storyboard LLM review failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while reviewing the storyboard: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis
        from jiuwenswarm.server.runtime.designer.pipeline.clip_scope import apply_shot_scope

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        analysis = apply_shot_scope(analysis, user_prompt)
        shots = list(analysis.get("clips") or shots)
        meta["language_lock"] = str(analysis.get("language_lock") or meta.get("language_lock") or "en")
        if isinstance(analysis.get("bgm_lock"), dict):
            meta["bgm_lock"] = analysis["bgm_lock"]
        patched.append("audio_locks_approved")

        if shots:
            analysis["clips"] = shots
            meta["script_analysis"] = analysis
            from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
                render_storyboard_table,
            )

            meta["approved_storyboard"] = render_storyboard_table(
                shots,
                [c for c in (analysis.get("characters") or []) if isinstance(c, dict)],
                current_content_language(),
            )
            # Patch storyboard + downstream clip configs once.
            try:
                from jiuwenswarm.server.runtime.designer.audio_locks import (
                    stamp_audio_fields_on_clip_config,
                )

                for node in graph.get("nodes") or []:
                    cfg = dict(node.get("config") or {})
                    role = _role_key(node)
                    if role == "storyboard":
                        cfg["planned_shots"] = shots
                        node["config"] = cfg
                        continue
                    if role not in {"clip"}:
                        continue
                    idx = int(cfg.get("clip_index") or 0)
                    for shot in shots:
                        if int(shot.get("clip_index") or 0) != idx:
                            continue
                        if shot.get("action"):
                            cfg["clip_action"] = str(shot["action"])
                        if shot.get("camera"):
                            cfg["camera"] = str(shot["camera"])
                        if shot.get("timeline"):
                            cfg["timeline"] = str(shot["timeline"])
                        if isinstance(shot.get("continuity_lock"), dict):
                            cfg["continuity_lock"] = shot["continuity_lock"]
                        if role == "clip":
                            routing = (
                                meta.get("audio_routing")
                                if isinstance(meta.get("audio_routing"), dict)
                                else {}
                            )
                            cfg = stamp_audio_fields_on_clip_config(
                                cfg,
                                shot=shot,
                                analysis=analysis,
                                meta=meta,
                                clip_embedded=bool(
                                    routing.get("clip_embedded")
                                    or meta.get("prefer_wan3_clip_audio")
                                ),
                            )
                        node["config"] = cfg
                        break
                try:
                    from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                        chain_prior_speech_across_clips,
                    )

                    chain_prior_speech_across_clips(graph)
                except Exception:  # noqa: BLE001
                    logger.debug("chain_prior_speech_across_clips failed", exc_info=True)
            except Exception:  # noqa: BLE001
                logger.info("Director storyboard patch of leaf configs failed", exc_info=True)

        # After storyboard edits: prune unused + keep spatial graph coherent for final shot.
        prune_notes = _director_prune_and_cohere(graph)
        spatial_notes = _spatial_geography_lock_patch(graph)
        patched.extend(prune_notes)
        patched.extend(spatial_notes)

        ack["patched"] = patched[:40]
        ack["pruned_nodes"] = [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
        ack["spatial_lock_fixes"] = spatial_notes
        meta["storyboard_reviewed"] = True
        meta["director_storyboard_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def validate_plan(
        self, graph: DesignerExecutionGraph
    ) -> dict[str, Any]:
        """Validate the director brief, shots, and graph once via LLM (structural pass first)."""
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            LLM_NOT_CONFIGURED,
            model_text_or_raise,
        )

        ack = self.validate_plan_structure(graph)
        models = list_configured_models()
        if not models:
            raise DesignerLlmError(
                "Chat model is not configured. Configure a model in Settings before using Design.",
                code=LLM_NOT_CONFIGURED,
            )
        analysis = (graph.get("metadata") or {}).get("script_analysis") or {}
        system = (
            "You are the Designer Director Agent. Validate once (no loops) for best cinematic "
            "quality. Explicit user facts and constraints are authoritative; the approved "
            "enriched Brief, Storyboard, and structured shots are authoritative for details the "
            "user left unspecified. Preserve their creative arc, distinct timed shots, and exact "
            "speech plan. Do not reject approved enrichment merely because it was not stated "
            "verbatim in a sparse user prompt, and do not introduce a conflicting or unrelated "
            "plot, cast, claim, or geography. Check: "
            "(1) each shot's character_ids match that shot's focus subjects, "
            "(2) later shots do not reuse the wrong earlier cast, "
            "(3) enough shots cover every major character and prompt shot, "
            "(4) brief/storyboard are comprehensive enough for clip and shot prompting, "
            "(5) SPATIAL CONSISTENCY: motion + geography — landmarks/layout/light must "
            "match the master scene specs across shot views (edit/ref, not new buildings), "
            "(6) IDENTITY CONSISTENCY: every clip/shot must reference canonical character "
            "specs (identity_refs.character_node_ids), not reinvent costumes, "
            "(7) GRAPH USEFULNESS: every node must be useful for the final compose shot — "
            "list prune_ids for unused/orphan nodes; after prune the remaining graph must stay "
            "coherent (master scene → shot views → frames → shots → compose). "
            + "Respond JSON only: "
            '{"ok":true|false,"issues":["..."],"prune_ids":["n_unused"],'
            '"spatial_lock":{"landmarks":"...","layout":"...","light":"...","static_rule":"..."},'
            '"shot_fixes":[{"clip_index":1,"character_ids":["char_1"],'
            '"action":"...","continuity_lock":{"motion":"...","facing":"...","forbid":"..."},'
            '"costume_lock":"...","camera":"..."}],"notes":"..."}'
        ) + _language_rule() + POSITIVE_VISUAL_RULE
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "approved_brief": str(
                    (graph.get("metadata") or {}).get("approved_brief") or ""
                )[:12000],
                "approved_storyboard": str(
                    (graph.get("metadata") or {}).get("approved_storyboard") or ""
                )[:16000],
                "script_analysis": analysis,
                "continuity_locks": (graph.get("metadata") or {}).get("continuity_locks"),
                "spatial_lock": (graph.get("metadata") or {}).get("spatial_lock"),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "label": n.get("label"),
                        "role": _role_key(n),
                        "character_ids": (n.get("config") or {}).get("character_ids"),
                        "cast_names": (n.get("config") or {}).get("cast_names"),
                        "clip_action": (n.get("config") or {}).get("clip_action"),
                        "camera": (n.get("config") or {}).get("camera"),
                        "continuity_lock": (n.get("config") or {}).get("continuity_lock"),
                        "spatial_lock": (n.get("config") or {}).get("spatial_lock"),
                        "scene_strategy": (n.get("config") or {}).get("scene_strategy"),
                        "inputs": (n.get("config") or {}).get("inputs"),
                        "generate_prompt": ((n.get("config") or {}).get("generate") or {}).get(
                            "prompt"
                        ),
                    }
                    for n in (graph.get("nodes") or [])
                ],
                "structural_ack": ack,
            },
            ensure_ascii=False,
        )
        try:
            result = await call_model_tool(
                prompt=prompt,
                system=system,
                max_tokens=32768,
            )
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text) or {}
            if not parsed:
                raise DesignerLlmError(
                    "Chat model did not return a usable plan validation.",
                    code=LLM_API_ERROR,
                )
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director LLM validate_plan failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while validating the plan: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        # Apply one-shot shot_fixes into analysis + clip configs (no re-loop).
        fixes = parsed.get("shot_fixes") if isinstance(parsed.get("shot_fixes"), list) else []
        applied: list[str] = []
        analysis = dict(analysis) if isinstance(analysis, dict) else {}
        shots = list(analysis.get("clips") or [])
        id_to_name = {
            str(c.get("id")): str(c.get("name") or c.get("id"))
            for c in (analysis.get("characters") or [])
            if isinstance(c, dict) and c.get("id")
        }
        for fix in fixes:
            if not isinstance(fix, dict):
                continue
            try:
                idx = int(fix.get("clip_index") or 0)
            except (TypeError, ValueError):
                continue
            if idx < 1:
                continue
            cids = [str(x) for x in (fix.get("character_ids") or []) if str(x)]
            action = str(fix.get("action") or "").strip()
            lock = fix.get("continuity_lock") if isinstance(fix.get("continuity_lock"), dict) else None
            camera = str(fix.get("camera") or "").strip()
            costume = str(fix.get("costume_lock") or "").strip()
            for shot in shots:
                if int(shot.get("clip_index") or 0) != idx:
                    continue
                if cids:
                    shot["character_ids"] = cids
                if action:
                    shot["action"] = action
                    shot["clip_prompt"] = action
                if lock:
                    shot["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                if camera:
                    shot["camera"] = camera
                if costume:
                    shot["costume_lock"] = costume
                applied.append(f"shot {idx} llm-fix")
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if _role_key(node) not in {"clip"}:
                    continue
                if int(cfg.get("clip_index") or 0) != idx:
                    continue
                if cids:
                    cfg["character_ids"] = cids
                    cfg["cast_names"] = [id_to_name.get(cid, cid) for cid in cids]
                if action:
                    cfg["clip_action"] = action
                if camera:
                    cfg["camera"] = camera
                if costume:
                    cfg["costume_lock"] = costume
                if lock:
                    cfg["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                node["config"] = cfg
        if shots:
            analysis["clips"] = shots
            meta = dict(graph.get("metadata") or {})
            meta["script_analysis"] = analysis
            graph["metadata"] = meta
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if _role_key(node) != "storyboard":
                    continue
                cfg["planned_shots"] = shots
                node["config"] = cfg

        # Re-stamp consistency + identity + spatial after LLM fixes; prune unused nodes.
        if isinstance(parsed.get("spatial_lock"), dict):
            meta = dict(graph.get("metadata") or {})
            meta["spatial_lock"] = {
                str(k): str(v)[:400]
                for k, v in parsed["spatial_lock"].items()
                if str(v).strip()
            }
            analysis2 = dict(meta.get("script_analysis") or {})
            analysis2["spatial_lock"] = meta["spatial_lock"]
            meta["script_analysis"] = analysis2
            graph["metadata"] = meta
        # Explicit prune_ids from director LLM (then structural prune).
        # Never drop shot shots or required audio — every shot must reach the final film with sound.
        protected = {
            str(n.get("id")) for n in (graph.get("nodes") or []) if isinstance(n, dict) and _is_prune_protected(n)
        }
        from jiuwenswarm.server.runtime.designer.user_references import (
            is_user_reference_node,
        )

        reference_ids = {
            str(n.get("id"))
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and is_user_reference_node(n)
        }
        unprunable = protected | reference_ids | {"n_compose"}
        prune_ids = [str(x) for x in (parsed.get("prune_ids") or []) if str(x).strip() and str(x) not in unprunable]
        if prune_ids:
            drop = set(prune_ids)
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if not isinstance(n, dict) or str(n.get("id")) not in drop
            ]
            graph["edges"] = _edges_avoiding(graph.get("edges") or [], drop)
            applied.extend([f"director_prune:{x}" for x in prune_ids])

        consistency_notes = _spatial_continuity_patch(graph)
        identity_notes = _identity_consistency_patch(graph)
        spatial_notes = _spatial_geography_lock_patch(graph)
        prune_notes = _director_prune_and_cohere(graph)
        ensure_notes = self.ensure_agents_and_prune(graph)

        ack = {
            **ack,
            "ok": bool(parsed.get("ok", True)),
            "llm_issues": list(parsed.get("issues") or [])[:20],
            "llm_notes": str(parsed.get("notes") or "")[:1000],
            "patched": list(ack.get("patched") or [])
            + applied
            + consistency_notes
            + identity_notes
            + spatial_notes
            + prune_notes
            + list(ensure_notes.get("notes") or []),
            "consistency_fixes": list(ack.get("consistency_fixes") or []) + consistency_notes,
            "identity_fixes": list(ack.get("identity_fixes") or []) + identity_notes,
            "spatial_lock_fixes": list(ack.get("spatial_lock_fixes") or []) + spatial_notes,
            "pruned_nodes": list(
                dict.fromkeys(
                    [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
                    + list(ensure_notes.get("pruned") or [])
                )
            ),
            "ensure_agents": ensure_notes,
            "source": "llm",
        }
        meta = dict(graph.get("metadata") or {})
        meta["director_plan_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def review(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_plan: dict[str, Any] | None,
        prior_feedback: dict[str, Any] | None,
        director_report: dict[str, Any] | None = None,
        node_states: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        vision_notes: dict[str, str] = {}
        modality = (graph.get("metadata") or {}).get("modality_plan") or {}
        if bool(modality.get("can_vision")):
            from jiuwenswarm.server.runtime.designer.capabilities import (
                collect_rateable_image_paths,
                inspect_image_for_rating,
            )

            for nid, path in collect_rateable_image_paths(graph, node_states, limit=2):
                q = (
                    "Rate briefly for a short film pipeline: is this image usable and "
                    "consistent with a cinematic image? Reply in 2 short sentences; "
                    "say match/mismatch/clear/blank if relevant."
                )
                ans = await inspect_image_for_rating(path, q)
                if ans:
                    vision_notes[nid] = ans
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        _ = prior_feedback
        system = (
            "You are the Designer Director Agent. Review the whole pipeline. "
            "Score every node, the director, and overall job 0-10. "
            "Respond JSON only: "
            '{"scores":{"<node_id>":0-10,"director":0-10,"overall":0-10},'
            '"summary":"...","pipeline_notes":"...","suggestions":{"<node_id>":"...","global":"..."}}'
        ) + _language_rule()
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "director_plan": director_plan or {},
                "director_report": director_report or {},
                "agent_feedback": agent_feedback,
                "prior_feedback_final": (prior_feedback or {}).get("final"),
                "vision_notes": vision_notes,
                "rating_modality": (graph.get("metadata") or {}).get("rating_modality"),
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            max_tokens=32768,
        )
        text = model_text_or_raise(result)
        parsed = _extract_json_object(text) or {}
        if not parsed:
            raise DesignerLlmError(
                "Chat model did not return a usable director review.",
                code=LLM_API_ERROR,
            )
        scores_raw = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
        scores: dict[str, int] = {}
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            scores[nid] = _clamp_score(scores_raw.get(nid), default=6)
        scores["director"] = _clamp_score(scores_raw.get("director"), default=6)
        scores["overall"] = _clamp_score(scores_raw.get("overall"), default=6)
        rating_mod = str(
            (graph.get("metadata") or {}).get("rating_modality") or "text_only"
        )
        return {
            "scores": scores,
            "summary": str(parsed.get("summary") or text)[:3000],
            "pipeline_notes": str(parsed.get("pipeline_notes") or "")[:2000],
            "suggestions": parsed.get("suggestions")
            if isinstance(parsed.get("suggestions"), dict)
            else {},
            "director_model": result.get("model"),
            "rates_director": True,
            "rating_modality": rating_mod,
            "vision_used": bool(vision_notes),
            "vision_notes": vision_notes,
        }

    async def finalize(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_review: dict[str, Any],
        node_states: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        vision_notes: dict[str, str] = {}
        modality = (graph.get("metadata") or {}).get("modality_plan") or {}
        if bool(modality.get("can_vision")):
            from jiuwenswarm.server.runtime.designer.capabilities import (
                collect_rateable_image_paths,
                inspect_image_for_rating,
            )

            for nid, path in collect_rateable_image_paths(graph, node_states, limit=3):
                q = (
                    "Does this image match a usable cinematic reference for a short film? "
                    "Two short sentences; include match/mismatch/clear/blank if relevant."
                )
                ans = await inspect_image_for_rating(path, q)
                if ans:
                    vision_notes[nid] = ans
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        _ = director_review
        system = (
            "You are the Designer Director closing the run. "
            "Use vision_notes when present; otherwise rate from text status/messages only. "
            "Do not claim to have seen media without vision_notes. "
            "Rate every node agent and overall 0-10, summarize, and give per-node + global improvements. "
            "JSON only: "
            '{"scores":{"<node_id>":0-10,"overall":0-10},'
            '"summary":"...","suggestions":{"<node_id>":"...","global":"..."},'
            '"aggregated_score":0-10,"improvement_plan":"..."}'
        ) + _language_rule()
        prompt = json.dumps(
            {
                "agent_feedback": agent_feedback,
                "director_review": director_review,
                "user_prompt": graph.get("description"),
                "vision_notes": vision_notes,
                "rating_modality": (graph.get("metadata") or {}).get("rating_modality"),
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            max_tokens=32768,
        )
        text = model_text_or_raise(result)
        parsed = _extract_json_object(text) or {}
        if not parsed:
            raise DesignerLlmError(
                "Chat model did not return a usable director finalize report.",
                code=LLM_API_ERROR,
            )
        scores_raw = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
        scores: dict[str, int] = {}
        self_scores: list[int] = []
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            scores[nid] = _clamp_score(scores_raw.get(nid), default=6)
            self_scores.append(_clamp_score((agent_feedback.get(nid) or {}).get("self_score"), 6))
        scores["overall"] = _clamp_score(scores_raw.get("overall"), default=6)
        director_overall = _clamp_score((director_review.get("scores") or {}).get("overall"), 6)
        avg_self = sum(self_scores) / max(1, len(self_scores))
        aggregated = _clamp_score(
            parsed.get("aggregated_score"),
            default=int(round((scores["overall"] + director_overall + avg_self) / 3)),
        )
        suggestions = (
            parsed.get("suggestions")
            if isinstance(parsed.get("suggestions"), dict)
            else {}
        )
        return {
            "scores": scores,
            "summary": str(parsed.get("summary") or "")[:3000],
            "suggestions": suggestions,
            "aggregated_score": aggregated,
            "improvement_plan": str(parsed.get("improvement_plan") or suggestions.get("global") or "")[
                :3000
            ],
            "director_model": result.get("model"),
            "rating_modality": str(
                (graph.get("metadata") or {}).get("rating_modality") or "text_only"
            ),
            "vision_used": bool(vision_notes),
            "vision_notes": vision_notes,
        }


def _heuristic_node_score(
    node_id: str,
    *,
    agent_feedback: dict[str, dict[str, Any]],
    node_states: dict[str, Any] | None,
) -> tuple[int, str]:
    fb = agent_feedback.get(node_id) or {}
    if "self_score" in fb:
        return _clamp_score(fb.get("self_score"), 6), str(fb.get("notes") or "")
    state = (node_states or {}).get(node_id) or {}
    status = str(state.get("status") or "")
    if status == "completed" and state.get("output_ref"):
        return 7, "Completed with output"
    if status == "completed":
        return 6, "Completed"
    if status == "failed":
        return 2, str(state.get("error") or "failed")
    return 5, status or "unknown"


def _shot_distinctness_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Ensure each clip has a unique clip_action + generate prompt (text-only)."""
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import shot_brief_prompt

    notes: list[str] = []
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"clip"}:
            continue
        idx = int(cfg.get("clip_index") or 0) or 1
        action = str(cfg.get("clip_action") or "").strip()
        camera = str(cfg.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        cfg["camera"] = camera
        if not action:
            timeline = str(cfg.get("timeline") or "").strip()
            action = (
                f"Film only the {timeline or f'shot {idx}'} time window. "
                "Do not restage another window from a new camera angle."
            )
            cfg["clip_action"] = action
            notes.append(f"{node.get('id')}: filled missing clip_action from time window")
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "").strip()
        marker = f"shot {idx}"
        cast_names = [str(x) for x in (cfg.get("cast_names") or []) if str(x).strip()]
        lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else None
        if not lock and action:
            cfg["continuity_lock"] = _infer_continuity_lock(action)
        if not prompt or (marker not in prompt.lower() and f"第{idx}镜" not in prompt):
            gen["prompt"] = shot_brief_prompt(idx, camera=camera, action=action, cast_names=cast_names)
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: refreshed generate.prompt")
        if role == "clip":
            cfg["max_video_calls"] = 1
        node["config"] = cfg
    return notes


def _cast_focus_alignment_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Director gate: re-score shot focus from action text so wrong cast is not reused."""
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _focus_character_ids,
        _match_terms_for_character,
    )

    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = list(analysis.get("characters") or [])
    if not characters:
        return notes
    for ch in characters:
        if isinstance(ch, dict) and not ch.get("match_terms"):
            ch["match_terms"] = _match_terms_for_character(
                str(ch.get("name") or ""), str(ch.get("description") or "")
            )
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    # Refresh analysis shots if present. Do not expand a tight focus into a
    # full-cast keyword match (floods reference images → DashScope fails).
    for shot in analysis.get("clips") or []:
        if not isinstance(shot, dict):
            continue
        blob = f"{shot.get('action') or ''} {shot.get('clip_prompt') or ''}"
        focus = _focus_character_ids(blob, characters)
        old = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        if focus and focus != old:
            old_set, new_set = set(old), set(focus)
            if old and old_set.issubset(new_set) and len(new_set) > len(old_set):
                continue
            notes.append(
                f"analysis shot {shot.get('clip_index')}: "
                f"{shot.get('character_ids')} -> {focus}"
            )
            shot["character_ids"] = focus
    meta["script_analysis"] = analysis

    # Refresh approved storyboard so the handler writes the corrected focus cast.
    try:
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import render_storyboard_table

        planned = list(analysis.get("clips") or [])
        if planned:
            meta["approved_storyboard"] = render_storyboard_table(planned, characters, current_content_language())
            graph["metadata"] = meta
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if _role_key(node) != "storyboard":
                    continue
                cfg["planned_shots"] = planned
                node["config"] = cfg
                notes.append(f"{node.get('id')}: storyboard refreshed from cast-focus fixes")
    except Exception:  # noqa: BLE001
        pass

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"clip"}:
            continue
        blob = f"{cfg.get('clip_action') or ''} {(cfg.get('generate') or {}).get('prompt') or ''}"
        focus = _focus_character_ids(blob, characters)
        if not focus:
            continue
        old = [str(x) for x in (cfg.get("character_ids") or [])]
        if focus != old:
            old_set, new_set = set(old), set(focus)
            # Keep tighter original when keyword match only expands the cast.
            if old and old_set.issubset(new_set) and len(new_set) > len(old_set):
                continue
            cfg["character_ids"] = focus
            cfg["cast_names"] = [id_to_name.get(cid, cid) for cid in focus]
            notes.append(f"{node.get('id')}: cast focus {old} -> {focus}")
            # Prefer SOLO character specs (one character_id) over combined compose aids.
            solo_nodes: list[str] = []
            for cid in focus:
                for other in graph.get("nodes") or []:
                    if not isinstance(other, dict):
                        continue
                    oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                    if _role_key(other) != "character":
                        continue
                    if oc.get("combined_cast"):
                        continue
                    oids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                    if not oids and oc.get("character_id"):
                        oids = [str(oc.get("character_id"))]
                    if oids == [cid]:
                        solo_nodes.append(str(other.get("id")))
                        break
            if solo_nodes and len(solo_nodes) == len(focus):
                cfg["character_node_ids"] = list(dict.fromkeys(solo_nodes))
            else:
                char_nodes = []
                for other in graph.get("nodes") or []:
                    if not isinstance(other, dict):
                        continue
                    oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                    if _role_key(other) != "character":
                        continue
                    if oc.get("combined_cast"):
                        continue
                    oids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                    if not oids and oc.get("character_id"):
                        oids = [str(oc.get("character_id"))]
                    if set(oids) & set(focus):
                        char_nodes.append(str(other.get("id")))
                if char_nodes:
                    cfg["character_node_ids"] = list(dict.fromkeys(char_nodes))
            spec_node_ids = cfg.get("character_node_ids") or []
            spec_configs = [
                spec_node.get("config") or {}
                for spec_node in (graph.get("nodes") or [])
                if str(spec_node.get("id")) in spec_node_ids
            ]
            costume_parts = []
            for cid in focus:
                description = next((c.get("description") or "" for c in characters if str(c.get("id")) == cid), "")
                costume = next(
                    (
                        str(spec.get("costume_lock") or "")
                        for spec in spec_configs
                        if str(spec.get("character_id") or "") == cid
                    ),
                    str(description),
                )
                costume_parts.append(f"{id_to_name.get(cid, cid)}: {costume}")
            costume_lock = "; ".join(p for p in costume_parts if p).strip("; ")
            identity_refs = {
                "character_ids": list(focus),
                "character_node_ids": list(cfg.get("character_node_ids") or []),
                "cast_names": list(cfg.get("cast_names") or []),
                "costume_lock": costume_lock,
                "scene_node_id": "n_scene",
                "prior_clip_node_id": cfg.get("prior_clip_node_id"),
                "clip_strategy": cfg.get("clip_strategy") or "compose_from_character_specs",
            }
            cfg["identity_refs"] = identity_refs
            cfg["costume_lock"] = costume_lock
            cfg["director_task"] = _lock_free_media_task(cfg)
            node["config"] = cfg
    graph["metadata"] = meta
    return notes


_LEGACY_LOCK_TASK_PREFIXES = ("LOCKS:", "Use identity_refs sheets")


def _lock_free_media_task(cfg: dict[str, Any]) -> str:
    """Agent task text for a media node; lock values reach the agent only via the locks snapshot."""
    existing = str(cfg.get("director_task") or "").strip()
    if existing and not existing.startswith(_LEGACY_LOCK_TASK_PREFIXES):
        return existing
    setting = str(cfg.get("setting_id") or "").strip() or "this setting"
    return (
        f"Compose from the character specs in locks.character_node_ids for {setting}. "
        "Wardrobe, staging, scene specs and the other locks are in the JSON locks snapshot. "
        "Write one positive prompt from THIS storyboard row, then call the media model once."
    )


def _ensure_all_character_specs_precede_clips(graph: DesignerExecutionGraph) -> list[str]:
    """Every character spec node is a data predecessor of every clip node.

    Guarantees all character specs finish before any compose/edit clip runs.
    """
    notes: list[str] = []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    character_spec_node_ids = [
        nid
        for nid, node in by_id.items()
        if _role_key(node) == "character" and not bool((node.get("config") or {}).get("combined_cast"))
    ]
    if not character_spec_node_ids:
        return notes
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in (graph.get("edges") or [])
        if isinstance(e, dict)
    }
    for node in nodes:
        if _role_key(node) not in {"clip"}:
            continue
        fid = str(node.get("id") or "")
        if not fid:
            continue
        cfg = dict(node.get("config") or {})
        inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
        changed = False
        for sid in character_spec_node_ids:
            if sid not in inputs:
                inputs.append(sid)
                changed = True
            if (sid, fid) not in edge_pairs:
                graph.setdefault("edges", []).append(
                    {
                        "id": f"e_char_{sid}_{fid}",
                        "source": sid,
                        "target": fid,
                        "kind": "data",
                        "label": "identity_character_spec",
                    }
                )
                edge_pairs.add((sid, fid))
                notes.append(f"{fid}: character_spec_gate <- {sid}")
                changed = True
        if changed:
            cfg["inputs"] = list(dict.fromkeys(inputs))
            node["config"] = cfg
    return notes


def _identity_consistency_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Director gate: character specs first; every clip composes; same-setting prompt handoff."""
    notes: list[str] = []
    spec_by_cid: dict[str, str] = {}
    costume_by_cid: dict[str, str] = {}
    all_character_spec_ids: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if _role_key(node) != "character":
            continue
        if cfg.get("combined_cast"):
            continue
        nid = str(node.get("id") or "")
        if nid:
            all_character_spec_ids.append(nid)
        oids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        if not oids and cfg.get("character_id"):
            oids = [str(cfg.get("character_id"))]
        if len(oids) == 1:
            spec_by_cid[oids[0]] = nid
            costume_by_cid[oids[0]] = str(cfg.get("costume_lock") or cfg.get("prompt") or "")[:200]

    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = list(analysis.get("characters") or [])
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    scene_locks = (
        meta.get("scene_locks")
        if isinstance(meta.get("scene_locks"), dict)
        else (
            analysis.get("scene_locks")
            if isinstance(analysis.get("scene_locks"), dict)
            else {}
        )
    )

    notes.extend(_ensure_all_character_specs_precede_clips(graph))

    frame_nodes = sorted(
        [
            n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and _role_key(n) in {"clip"}
        ],
        key=lambda n: int((n.get("config") or {}).get("clip_index") or 0),
    )

    prev_frame_by_setting: dict[str, str] = {}
    scene_master_by_setting: dict[str, str] = {}
    spatial_meta = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    spatial_by_setting = (
        meta.get("spatial_lock_by_setting")
        if isinstance(meta.get("spatial_lock_by_setting"), dict)
        else {}
    )
    existing_masters = (
        meta.get("scene_masters") if isinstance(meta.get("scene_masters"), dict) else {}
    )
    for sid, fid in existing_masters.items():
        if str(sid).strip() and str(fid).strip():
            scene_master_by_setting[str(sid).strip()] = str(fid).strip()

    setting_order: list[str] = []
    for n in frame_nodes:
        sid = str((n.get("config") or {}).get("setting_id") or "set_1").strip() or "set_1"
        if sid not in setting_order:
            setting_order.append(sid)
    setting_num = {sid: i + 1 for i, sid in enumerate(setting_order)}

    def _stamp_media_node(node: dict[str, Any], *, assign_strategy: bool) -> None:
        nonlocal notes
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        cids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        spec_nodes = [spec_by_cid[cid] for cid in cids if cid in spec_by_cid]
        if spec_nodes and list(cfg.get("character_node_ids") or []) != spec_nodes:
            cfg["character_node_ids"] = spec_nodes
            notes.append(f"{node.get('id')}: identity_refs -> character specs {spec_nodes}")
        names = [id_to_name.get(cid, cid) for cid in cids]
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            costume_lock_for_ids,
            enrich_character_clothing,
        )

        analysis_chars = list(
            ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
            or []
        )
        for ch in analysis_chars:
            if isinstance(ch, dict):
                enrich_character_clothing(ch)
        costume_lock = str(cfg.get("costume_lock") or "").strip()
        detailed = costume_lock_for_ids(analysis_chars, cids) if cids else ""
        has_slots = any(key in costume_lock for key in ("top=", "bottom=", "outfit="))
        if detailed and (not costume_lock or not has_slots):
            costume_lock = detailed
            cfg["costume_lock"] = costume_lock
            notes.append(f"{node.get('id')}: clothing costume_lock stamped")
        elif not costume_lock and cids:
            costume_lock = "; ".join(
                f"{id_to_name.get(cid, cid)}: {costume_by_cid.get(cid, '')[:120]}".strip(": ")
                for cid in cids
            )[:720]
            cfg["costume_lock"] = costume_lock
            notes.append(f"{node.get('id')}: costume_lock stamped")

        setting_id = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        cfg["setting_id"] = setting_id
        strategy = "compose_from_character_specs"
        is_master = False
        handoff_from = None
        if assign_strategy and role in {"clip"}:
            prior_in_set = prev_frame_by_setting.get(setting_id)
            if prior_in_set:
                is_master = False
                handoff_from = scene_master_by_setting.get(setting_id) or prior_in_set
                cfg["scene_prompt_handoff_from"] = handoff_from
                cfg["is_scene_master"] = False
                cfg["scene_master_node_id"] = handoff_from
                cfg.pop("prior_clip_node_id", None)
                inputs = list(cfg.get("inputs") or [])
                if handoff_from and handoff_from not in inputs:
                    inputs.append(handoff_from)
                cfg["inputs"] = inputs
                notes.append(
                    f"{node.get('id')}: same-setting compose + prompt handoff from "
                    f"{handoff_from} ({setting_id})"
                )
            else:
                is_master = True
                cfg.pop("prior_clip_node_id", None)
                cfg.pop("scene_prompt_handoff_from", None)
                cfg["is_scene_master"] = True
                cfg["scene_master_node_id"] = str(node.get("id") or "")
                notes.append(
                    f"{node.get('id')}: SCENE MASTER compose for setting {setting_id}"
                )
            cfg["clip_strategy"] = strategy
            nid = str(node.get("id") or "")
            if is_master and nid:
                scene_master_by_setting[setting_id] = nid
            if nid:
                prev_frame_by_setting[setting_id] = nid

        specs = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
        if not specs and isinstance(scene_locks.get(setting_id), dict):
            specs = dict(scene_locks[setting_id])
            cfg["scene_specs"] = specs

        shot_spatial = (
            spatial_by_setting.get(setting_id)
            if isinstance(spatial_by_setting.get(setting_id), dict)
            else None
        )
        if isinstance(shot_spatial, dict):
            cfg["spatial_lock"] = dict(shot_spatial)
        elif not isinstance(cfg.get("spatial_lock"), dict) and spatial_meta:
            cfg["spatial_lock"] = dict(spatial_meta)

        master_frame = str(
            cfg.get("scene_master_node_id")
            or scene_master_by_setting.get(setting_id)
            or ""
        ).strip() or None
        identity_refs = {
            "character_ids": cids,
            "character_node_ids": list(cfg.get("character_node_ids") or spec_nodes),
            "cast_names": names or list(cfg.get("cast_names") or []),
            "costume_lock": costume_lock,
            "scene_node_id": None,
            "master_scene_node_id": None,
            "scene_master_node_id": master_frame,
            "is_scene_master": bool(cfg.get("is_scene_master")),
            "prior_clip_node_id": None,
            "scene_prompt_handoff_from": cfg.get("scene_prompt_handoff_from") or handoff_from,
            "clip_strategy": "compose_from_character_specs",
            "setting_id": setting_id,
            "view_key": cfg.get("view_key"),
            "spatial_lock": cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else None,
            "occupancy": cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else None,
            "scene_consistency_mode": "scene_specs_plus_clips",
            "scene_specs": specs,
            "all_character_spec_node_ids": list(all_character_spec_ids),
        }
        cfg["identity_refs"] = identity_refs
        if names:
            cfg["cast_names"] = identity_refs["cast_names"]
        # Sensible LLM-style node names (Brief: … / Scene N: Clip M: …).
        from jiuwenswarm.common.schema.designer_node_labels import (
            derive_clip_name,
            derive_story_name,
            label_character,
            label_clip,
        )

        story_name = derive_story_name(
            analysis=analysis,
            prompt=str(graph.get("description") or ""),
            graph_title=str(graph.get("title") or ""),
        )
        scene_n = setting_num.get(setting_id, 1)
        shot_i = int(cfg.get("clip_index") or 0)
        # Per-setting shot ordinal from film order.
        shot_n = 0
        for fn in frame_nodes:
            fsid = str((fn.get("config") or {}).get("setting_id") or "set_1").strip() or "set_1"
            fi = int((fn.get("config") or {}).get("clip_index") or 0)
            if fsid != setting_id:
                continue
            if fi <= shot_i:
                shot_n += 1
        shot_n = max(1, shot_n or shot_i or 1)
        clip_name = derive_clip_name(
            {
                "title": cfg.get("shot_title"),
                "action": cfg.get("clip_action"),
                "clip_prompt": (cfg.get("generate") or {}).get("prompt")
                if isinstance(cfg.get("generate"), dict)
                else "",
            },
            fallback_index=shot_n,
        )
        if role == "clip" and shot_i:
            label = label_clip(
                scene_number=scene_n, clip_number=shot_n, clip_name=clip_name
            )
            node["label"] = label
            cfg["agent_name"] = label
        cfg["director_task"] = _lock_free_media_task(cfg)
        node["config"] = cfg

    for node in frame_nodes:
        _stamp_media_node(node, assign_strategy=True)

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if _role_key(node) != "clip":
            continue
        _stamp_media_node(node, assign_strategy=False)
        # Also rename character sheets if generic.
    from jiuwenswarm.common.schema.designer_node_labels import (
        derive_story_name,
        label_brief,
        label_character,
        label_compose,
        label_storyboard,
    )

    story_name = derive_story_name(
        analysis=analysis,
        prompt=str(graph.get("description") or ""),
        graph_title=str(graph.get("title") or ""),
    )
    char_i = 0
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = _role_key(node)
        cfg = dict(node.get("config") or {})
        if role == "brief" or str(node.get("id") or "") == "n_brief":
            node["label"] = label_brief(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role == "storyboard" or str(node.get("id") or "") == "n_storyboard":
            node["label"] = label_storyboard(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role == "compose" or str(node.get("id") or "") in {"n_compose", "n_final"}:
            node["label"] = label_compose(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role != "character":
            continue
        char_i += 1
        name = str(
            cfg.get("character_name")
            or (cfg.get("character_names") or [None])[0]
            or ""
        ).strip()
        label = label_character(char_i, name or f"Character {char_i}")
        node["label"] = label
        cfg["agent_name"] = label
        node["config"] = cfg

    meta["scene_consistency_mode"] = "scene_specs_plus_clips"
    meta["scene_masters"] = dict(scene_master_by_setting)
    meta["scene_locks"] = dict(scene_locks)
    graph["metadata"] = meta
    return notes


