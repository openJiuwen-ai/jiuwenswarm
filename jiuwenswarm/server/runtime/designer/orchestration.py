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
    continuity_prompt_clause as _continuity_prompt_clause,
    infer_continuity_lock as _infer_continuity_lock,
    merge_lock_with_previous,
)
from jiuwenswarm.server.runtime.designer.feedback import (
    save_feedback,
    suggestion_for_node,
)
from jiuwenswarm.server.runtime.designer.model_tools import (
    call_model_tool,
    list_configured_models,
)
from jiuwenswarm.server.runtime.designer.user_references import carry_user_references

logger = logging.getLogger(__name__)

# Role → tool set for one-pass node agents (no feedback loop).
_ROLE_TOOLS: dict[str, list[str]] = {
    "brief": ["call_model", "read_upstream"],
    "character": ["call_model", "read_upstream", "call_image_model"],
    "character_design": ["call_model", "read_upstream", "call_image_model"],
    "scene": ["call_model", "read_upstream", "call_image_model"],
    "storyboard": ["call_model", "read_upstream"],
    "frame": ["call_model", "read_upstream", "call_image_model"],
    "keyframe": ["call_model", "read_upstream", "call_image_model"],
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


def _spatial_continuity_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Director gate: stamp consistency locks so keyframe/shot prompts share blocking."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    shot_locks: dict[int, dict[str, str]] = {}

    for shot in analysis.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        try:
            idx = int(shot.get("shot_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 1:
            continue
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
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
        clause = _continuity_prompt_clause(lock)
        kf = str(shot.get("keyframe_prompt") or action)
        if clause and "CONSISTENCY LOCK" not in kf:
            shot["keyframe_prompt"] = (kf[:500] + clause)[:700]
            notes.append(f"analysis shot {idx}: consistency lock stamped")

    if analysis.get("shots"):
        meta["script_analysis"] = analysis
        graph["metadata"] = meta

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"frame", "clip", "keyframe", "storyboard"}:
            continue
        idx = int(cfg.get("shot_index") or 0) or 0
        action = str(cfg.get("shot_action") or "")
        lock = dict(cfg.get("continuity_lock") or {}) if isinstance(cfg.get("continuity_lock"), dict) else {}
        if idx and idx in shot_locks:
            lock = shot_locks[idx]
        elif action and not lock:
            lock = _infer_continuity_lock(action)
        if not lock:
            continue
        cfg["continuity_lock"] = lock
        clause = _continuity_prompt_clause(lock)
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "").strip()
        if clause and "CONSISTENCY LOCK" not in prompt:
            if not prompt:
                camera = str(cfg.get("camera") or "medium / eye-level")
                prompt = f"Film shot {idx or '?'} only. Camera {camera}. Action: {action}."
            gen["prompt"] = (prompt + clause)[:1200]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: consistency lock in generate.prompt")
        elif role == "storyboard":
            notes.append(f"{node.get('id')}: consistency locks available for planned shots")
        node["config"] = cfg

    # Keep the storyboard agent's structured shot plan current.
    if shot_locks:
        shots = list(analysis.get("shots") or [])
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
    """Stamp / refresh spatial_lock on scene/frame/shot so architecture stays faithful."""
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
                "Empty environment plates; keyframes keep the SAME extras layout "
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
        if role not in {"scene", "frame", "clip", "keyframe", "brief", "storyboard"}:
            continue
        cfg["spatial_lock"] = lock
        if (
            role == "scene"
            and nid != "n_scene"
            and "n_scene" in ids
            and not cfg.get("master_scene_node_id")
        ):
            cfg["master_scene_node_id"] = "n_scene"
            cfg.setdefault("scene_strategy", "edit_master_view")
            notes.append(f"{nid}: master_scene_node_id=n_scene")
        if role in {"frame", "clip", "keyframe", "scene"}:
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            prompt = str(gen.get("prompt") or cfg.get("prompt") or "")
            if lock_clause.strip() and "SPATIAL LOCK" not in prompt:
                if gen.get("prompt") is not None or role in {"frame", "clip", "keyframe"}:
                    gen["prompt"] = (prompt + lock_clause)[:1400]
                    cfg["generate"] = gen
                else:
                    cfg["prompt"] = (prompt + lock_clause)[:1400]
                notes.append(f"{nid}: spatial_lock stamped")
        node["config"] = cfg
    return notes


def _director_prune_and_cohere(graph: DesignerExecutionGraph) -> list[str]:
    """Prune unused nodes, rewire spatial edges, keep every kept node useful for final film."""
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        ensure_combined_cast_reach_compose,
        prune_non_contributing_nodes,
    )

    notes: list[str] = []
    # Drop unused combined cast sheets that never feed a frame/clip.
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
            str(t).startswith("n_frame") or str(t).startswith("n_clip") or t == "n_compose"
            for t in (outs.get(nid) or [])
        ):
            drop.append(nid)
    if drop:
        drop_set = set(drop)
        graph["nodes"] = [n for n in nodes if str(n.get("id")) not in drop_set]
        graph["edges"] = [
            e
            for e in edges
            if str(e.get("source") or "") not in drop_set
            and str(e.get("target") or "") not in drop_set
        ]
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
            elif role in {"frame", "keyframe", "clip"}:
                shot_idx = int(cfg.get("shot_index") or 0)
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
        if role in {"frame", "keyframe", "clip"} or nid.startswith("n_frame_") or nid.startswith("n_clip_"):
            idx = int(cfg.get("shot_index") or 0)
            if idx >= 1 and idx not in kept_shot_idxs:
                kept_shot_idxs.append(idx)
    kept_shot_idxs.sort()
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    if kept_shot_idxs and shots:
        kept = {
            int(s.get("shot_index") or 0)
            for s in shots
            if int(s.get("shot_index") or 0) in set(kept_shot_idxs)
        }
        if kept and kept != {int(s.get("shot_index") or 0) for s in shots}:
            analysis["shots"] = [
                s for s in shots if int(s.get("shot_index") or 0) in kept
            ]
            notes.append(f"reedit_shots_keep:{sorted(kept)}")
        # Rebuild already_done chains per setting (never leak office beats into a new room).
        already_by_setting: dict[str, list[str]] = {}
        exited_by_setting: dict[str, set[str]] = {}
        revised: list[dict[str, Any]] = []
        for s in analysis.get("shots") or []:
            if not isinstance(s, dict):
                continue
            shot = dict(s)
            sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
            already = list(already_by_setting.get(sid) or [])
            shot["already_done"] = list(already)
            action = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
            exits = [
                str(x)
                for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
                if str(x)
            ]
            idx = int(shot.get("shot_index") or 0)
            if action:
                already.append(f"shot{idx}: {action[:120]}")
            carried = set(exited_by_setting.get(sid) or ())
            for cid in exits:
                already.append(
                    f"{cid} exited by shot{idx} — omit from later same-setting prompts until returned"
                )
                carried.add(cid)
            # Storyboard return clears exit for anyone listed on_screen again.
            visible = [
                str(x)
                for x in (
                    shot.get("on_screen")
                    or shot.get("visible_cast_ids")
                    or shot.get("character_ids")
                    or []
                )
                if str(x)
            ]
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
            ensemble = [
                str(x)
                for x in (
                    shot.get("ensemble_cast_ids")
                    or shot.get("compose_cast_ids")
                    or visible
                    or []
                )
                if str(x)
            ]
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
        analysis["shots"] = revised
        meta["script_analysis"] = analysis
        notes.append("reedit_already_done_occupancy")

    # Sync storyboard markdown + approved_storyboard from surviving shots.
    chars = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    surviving = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    if surviving:
        lines = ["# Storyboard Scenario", ""]
        for s in surviving:
            idx = int(s.get("shot_index") or 0)
            lines.append(
                f"### Shot {idx} — {s.get('title') or s.get('camera') or 'beat'}"
            )
            lines.append(f"- Setting: {s.get('setting_id') or 'set_1'}")
            lines.append(f"- Timeline: {s.get('timeline') or f'{(idx-1)*5}-{idx*5}s'}")
            lines.append(f"- Action: {s.get('action') or s.get('keyframe_prompt') or ''}")
            lines.append(f"- Camera: {s.get('camera') or ''}")
            if s.get("speech_line"):
                lines.append(f"- Speech: {s.get('speech_line')}")
            done = s.get("already_done") or []
            if done:
                lines.append(f"- Already done: {'; '.join(str(x) for x in done[:8])}")
            occ = s.get("occupancy") if isinstance(s.get("occupancy"), dict) else {}
            if occ:
                lines.append(
                    f"- Occupancy must_appear={occ.get('must_appear')}; featured={occ.get('featured')}"
                )
            lines.append("")
        sb_md = "\n".join(lines).strip() + "\n"
        meta["approved_storyboard"] = sb_md
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

    # Stamp occupancy / already_done onto surviving frame+shot nodes.
    by_idx = {
        int(s.get("shot_index") or 0): s
        for s in (analysis.get("shots") or [])
        if isinstance(s, dict) and int(s.get("shot_index") or 0) >= 1
    }
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = dict(n.get("config") or {})
        role = _role_key(n)
        if role not in {"frame", "keyframe", "clip"}:
            continue
        idx = int(cfg.get("shot_index") or 0)
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
        drop = (
            {
                str(n.get("id") or "")
                for n in (graph.get("nodes") or [])
                if isinstance(n, dict)
                and (
                    str(n.get("id") or "") == "n_speech"
                    or _role_key(n).lower() in {"speech", "tts"}
                )
            }
            if not can_speech
            else set()
        )
        if drop:
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if str(n.get("id") or "") not in drop
            ]
            graph["edges"] = [
                e
                for e in (graph.get("edges") or [])
                if str(e.get("source") or "") not in drop
                and str(e.get("target") or "") not in drop
            ]
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
                idx = int(cfg.get("shot_index") or 0)
                shot_row = next(
                    (
                        s
                        for s in (analysis.get("shots") or [])
                        if isinstance(s, dict) and int(s.get("shot_index") or 0) == idx
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


def _pick_model(models: list[dict[str, Any]], *, optimize_for: str, prefer_image: bool = False) -> str:
    if not models:
        return ""
    ordered = list(models)
    if optimize_for == "cost":
        ordered = list(reversed(ordered))
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


def validate_plan_occupancy(analysis: dict[str, Any]) -> dict[str, Any]:
    """Code validators after Director patch (no LLM): setting_id, on_screen, scene_locks, ids."""
    errors: list[str] = []
    warnings: list[str] = []
    characters = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
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
        idx = int(shot.get("shot_index") or i)
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if not sid:
            errors.append(f"shot{idx}_missing_setting_id")
        on_screen = [
            str(x)
            for x in (
                shot.get("on_screen")
                or shot.get("visible_cast_ids")
                or shot.get("featured_cast_ids")
                or []
            )
            if str(x)
        ]
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
    """Single overseeing agent: brief, storyboard, graph, locks, prompt gate, and ratings."""

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
                    "Respect film-wide aspect_lock, style_lock, spatial_lock, and "
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
                "frame",
                "keyframe",
                "clip",
                "character",
                "character_design",
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

        orphans = [
            nid
            for nid in find_non_contributing_node_ids(graph)
            if any(
                str(n.get("id") or "") == nid
                and isinstance(n.get("config"), dict)
                and n["config"].get("user_added")
                for n in (graph.get("nodes") or [])
                if isinstance(n, dict)
            )
        ]
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

    def adjust_clips_after_keyframes(
        self,
        graph: DesignerExecutionGraph,
        *,
        node_states: dict[str, Any] | None,
        agent_feedback: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        """After scene specs (or legacy keyframes) complete: refresh pending shot briefs."""
        notes = _shot_distinctness_patch(graph)
        feedback = agent_feedback or {}
        states = node_states or {}
        meta0 = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        scene_specs_mode = (
            str(meta0.get("scene_continuity_mode") or "") == "scene_card_plus_clip_shots"
        )
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "clip":
                continue
            if str((states.get(str(node.get("id") or "")) or {}).get("status") or "") in {
                "completed",
                "failed",
                "skipped",
            }:
                continue
            idx = int(cfg.get("shot_index") or 0) or 1
            action = str(cfg.get("shot_action") or "").strip()
            camera = str(cfg.get("camera") or "medium / eye-level")
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else {}
            clause = _continuity_prompt_clause(lock if isinstance(lock, dict) else None)
            shot_line = action or "match storyboard shot for this shot"
            if scene_specs_mode:
                scene_nid = str(cfg.get("scene_node_id") or "").strip()
                scene_msg = str((feedback.get(scene_nid) or {}).get("message") or "") if scene_nid else ""
                solos = [str(x) for x in (cfg.get("character_node_ids") or []) if str(x).strip()]
                who = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}
                who_line = "; ".join(f"{k}: {v}" for k, v in who.items() if v) if who else ""
                # Preserve locks already stamped on generate.prompt — append beat only if empty.
                existing = str(gen.get("prompt") or "").strip()
                if not existing or "FIRST FRAME" not in existing:
                    gen["prompt"] = (
                        f"Film shot {idx} from scene specs {scene_nid or 'n_scene'} + solo refs "
                        f"{', '.join(solos) or 'cast'}. Camera {camera}. Action: {shot_line}. "
                        + (f"WHO DOES WHAT: {who_line}. " if who_line else "")
                        + (f"Scene note: {scene_msg[:180]}. " if scene_msg else "")
                        + "Do not repeat other shots."
                        f"{clause}"
                    )
                cfg["generate"] = gen
                if action:
                    cfg["shot_action"] = action[:500]
                cfg["max_video_calls"] = 1
                cfg.pop("continuity_clip_node_id", None)
                node["config"] = cfg
                notes.append(f"{node.get('id')}: post-scene-specs shot brief updated")
                continue
            frame_id = f"n_frame_{idx}"
            frame_msg = str((feedback.get(frame_id) or {}).get("message") or "")
            gen["prompt"] = (
                f"Film shot {idx} only from its keyframe. Camera {camera}. "
                f"Action: {beat}. "
                + (f"Keyframe note: {frame_msg[:180]}. " if frame_msg else "")
                + "Do not repeat other shots."
                f"{clause}"
            )
            cfg["generate"] = gen
            if action:
                cfg["shot_action"] = action[:500]
            cfg["max_video_calls"] = 1
            if idx > 1:
                cfg["continuity_frame_node_id"] = cfg.get("continuity_frame_node_id") or f"n_frame_{idx - 1}"
            node["config"] = cfg
            notes.append(f"{node.get('id')}: post-keyframe shot brief updated")
        meta = dict(graph.get("metadata") or {})
        meta["shots_adjusted_after_keyframes"] = True
        meta["director_keyframe_adjust"] = {"notes": notes[:40], "rating_modality": "text_only"}
        graph["metadata"] = meta
        return notes

    async def plan(
        self,
        graph: DesignerExecutionGraph,
        *,
        optimize_for: str,
        prior_feedback: dict[str, Any] | None,
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
            "For each node, choose optimize_for (cost|quality) and a preferred_model "
            "from the configured Settings model list. "
            "On rerun, incorporate prior feedback suggestions. "
            "Respond with JSON only: "
            '{"optimize_for_global":"cost|quality",'
            '"brief_notes":"...",'
            '"spatial_lock":{"setting":"...","landmarks":"...","light":"...","static_rule":"..."},'
            '"node_directives":{"<node_id>":{"optimize_for":"...","preferred_model":"...","task":"..."}},'
            '"notes":"..."}'
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "optimize_for_default": optimize_for,
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
            optimize_for=optimize_for,
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
            mode = str(entry.get("optimize_for") or parsed.get("optimize_for_global") or optimize_for)
            mode = "cost" if mode == "cost" else "quality"
            preferred = str(entry.get("preferred_model") or "")
            if preferred and preferred not in model_ids and model_ids:
                preferred = model_ids[0]
            elif not preferred and model_ids:
                preferred = model_ids[0] if mode == "quality" else model_ids[-1]
            tools = _tools_for_node(node)
            directives[nid] = {
                "optimize_for": mode,
                "preferred_model": preferred,
                "task": str(entry.get("task") or f"Execute node {node.get('label') or nid}"),
                "tools": tools,
            }
        plan = {
            "optimize_for_global": (
                "cost"
                if str(parsed.get("optimize_for_global") or optimize_for) == "cost"
                else "quality"
            ),
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
            cfg["optimize_for"] = d.get("optimize_for", optimize_for)
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
                "dialogue or voiceover lines with speaker and timing when speech improves the "
                "concept; explicitly choose visual-only storytelling when it does not. Never add "
                "speech when the user requested silence. Also cover: character identity locks "
                "(face/hair/body/costume), "
                "scene geography (spatial lock), motion consistency, time-coherent continuity "
                "(do not undo a completed shot on a later shot), "
                "shot-view coverage for every named beat, audio policy, and one explicit "
                "Visual Style section. Preserve the user's exact visual medium and rendering "
                "details; use the provided style_lock when present. "
                "Preserve all explicit people, places, brand facts, claims, and requested events; "
                "creative enrichment may fill only details the user left unspecified. "
                "Respond with markdown brief only (no JSON wrapper)."
            )
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "characters": characters,
                        "scenes": scenes,
                        "shots": analysis.get("shots"),
                        "style_lock": analysis.get("style_lock"),
                        "audio": audio,
                        "spatial_lock": meta.get("spatial_lock"),
                        "director_brief_notes": meta.get("director_brief_notes")
                        or ((meta.get("director_plan") or {}).get("brief_notes")),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for="quality",
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
        """LLM-author storyboard markdown + planned_shots. Failures raise."""
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )
        from jiuwenswarm.server.runtime.designer.smart_graph import _write_storyboard_markdown

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        shots = list(analysis.get("shots") or [])
        user_prompt = str(graph.get("description") or "")
        # Keep the enriched timed shot and speech sections available to storyboard authoring.
        approved_brief = str(meta.get("approved_brief") or "")[:12000]
        sb_md = ""
        source = ""
        notes = "Director LLM authored storyboard."
        try:
            system = (
                "You are the Designer Director. Author a hierarchical "
                "storyboard: Scene (setting_id) → Keyframes/shots. FIRST list every "
                "named human as characters[] (id, name, description) — one solo card "
                "each. NOT every character appears in every scene. "
                "Copy the approved Brief's visual style exactly into style_lock and "
                "the storyboard_markdown Visual Style section; never substitute a "
                "model or leaf default. "
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
                "speech_line values in the appropriate shots. Keep language_lock and exact "
                "wording; use empty speech fields for deliberately silent shots and never add "
                "speech when the user requested silence. The human-readable storyboard_markdown "
                "must also show each exact spoken line or voiceover in its timed shot. "
                "First shot of each setting: "
                "keyframe_strategy=compose_from_solo_refs — composer places ONLY "
                "on_screen cast with cast_actions (who is doing what). "
                "Later same setting: edit_prior_keyframe (architecture locked); "
                "storyboard updates on_screen / offscreen / cast_actions. "
                "offscreen = in this scene but not in frame; never draw them. "
                "Cast absent from a setting must not appear there. "
                "NO scene specs. Crowd/extras persist across same-setting shots "
                "unless they exit. Each shot needs timeline, camera, action, "
                "on_screen, offscreen, cast_actions, featured_cast_ids, setting_id, "
                "start_state {pose,seats,facing,on_screen,offscreen}, "
                "end_state {pose,seats,facing,exited,speech_done,on_screen}, "
                "continuity_lock, keyframe_prompt, exiting_character_ids, "
                "speech_by_character (map character_id→exact spoken line for This shot; "
                "empty {} if silent), speech_line (joined fallback). "
                "Same setting_id: next shot start_state MUST match prior end_state. "
                "Exactly one shot has emotion=climax. Each shot includes irreversible "
                "(what is newly true at the last frame) and cast_states "
                "{id:{wardrobe,emotion,presence}}. Face stays the character sheet; "
                "change wardrobe or emotion only when this shot's story changes them. "
                "Shots are self-contained continuity windows — do not rely on prior "
                "clip media. Film-wide locks: language_lock (e.g. en/zh — ALL dialogue in that "
                "language), bgm_lock {mood,style,instruments,continuity,rule}, "
                "include_speech, include_music. "
                "Respond JSON only: "
                '{"style_lock":{"look":"...","medium":"..."},'
                '"characters":[{"id":"char_1","name":"...","description":"..."}],'
                '"shots":[{"shot_index":1,"timeline":"<copy this shot timeline from the approved brief>",'
                '"camera":"...",'
                '"action":"...","on_screen":["char_1"],"offscreen":["char_2"],'
                '"featured_cast_ids":["char_1"],"cast_actions":{"char_1":"preaching"},'
                '"ensemble_cast_ids":["char_1","char_2"],"setting_id":"set_1",'
                '"keyframe_strategy":"compose_from_solo_refs",'
                '"start_state":{"pose":"...","seats":{},"facing":"..."},'
                '"end_state":{"pose":"...","exited":[],"speech_done":"..."},'
                '"continuity_lock":{"forbid":"..."},"keyframe_prompt":"...",'
                '"exiting_character_ids":[],'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"language_lock":"en",'
                '"bgm_lock":{"mood":"...","style":"...","instruments":"...",'
                '"continuity":"same bed","rule":"non-vocal underscore"},'
                '"include_speech":true,"include_music":true,'
                '"storyboard_markdown":"...","notes":"...","target_shot_count":N}'
            )
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": approved_brief,
                        "style_lock": analysis.get("style_lock"),
                        "characters": characters,
                        "shots": shots,
                        "spatial_lock": meta.get("spatial_lock"),
                        "rule": "Multi-shot storyboard required when multiple beats exist.",
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for="quality",
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
                    cid = str(raw.get("id") or f"char_{i}").strip() or f"char_{i}"
                    name = str(raw.get("name") or cid).strip() or cid
                    cleaned_chars.append(
                        {
                            "id": cid,
                            "name": name,
                            "description": str(raw.get("description") or name)[:600],
                        }
                    )
                if cleaned_chars:
                    characters = cleaned_chars
                    analysis["characters"] = characters
                    notes = "Director LLM authored cast + storyboard."
            llm_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
            if llm_shots:
                cleaned: list[dict[str, Any]] = []
                for i, raw in enumerate(llm_shots, start=1):
                    if not isinstance(raw, dict):
                        continue
                    shot = dict(raw)
                    shot["shot_index"] = int(shot.get("shot_index") or i)
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
                            str(k): str(v)[:280]
                            for k, v in shot["speech_by_character"].items()
                            if str(v).strip()
                        }
                    if shot.get("speech_line"):
                        shot["speech_line"] = str(shot.get("speech_line"))[:500]
                    cleaned.append(shot)
                if cleaned:
                    shots = cleaned
                    analysis["shots"] = cleaned
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
            md_candidate = str(parsed.get("storyboard_markdown") or "").strip()
            if md_candidate and len(md_candidate) > 40:
                sb_md = md_candidate
                source = "llm"
                analysis["source"] = "llm"
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

        from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import apply_shot_scope

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        analysis = apply_shot_scope(analysis, user_prompt)
        shots = list(analysis.get("shots") or shots)
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
                ensure_shot_start_end_states,
                validate_storyboard_state_chain,
            )

            shots = ensure_shot_start_end_states(shots)
            analysis["shots"] = shots
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
            analysis["shots"] = shots
            meta["script_analysis"] = analysis
        if not sb_md:
            if source != "llm" or not shots:
                raise DesignerLlmError(
                    "Chat model did not return a usable storyboard.",
                    code=LLM_API_ERROR,
                )
            # LLM returned shots/cast but omitted markdown — draft hint only.
            sb_md = _write_storyboard_markdown(
                shots,
                characters,
                style_lock=(
                    analysis.get("style_lock")
                    if isinstance(analysis.get("style_lock"), dict)
                    else {}
                ),
            )

        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            ensure_visual_style_statement,
            synchronize_graph_style_from_brief,
        )

        sb_md = ensure_visual_style_statement(
            sb_md,
            analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {},
        )

        stamped = False
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "storyboard" and str(node.get("id") or "") != "n_storyboard":
                continue
            cfg.pop("skip_llm", None)
            cfg["planned_shots"] = shots
            cfg["kind"] = "agent"
            cfg["delegate"] = "agent"
            node["config"] = cfg
            stamped = True
            break
        if shots:
            analysis["target_shot_count"] = len(shots)
            meta["script_analysis"] = analysis
        # Propagate timelines/consistency/audio locks into frame/shot configs once.
        from jiuwenswarm.server.runtime.designer.audio_locks import stamp_audio_fields_on_clip_config

        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) not in {"frame", "clip", "keyframe"}:
                continue
            idx = int(cfg.get("shot_index") or 0)
            for shot in shots:
                if int(shot.get("shot_index") or 0) != idx:
                    continue
                if shot.get("action"):
                    cfg["shot_action"] = str(shot["action"])[:500]
                if shot.get("camera"):
                    cfg["camera"] = str(shot["camera"])[:120]
                if shot.get("timeline"):
                    cfg["timeline"] = str(shot["timeline"])[:40]
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
                        cfg["speech_line"] = str(shot.get("speech_line"))[:500]
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
        synchronize_graph_style_from_brief(graph, sb_md)
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
        graph: DesignerExecutionGraph,
        *,
        optimize_for: str = "quality",
    ) -> dict[str, Any]:
        """Director owns flexible multi-shot topology from Brief+Storyboard.

        Not a frozen single-keyframe template: LLM expands shots from the locked
        storyboard, then materializes one frame+shot agent per shot.
        """
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )
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
        shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
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
                "medium for every character, scene, keyframe, and clip. "
                "MUST include characters[] — every named human gets one solo identity "
                "card (id, name, description). NOT every character in every scene. "
                "MUST include shots[] grouped by setting_id (distinct places). "
                "Shots are consecutive TIME windows that concatenate to the film. "
                "Each shot.action is THAT window only — do not paste the user prompt "
                "into every clip. Preserve every distinct narrative/content shot from the "
                "approved storyboard; each action must advance the story or message, not repeat "
                "an earlier action as filler or alternate coverage. Preserve exact per-shot "
                "speech_by_character and speech_line from the storyboard. "
                "story window needs, with contiguous timelines. A duration the approved brief "
                "already gave a shot stays that length. Keep the brief's character names and "
                "wardrobe, place names, and continuity notes. Retain every storyboard "
                "clip that provides distinct content. "
                "YOU own target_shot_count (prefer ≤8, hard max 16). "
                "One clip = one continuous shot of that window's length. New KF on hard cut, new setting, "
                "wardrobe/prop change, or on-screen cast change. "
                "Qwen KF: lock identity+wardrobe; ≤2–3 people with refs; one variable "
                "per new KF. Honor explicit N-shot / N分镜 as a HARD ceiling. "
                "First KF of each setting: compose_from_solo_refs with on_screen + "
                "cast_actions (composer decides who appears and what they are doing). "
                "Later same setting: edit_prior_keyframe (architecture locked). "
                "offscreen stay out of frame. "
                "Scene specs ARE required — environment-only Qwen stills. "
                "Each shot: shot_index, timeline, camera, action, on_screen, offscreen, "
                "cast_actions, featured_cast_ids, ensemble_cast_ids, setting_id, "
                "keyframe_prompt, exiting_character_ids, keyframe_strategy, "
                "speech_by_character, speech_line. "
                "Schema: "
                '{"style_lock":{"look":"...","medium":"..."},'
                '"characters":[{"id":"char_1","name":"...","description":"..."}],'
                '"shots":[{"shot_index":1,"timeline":"<copy this shot timeline from the approved brief>",'
                '"action":"...",'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"target_shot_count":N,"include_speech":bool,'
                '"include_music":bool,"notes":"..."}'
            )
            from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
                infer_shot_budget,
            )

            shot_ceiling = infer_shot_budget(user_prompt, analysis)
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": approved_brief,
                        "approved_storyboard": approved_sb,
                        "style_lock": analysis.get("style_lock"),
                        "characters": characters,
                        "current_shots": shots,
                        "target_shot_count": shot_ceiling or analysis.get("target_shot_count"),
                        "rule": (
                            "Decide shot count wisely: prefer fewer; merge same-cast "
                            "continuous motion into one beat. Explicit N-shot / "
                            "target_shot_count from the user is a hard ceiling. Soft prefer "
                            "≤8 shots. All solo cast cards before keyframes; compose "
                            "first KF per setting_id; edit_prior only within the same "
                            "setting_id. Per-shot on_screen is authoritative for who "
                            "appears — not every solo in every frame."
                        ),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for=optimize_for,
                max_tokens=65536,
            )
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text)
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
                cid = str(raw.get("id") or f"char_{i}").strip() or f"char_{i}"
                name = str(raw.get("name") or cid).strip() or cid
                cleaned_chars.append(
                    {
                        "id": cid,
                        "name": name,
                        "description": str(raw.get("description") or name)[:600],
                    }
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
            llm_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
            cleaned: list[dict[str, Any]] = []
            for i, raw in enumerate(llm_shots, start=1):
                if not isinstance(raw, dict):
                    continue
                shot = dict(raw)
                shot["shot_index"] = int(shot.get("shot_index") or i)
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
                _HARD_MAX_SHOTS,
                _SOFT_MAX_SHOTS,
                _explicit_shot_count_from_prompt,
            )

            explicit_n = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
            try:
                llm_tsc = int(parsed.get("target_shot_count") or 0)
            except (TypeError, ValueError):
                llm_tsc = 0
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                needs_duration_slicing as _nds,
            )

            # LLM redesign owns N; explicit user language is the only hard ceiling
            # unless requested runtime exceeds Wan max (then sequential slice count).
            if _nds(user_prompt):
                cap = _HARD_MAX_SHOTS
                keep = min(cap, max(llm_tsc, explicit_n, len(shots), 1))
                shots = shots[:keep]
            elif explicit_n >= 1:
                shots = shots[: min(explicit_n, _HARD_MAX_SHOTS)]
            else:
                cap = min(_SOFT_MAX_SHOTS, _HARD_MAX_SHOTS)
                keep = min(cap, llm_tsc) if llm_tsc >= 1 else min(cap, len(shots))
                shots = shots[:keep]
            for i, sh in enumerate(shots, start=1):
                sh["shot_index"] = i
            analysis["target_shot_count"] = len(shots)
            audio = dict(analysis.get("audio") or {})
            if "include_speech" in parsed:
                audio["include_speech"] = bool(parsed.get("include_speech"))
            if "include_music" in parsed:
                audio["include_music"] = bool(parsed.get("include_music"))
            if audio.get("include_speech") and audio.get("include_music"):
                audio["policy"] = "speech_and_music"
            analysis["audio"] = audio
            analysis["scene_continuity_mode"] = "scene_card_plus_clip_shots"
        except DesignerLlmError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Director design_execution_graph LLM failed", exc_info=True)
            raise DesignerLlmError(
                f"Chat model request failed while designing the execution graph: {exc}",
                code=LLM_API_ERROR,
            ) from exc

        # Final clamp — soft max unless the user asked for an explicit count.
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
                _HARD_MAX_SHOTS,
                _SOFT_MAX_SHOTS,
                _explicit_shot_count_from_prompt,
                infer_shot_budget,
            )

            explicit_n = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                needs_duration_slicing,
            )

            if needs_duration_slicing(user_prompt):
                final_ceiling = min(
                    _HARD_MAX_SHOTS,
                    max(len(shots), int(infer_shot_budget(user_prompt, analysis) or 0), 1),
                )
            elif explicit_n >= 1:
                final_ceiling = min(explicit_n, _HARD_MAX_SHOTS)
            else:
                # Prefer live shot list length; soft-cap only.
                final_ceiling = min(
                    _SOFT_MAX_SHOTS,
                    max(len(shots), int(infer_shot_budget(user_prompt, analysis) or 0), 1),
                )
            if final_ceiling >= 1 and len(shots) > final_ceiling:
                shots = shots[:final_ceiling]
                for i, sh in enumerate(shots, start=1):
                    if isinstance(sh, dict):
                        sh["shot_index"] = i
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
        analysis["shots"] = shots
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                apply_shot_scope,
            )

            analysis = apply_shot_scope(analysis, user_prompt)
            shots = list(analysis.get("shots") or shots)
            analysis["shots"] = shots
        except Exception:  # noqa: BLE001
            pass
        analysis["target_shot_count"] = len(shots)
        meta["script_analysis"] = analysis

        # Cast shrink guard: never replace a richer solo cast with a thinner redesign.
        old_solos = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        new_humans = [
            c
            for c in characters
            if isinstance(c, dict)
            and c.get("id")
            and not c.get("is_prop")
            and str(c.get("cast_kind") or "") not in {"brand_mascot", "prop"}
        ]
        if old_solos > 1 and len(new_humans) < old_solos:
            logger.info(
                "design_execution_graph rejected cast shrink %s → %s; keeping current graph",
                old_solos,
                len(new_humans),
            )
            meta["director_composed_on_bootstrap"] = True
            meta["director_graph_ack"] = {
                "ok": True,
                "source": "kept_prior_cast",
                "notes": "Rejected redesign that would shrink solo cast.",
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
            optimize_for=optimize_for,
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
        rmeta["freeze_shot_topology"] = True
        rmeta["director_graph_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "shot_count": len(shots),
            "frame_nodes": sum(
                1
                for n in (rebuilt.get("nodes") or [])
                if str(n.get("id") or "").startswith("n_frame_")
            ),
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

    def decide_capabilities(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
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

    def review_leaf_media_prompt(
        self,
        graph: DesignerExecutionGraph,
        node: DesignerGraphNode,
    ) -> dict[str, Any]:
        """Gate every frame/shot media prompt against locks + prior handoff / already_done."""
        from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
            collect_prior_clip_prompts,
        )
        from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
            architecture_clause_from_bible,
            strip_prior_prompt_pastes,
        )

        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"frame", "keyframe", "clip", "character", "character_design", "scene"}:
            return {"patched": False, "notes": "skip_non_media"}
        shot_index = int(cfg.get("shot_index") or 0)
        gen = dict(cfg.get("generate") or {})
        prompt = strip_prior_prompt_pastes(str(gen.get("prompt") or cfg.get("prompt") or "").strip())
        notes: list[str] = []
        changed = False
        if prompt != str(gen.get("prompt") or cfg.get("prompt") or "").strip():
            notes.append("strip_prior_prompt_pastes")
            changed = True

        setting_id = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        strategy = str(
            cfg.get("keyframe_strategy")
            or (cfg.get("identity_refs") or {}).get("keyframe_strategy")
            or ""
        ).strip()
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        costume_lock = str(cfg.get("costume_lock") or identity.get("costume_lock") or "").strip()
        spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
        if not spatial:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            spatial = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
        solo_ids = [
            str(x)
            for x in (
                identity.get("character_node_ids")
                or cfg.get("character_node_ids")
                or []
            )
            if str(x)
        ]
        prior_kf_id = str(
            cfg.get("prior_keyframe_node_id") or identity.get("prior_keyframe_node_id") or ""
        ).strip()

        # Enforce setting-locked compose + scene specs before any media tool call.
        if role in {"frame", "keyframe"}:
            strategy = "compose_from_solo_refs"
            cfg["keyframe_strategy"] = strategy
            specs = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else None
            if not specs:
                meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
                locks = meta.get("scene_locks") if isinstance(meta.get("scene_locks"), dict) else {}
                if isinstance(locks.get(setting_id), dict):
                    specs = dict(locks[setting_id])
                    cfg["scene_specs"] = specs
            handoff = str(
                cfg.get("scene_prompt_handoff_from")
                or identity.get("scene_prompt_handoff_from")
                or prior_kf_id
                or ""
            ).strip()
            if "compose" not in prompt.lower() and "STRATEGY=compose_from_solo_refs" not in prompt:
                prompt = (
                    prompt
                    + f"\nLOCK: STRATEGY=compose_from_solo_refs setting={setting_id}. "
                    f"Compose solo sheets {', '.join(solo_ids) or 'all cast solos'} "
                    "INTO the locked scene specs — keep architecture across views."
                )
                notes.append("enforce_compose_solos_lock")
                changed = True
            if specs and "SCENE SPECS" not in prompt:
                prompt = (
                    prompt
                    + f"\nSCENE SPECS: scene={specs.get('scene_name') or specs.get('place')}; lighting={specs.get('lighting')}; "
                    f"objects={', '.join(str(x) for x in (specs.get('objects') or [])[:6])}; "
                    f"crowd={specs.get('crowd')}; coherence={specs.get('coherence_rule')}; "
                    f"active_view={cfg.get('view_key') or specs.get('active_view')}."
                )
                notes.append("enforce_scene_specs")
                changed = True
            if handoff and "SCENE PROMPT HANDOFF" not in prompt:
                arch = str(cfg.get("scene_architecture_clause") or "").strip() or architecture_clause_from_bible(specs)
                prompt = (
                    prompt
                    + f"\nSCENE PROMPT HANDOFF from {handoff}: "
                    + (arch or "reuse scene architecture only")
                    + " — change only camera view + on_screen cast/actions."
                )
                notes.append("enforce_scene_prompt_handoff")
                changed = True
            # Architecture only — never paste full master action prompt.
            arch = str(cfg.get("scene_architecture_clause") or "").strip()
            if not arch:
                arch = architecture_clause_from_bible(specs)
            master_prompt = str(cfg.get("scene_master_prompt") or "").strip()
            if master_prompt and (
                "Primary action" in master_prompt
                or "PRIOR KEYFRAME" in master_prompt
                or len(master_prompt) > 900
            ):
                # Contaminated full prompt — replace with architecture.
                master_prompt = arch
                cfg["scene_master_prompt"] = arch[:900] if arch else ""
                notes.append("slim_contaminated_scene_master_prompt")
                changed = True
            if arch and "SCENE ARCHITECTURE LOCK" not in prompt and "MASTER SCENE PROMPT" not in prompt:
                prompt = prompt + "\n" + arch
                notes.append("inject_scene_architecture")
                changed = True
            elif master_prompt and "SCENE ARCHITECTURE LOCK" not in prompt and "MASTER SCENE PROMPT" not in prompt:
                # Only allow if it already looks like an architecture clause.
                if master_prompt.startswith("SCENE ARCHITECTURE") or "scene=" in master_prompt[:80]:
                    prompt = prompt + "\n" + master_prompt[:900]
                    notes.append("inject_scene_architecture_from_master")
                    changed = True

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

        if solo_ids and "IDENTITY" not in prompt and "solo" not in prompt.lower():
            prompt = prompt + f"\nIDENTITY solo sheets (do not invent faces): {', '.join(solo_ids)}"
            notes.append("inject_identity_solos")
            changed = True

        # Prior keyframe: short consistency note only (never paste full prior generate.prompt).
        prior_kf_action = str(cfg.get("previous_keyframe_action") or "").strip()
        prior_kf_prompt = str(cfg.get("previous_keyframe_prompt") or "").strip()
        if not prior_kf_action and shot_index > 1:
            prev_id = prior_kf_id or f"n_frame_{shot_index - 1}"
            for n in graph.get("nodes") or []:
                if str(n.get("id") or "") != prev_id:
                    continue
                pcfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                prior_kf_action = str(
                    pcfg.get("shot_action")
                    or pcfg.get("character_action")
                    or ""
                ).strip()
                prior_kf_prompt = str(
                    pcfg.get("last_approved_prompt")
                    or (pcfg.get("generate") or {}).get("prompt")
                    or ""
                ).strip()
                if prior_kf_action or prior_kf_prompt:
                    if prior_kf_action:
                        cfg["previous_keyframe_action"] = prior_kf_action[:300]
                    # Soft-dep readiness marker — do not inject full text into prompt.
                    if prior_kf_prompt and not str(cfg.get("previous_keyframe_prompt") or "").strip():
                        cfg["previous_keyframe_prompt"] = prior_kf_prompt[:800]
                    cfg["previous_keyframe_node_id"] = prev_id
                    notes.append("pull_prior_keyframe_action")
                    changed = True
                break
        if (
            (prior_kf_action or prior_kf_prompt)
            and "PREVIOUS KEYFRAME HAD" not in prompt
            and "PRIOR KEYFRAME PROMPT" not in prompt
        ):
            from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
                keyframe_continuity_note,
            )

            kf_note = keyframe_continuity_note(
                shot_index=shot_index,
                this_action=str(cfg.get("shot_action") or cfg.get("character_action") or ""),
                this_camera=str(cfg.get("camera") or ""),
                prior_action=prior_kf_action or prior_kf_prompt[:220],
                prior_shot_index=shot_index - 1 if shot_index > 1 else None,
            )
            if kf_note:
                prompt = prompt + "\n\n" + kf_note
                notes.append("inject_prior_keyframe_note")
                changed = True

        already_done = [str(x) for x in (cfg.get("already_done") or []) if str(x)]
        if not already_done:
            analysis = (graph.get("metadata") or {}).get("script_analysis") or {}
            for s in analysis.get("shots") or []:
                if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index:
                    already_done = [str(x) for x in (s.get("already_done") or []) if str(x)]
                    cfg["already_done"] = already_done
                    break
        if already_done:
            cfg["already_done"] = already_done

        occupancy = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
        if not occupancy and isinstance(identity.get("occupancy"), dict):
            occupancy = dict(identity["occupancy"])
        config_on_screen = [
            str(x)
            for x in (
                cfg.get("on_screen")
                or occupancy.get("must_appear")
                or cfg.get("character_ids")
                or []
            )
            if str(x)
        ]
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
        if role in {"frame", "keyframe", "clip"} and config_on_screen:
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
            # Keep solo_ids aligned to on_screen only.
            solo_by_cid = {
                str(c): sid
                for c, sid in zip(
                    cfg.get("character_ids") or [],
                    solo_ids,
                )
                if str(c)
            }
            # Prefer resolving from graph solos when available.
            for other in graph.get("nodes") or []:
                if not isinstance(other, dict):
                    continue
                oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                if _role_key(other) not in {"character", "character_design"}:
                    continue
                if oc.get("combined_cast"):
                    continue
                cids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                if len(cids) == 1:
                    solo_by_cid[cids[0]] = str(other.get("id") or "")
            aligned = [solo_by_cid[c] for c in config_on_screen if c in solo_by_cid]
            if aligned and aligned != solo_ids:
                solo_ids = aligned
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
            for s in analysis0.get("shots") or []:
                if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index:
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
                    prior_action=str(cfg.get("previous_clip_action") or ""),
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
            from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
                ensure_cfg_staging_locks,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            shot_row = next(
                (
                    s
                    for s in (
                        ((graph.get("metadata") or {}).get("script_analysis") or {}).get("shots")
                        or []
                    )
                    if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index
                ),
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

        # Film-wide aspect + style locks (Director enforces; leaves cannot drop).
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
        aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
        if not aspect:
            aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        if not aspect:
            aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
        if not aspect:
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                    infer_aspect_lock,
                )

                aspect = infer_aspect_lock(
                    str(graph.get("description") or meta.get("user_prompt") or "")
                )
            except Exception:  # noqa: BLE001
                aspect = {}
        style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
        if not style:
            style = meta.get("style_lock") if isinstance(meta.get("style_lock"), dict) else {}
        if not style:
            style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}

        if aspect:
            cfg["aspect_lock"] = aspect
            if role in {"frame", "keyframe", "character", "character_design", "scene"}:
                img_size = str(aspect.get("image_size") or cfg.get("image_size") or "1K").strip()
                if str(cfg.get("image_size") or "") != img_size:
                    cfg["image_size"] = img_size
                    notes.append("stamp_image_size_aspect")
                    changed = True
            if role == "clip":
                from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                    video_format_for_node,
                )

                vsize, vres = video_format_for_node(graph, cfg, aspect if isinstance(aspect, dict) else None)
                if str(cfg.get("video_size") or "") != vsize or str(cfg.get("video_resolution") or "") != vres:
                    cfg["video_size"] = vsize
                    cfg["video_resolution"] = vres
                    notes.append("stamp_video_resolution")
                    changed = True
            ratio = str(aspect.get("ratio") or "").strip()
            rule = str(aspect.get("rule") or "").strip()
            if "ASPECT LOCK" not in prompt and (rule or ratio):
                prompt = (
                    prompt
                    + f"\nASPECT LOCK: {ratio or 'film ratio'} — "
                    + (rule or f"keep {ratio} on every still and shot; never change mid-film.")
                )
                notes.append("inject_aspect_lock")
                changed = True

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
            # Scene specs + setting isolation for shots (same locks as keyframes).
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
                    + f"\nSETTING LOCK: animate only `{setting_id}` from THIS shot's keyframe; "
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
            prior_shots = collect_prior_clip_prompts(graph, shot_index=shot_index)
            if not prior_shots and str(cfg.get("previous_clip_action") or "").strip():
                prior_shots = [
                    {
                        "node_id": str(cfg.get("previous_clip_node_id") or ""),
                        "shot_index": int(
                            cfg.get("previous_clip_shot_index") or max(1, shot_index - 1)
                        ),
                        "shot_action": str(cfg.get("previous_clip_action") or ""),
                        "speech_line": str(cfg.get("previous_clip_speech") or ""),
                    }
                ]
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
                    apply_continuity_contract,
                    prompt_violates_continuity,
                )

                cfg, prompt, cont_notes = apply_continuity_contract(
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
                if prompt_violates_continuity(prompt, cfg=cfg):
                    prompt = ""
                    notes.append("reject_continuity_violation_blank_prompt")
                    changed = True
            except Exception:  # noqa: BLE001
                pass
            # Keep prior_shots available for debug / regenerate packets only.
            if prior_shots:
                cfg["prior_storyboard_shots"] = [
                    {
                        "shot_index": p.get("shot_index"),
                        "shot_action": p.get("shot_action"),
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
                str(x)
                for x in (
                    cfg.get("on_screen")
                    or (cfg.get("occupancy") or {}).get("must_appear")
                    or cfg.get("character_ids")
                    or []
                )
                if str(x)
            )
            if "CHARACTER CONSISTENCY" not in prompt:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_reference_binding import (
                    clip_uses_scene_card,
                )

                if clip_uses_scene_card(cfg, graph if isinstance(graph, dict) else {}):
                    prompt = (
                        prompt
                        + "\nCHARACTER CONSISTENCY LOCK: animate ONLY on-screen people from "
                        "solo sheets bound as character1/character2… (attach order). "
                        "The LAST reference is the Scene specs (environment) — not a person. "
                        f"Keep faces, body types, ages, and costumes"
                        f"{(' for ' + cast_who) if cast_who else ''}. "
                        "Do not recast, redesign wardrobe, or invent unlabeled extras."
                    )
                else:
                    prompt = (
                        prompt
                        + "\nCHARACTER CONSISTENCY LOCK: animate ONLY people already in Image 1 "
                        "(this shot's keyframe); keep the same faces, body types, ages, and "
                        f"costumes{(' for ' + cast_who) if cast_who else ''}. "
                        "Do not recast, redesign wardrobe, or invent a different hero. "
                        "IDENTITY solo sheets remain the face authority."
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
            action = str(cfg.get("shot_action") or "").strip()
            if action and f"Primary action for shot {shot_index}" not in prompt:
                prompt = prompt + f"\nPrimary action for shot {shot_index}: {action}"
                notes.append("inject_this_shot_action")
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
                    for s in (analysis_a.get("shots") or [])
                    if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index
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
                from jiuwenswarm.server.runtime.designer.pipeline.clip_continuity_contract import (
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
            if block and ("LANGUAGE LOCK" not in prompt or "SPEECH LOCK" not in prompt or "BGM LOCK" not in prompt):
                # Replace soft fragments with full lock block once.
                if "LANGUAGE LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("inject_audio_locks")
                    changed = True
                elif "SPEECH LOCK" not in prompt or "BGM LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("reinforce_audio_locks")
                    changed = True

        # Soft anti-repeat for stills only. Shot video calls stay positive — director
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

                    tod = infer_time_of_day_lock(
                        str(graph.get("description") or meta_tod.get("user_prompt") or ""),
                        str(
                            (cfg.get("scene_specs") or {}).get("scene_name") or (cfg.get("scene_specs") or {}).get("place")
                            or cfg.get("shot_action")
                            or ""
                        ),
                    )
                    shot_tod = next(
                        (
                            s.get("time_of_day_lock")
                            for s in (analysis_tod.get("shots") or [])
                            if isinstance(s, dict)
                            and int(s.get("shot_index") or 0) == shot_index
                            and isinstance(s.get("time_of_day_lock"), dict)
                        ),
                        None,
                    )
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
                shot_index=shot_index,
                action=str(cfg.get("shot_action") or ""),
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
        # Stills + shots: positive practice prompts only (no LOCK essays on tool body).
        if role in {"scene", "frame", "keyframe", "character", "character_design"}:
            meta_l = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            analysis_l = (
                meta_l.get("script_analysis")
                if isinstance(meta_l.get("script_analysis"), dict)
                else {}
            )
            tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
            if not tod:
                shot_tod = next(
                    (
                        s.get("time_of_day_lock")
                        for s in (analysis_l.get("shots") or [])
                        if isinstance(s, dict)
                        and int(s.get("shot_index") or 0) == shot_index
                        and isinstance(s.get("time_of_day_lock"), dict)
                    ),
                    None,
                )
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

                    tod = infer_time_of_day_lock(
                        str(graph.get("description") or meta_l.get("user_prompt") or ""),
                        str((cfg.get("scene_specs") or {}).get("scene_name") or (cfg.get("scene_specs") or {}).get("place") or cfg.get("shot_action") or ""),
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
            # Fidelity: rewrite lock essays into positive still prompts; soft-fill gaps.
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
                    ensure_still_tool_prompt,
                )

                approved_still, still_notes = ensure_still_tool_prompt(
                    prompt,
                    role=role,
                    cfg=cfg,
                    graph=graph if isinstance(graph, dict) else {},
                )
                if approved_still != str(prompt or "").strip():
                    prompt = approved_still
                    notes.extend(still_notes or ["still_prompt_fidelity"])
                    changed = True
                cfg["director_still_prompt_notes"] = still_notes
                cfg["director_still_prompt_approved"] = True
            except Exception:  # noqa: BLE001
                pass

        # Stamp configured backend prompt budgets onto the leaf (never forces a model).
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
                media_prompt_limit_packet,
                resolve_prompt_limit,
                trim_prompt_to_limit,
            )

            packet = media_prompt_limit_packet()
            cfg["media_prompt_limits"] = packet
            kind = "video" if role == "clip" else "image"
            lim = resolve_prompt_limit(kind)
            cfg["prompt_char_limit"] = lim.max_chars
            cfg["prompt_limit_guidance"] = lim.guidance()
            if lim.max_chars and len(prompt) > lim.max_chars:
                trimmed, did = trim_prompt_to_limit(prompt, lim)
                if did:
                    prompt = trimmed
                    notes.append(f"trim_to_prompt_limit:{lim.max_chars}")
                    changed = True
                else:
                    notes.append(
                        f"prompt_over_advisory_limit:{len(prompt)}>{lim.max_chars}"
                    )
        except Exception:  # noqa: BLE001
            packet = {}
            lim = None

        cfg["director_prompt_reviewed"] = True
        cfg["director_lock_gate"] = {
            "setting_id": setting_id,
            "keyframe_strategy": strategy or cfg.get("keyframe_strategy"),
            "costume_lock": bool(costume_lock),
            "spatial_lock": bool(spatial),
            "aspect_lock": bool(aspect),
            "style_lock": bool(style),
            "aspect_ratio": (aspect or {}).get("ratio"),
            "image_size": cfg.get("image_size"),
            "video_size": cfg.get("video_size"),
            "video_resolution": cfg.get("video_resolution"),
            "solo_ids": solo_ids,
            "prior_keyframe_node_id": prior_kf_id or None,
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
        gen["prompt"] = prompt.strip()
        cfg["generate"] = gen
        # Character/scene leaves read cfg.prompt; keep both in sync after Director gate.
        store_cap = max(6000, int(cfg.get("prompt_char_limit") or 6000))
        if role in {"character", "character_design", "scene"} or not str(cfg.get("prompt") or "").strip():
            cfg["prompt"] = prompt.strip()[:store_cap]
        approved_cap = max(4000, int(cfg.get("prompt_char_limit") or 4000))
        if changed:
            cfg["last_approved_prompt"] = prompt.strip()[:approved_cap]
        else:
            cfg.setdefault("last_approved_prompt", prompt.strip()[:approved_cap])
        node["config"] = cfg
        nid = str(node.get("id") or "")
        for n in graph.get("nodes") or []:
            if str(n.get("id") or "") == nid:
                n["config"] = cfg
                break
        return {
            "patched": changed,
            "notes": notes,
            "shot_index": shot_index,
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
        # Film-wide aspect/style/axis locks on every still + shot node.
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

    def ensure_agents_and_prune(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
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

    def _enforce_leaf_agents(self, graph: DesignerExecutionGraph) -> list[str]:
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
            model_text_or_raise,
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
            )
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "brief": brief[:12000],
                        "characters": characters,
                        "shots": analysis.get("shots"),
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for="quality",
                max_tokens=32768,
            )
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text)
            if parsed is None:
                raise DesignerLlmError(
                    "Chat model did not return valid brief review JSON.",
                    code=LLM_API_ERROR,
                )
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
        *,
        node_states: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """After storyboard completes: fidelity + enhancement + consistency (one shot, no loop)."""
        meta = dict(graph.get("metadata") or {})
        if meta.get("storyboard_reviewed"):
            return dict(meta.get("director_storyboard_ack") or {"ok": True, "skipped": True})
        analysis = dict(meta.get("script_analysis") or {}) if isinstance(meta.get("script_analysis"), dict) else {}
        shots = list(analysis.get("shots") or [])
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
            action = str(shot.get("action") or shot.get("keyframe_prompt") or "").lower()
            if any(m in action for m in leave_markers):
                left_chars.extend([str(x) for x in (shot.get("character_ids") or [])])
        left_chars = list(dict.fromkeys(left_chars))
        patched: list[str] = []
        for shot in shots:
            idx = int(shot.get("shot_index") or 0)
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
            model_text_or_raise,
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
                "speech), per-shot speech_by_character (exact lines or {} if silent), and "
                "film-wide bgm_lock. Respond JSON only: "
                '{"ok":true,"shot_fixes":[{"shot_index":1,"action":"...","camera":"...",'
                '"timeline":"<copy this shot timeline from the approved brief>",'
                '"continuity_lock":{"forbid":"..."},'
                '"character_ids":["char_1"],'
                '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                '"language_lock":"en",'
                '"bgm_lock":{"mood":"...","style":"...","rule":"..."},'
                '"include_speech":true,"include_music":true,"notes":"..."}'
            )
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt,
                        "approved_brief": str(meta.get("approved_brief") or "")[:12000],
                        "approved_storyboard": str(
                            meta.get("approved_storyboard") or ""
                        )[:16000],
                        "shots": shots,
                        "brief_hint": meta.get("director_brief_notes") or "",
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for="quality",
                max_tokens=32768,
            )
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text)
            if parsed is None:
                raise DesignerLlmError(
                    "Chat model did not return valid storyboard review JSON.",
                    code=LLM_API_ERROR,
                )
            for fix in parsed.get("shot_fixes") or []:
                if not isinstance(fix, dict):
                    continue
                try:
                    idx = int(fix.get("shot_index") or 0)
                except (TypeError, ValueError):
                    continue
                for shot in shots:
                    if int(shot.get("shot_index") or 0) != idx:
                        continue
                    for key in ("action", "camera", "timeline", "keyframe_prompt", "speech_line"):
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
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import apply_shot_scope

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        analysis = apply_shot_scope(analysis, user_prompt)
        shots = list(analysis.get("shots") or shots)
        meta["language_lock"] = str(analysis.get("language_lock") or meta.get("language_lock") or "en")
        if isinstance(analysis.get("bgm_lock"), dict):
            meta["bgm_lock"] = analysis["bgm_lock"]
        patched.append("audio_locks_approved")

        if shots:
            analysis["shots"] = shots
            meta["script_analysis"] = analysis
            from jiuwenswarm.server.runtime.designer.smart_graph import (
                _write_storyboard_markdown,
            )

            meta["approved_storyboard"] = _write_storyboard_markdown(
                shots,
                list(analysis.get("characters") or []),
                style_lock=(
                    analysis.get("style_lock")
                    if isinstance(analysis.get("style_lock"), dict)
                    else {}
                ),
            )
            # Patch storyboard + downstream frame/clip configs once.
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
                    if role not in {"frame", "clip", "keyframe"}:
                        continue
                    idx = int(cfg.get("shot_index") or 0)
                    for shot in shots:
                        if int(shot.get("shot_index") or 0) != idx:
                            continue
                        if shot.get("action"):
                            cfg["shot_action"] = str(shot["action"])[:500]
                        if shot.get("camera"):
                            cfg["camera"] = str(shot["camera"])[:120]
                        if shot.get("timeline"):
                            cfg["timeline"] = str(shot["timeline"])[:40]
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
            "(4) brief/storyboard are comprehensive enough for keyframe and shot prompting, "
            "(5) SPATIAL CONSISTENCY: motion + geography — landmarks/layout/light must "
            "match the master scene specs across shot views (edit/ref, not new buildings), "
            "(6) IDENTITY CONSISTENCY: every frame/shot must reference canonical SOLO character "
            "sheets (identity_refs.character_node_ids), not reinvent costumes, "
            "(7) GRAPH USEFULNESS: every node must be useful for the final compose shot — "
            "list prune_ids for unused/orphan nodes; after prune the remaining graph must stay "
            "coherent (master scene → shot views → frames → shots → compose). "
            "Respond JSON only: "
            '{"ok":true|false,"issues":["..."],"prune_ids":["n_unused"],'
            '"spatial_lock":{"landmarks":"...","layout":"...","light":"...","static_rule":"..."},'
            '"shot_fixes":[{"shot_index":1,"character_ids":["char_1"],'
            '"action":"...","continuity_lock":{"motion":"...","facing":"...","forbid":"..."},'
            '"costume_lock":"...","camera":"..."}],"notes":"..."}'
        )
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
                        "shot_action": (n.get("config") or {}).get("shot_action"),
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
                optimize_for="quality",
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

        # Apply one-shot shot_fixes into analysis + frame/shot configs (no re-loop).
        fixes = parsed.get("shot_fixes") if isinstance(parsed.get("shot_fixes"), list) else []
        applied: list[str] = []
        analysis = dict(analysis) if isinstance(analysis, dict) else {}
        shots = list(analysis.get("shots") or [])
        id_to_name = {
            str(c.get("id")): str(c.get("name") or c.get("id"))
            for c in (analysis.get("characters") or [])
            if isinstance(c, dict) and c.get("id")
        }
        for fix in fixes:
            if not isinstance(fix, dict):
                continue
            try:
                idx = int(fix.get("shot_index") or 0)
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
                if int(shot.get("shot_index") or 0) != idx:
                    continue
                if cids:
                    shot["character_ids"] = cids
                if action:
                    shot["action"] = action[:500]
                    shot["keyframe_prompt"] = action[:600]
                if lock:
                    shot["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                    clause = _continuity_prompt_clause(shot["continuity_lock"])
                    kf = str(shot.get("keyframe_prompt") or "")
                    if clause and "CONSISTENCY LOCK" not in kf:
                        shot["keyframe_prompt"] = (kf + clause)[:700]
                if camera:
                    shot["camera"] = camera[:120]
                if costume:
                    shot["costume_lock"] = costume[:480]
                applied.append(f"shot {idx} llm-fix")
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if _role_key(node) not in {"frame", "clip", "keyframe"}:
                    continue
                if int(cfg.get("shot_index") or 0) != idx:
                    continue
                if cids:
                    cfg["character_ids"] = cids
                    cfg["cast_names"] = [id_to_name.get(cid, cid) for cid in cids]
                if action:
                    cfg["shot_action"] = action[:500]
                if camera:
                    cfg["camera"] = camera[:120]
                if costume:
                    cfg["costume_lock"] = costume[:480]
                if lock:
                    cfg["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                    gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
                    prompt = str(gen.get("prompt") or cfg.get("shot_action") or "")
                    clause = _continuity_prompt_clause(cfg["continuity_lock"])
                    if clause and "CONSISTENCY LOCK" not in prompt:
                        gen["prompt"] = (prompt + clause)[:1200]
                        cfg["generate"] = gen
                node["config"] = cfg
        if shots:
            analysis["shots"] = shots
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
            str(n.get("id"))
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict)
            and (
                str(n.get("id") or "").startswith("n_clip")
                or str(n.get("id") or "").startswith("n_frame")
                or str(n.get("id") or "") in {"n_scene", "n_compose", "n_speech", "n_music"}
                or _role_key(n)
                in {"clip", "frame", "keyframe", "compose", "speech", "music"}
            )
        }
        from jiuwenswarm.server.runtime.designer.user_references import (
            is_user_reference_node,
        )

        reference_ids = {
            str(n.get("id"))
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and is_user_reference_node(n)
        }
        prune_ids = [
            str(x)
            for x in (parsed.get("prune_ids") or [])
            if str(x).strip()
            and str(x) not in protected
            and str(x) not in reference_ids
            and str(x) != "n_compose"
        ]
        if prune_ids:
            drop = set(prune_ids)
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if not isinstance(n, dict) or str(n.get("id")) not in drop
            ]
            graph["edges"] = [
                e
                for e in (graph.get("edges") or [])
                if str(e.get("source") or "") not in drop
                and str(e.get("target") or "") not in drop
            ]
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

    def ack_keyframe_adjustment(
        self, graph: DesignerExecutionGraph, director_notes: list[str]
    ) -> dict[str, Any]:
        ack = {
            "ok": True,
            "director_notes": director_notes[:20],
            "notes": "Director accepted post-keyframe shot adjustments (once).",
            "rating_modality": str(
                (graph.get("metadata") or {}).get("rating_modality") or "text_only"
            ),
        }
        meta = dict(graph.get("metadata") or {})
        meta["director_keyframe_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def review(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_plan: dict[str, Any] | None,
        prior_feedback: dict[str, Any] | None,
        optimize_for: str,
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
                    "consistent with a cinematic still? Reply in 2 short sentences; "
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
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "optimize_for": optimize_for,
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
            optimize_for="quality",
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

    async def dual_rate_final(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_final: dict[str, Any],
        director_review: dict[str, Any],
        node_states: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Assign two independent rating agents; aggregate for Run-again feedback only."""
        base_payload = {
            "user_prompt": graph.get("description"),
            "node_ids": [str(n.get("id")) for n in (graph.get("nodes") or []) if n.get("id")],
            "agent_feedback_keys": list((agent_feedback or {}).keys())[:40],
            "director_authoring_summary": (director_final or {}).get("summary"),
            "director_review_summary": (director_review or {}).get("summary"),
            "compose_status": ((node_states or {}).get("n_compose") or {}).get("status"),
        }
        system = (
            "You are an independent Rater Agent for a Designer film pipeline. "
            "Strict Hollywood bar: score overall 0-10 (floats ok). "
            "Rate fidelity to user prompt, identity consistency, motion honesty "
            "(real video not stills), graph design, and per-node prompt/tool quality. "
            "Respond JSON only: "
            '{"overall":0-10,"node_scores":{"<id>":0-10},'
            '"feedback_nodes":{"<id>":"..."},'
            '"director_authoring_feedback":"...","director_review_feedback":"...","graph_design":"..."}'
        )
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
            model_text_or_raise,
        )

        raters: list[dict[str, Any]] = []
        for label in ("rater_a", "rater_b"):
            try:
                result = await call_model_tool(
                    prompt=json.dumps({**base_payload, "rater_id": label}, ensure_ascii=False),
                    system=system,
                    optimize_for="quality",
                    max_tokens=8192,
                )
                rtext = model_text_or_raise(result)
                parsed = _extract_json_object(rtext) or {}
                if not parsed:
                    raise DesignerLlmError(
                        f"Chat model did not return usable dual-rater output ({label}).",
                        code=LLM_API_ERROR,
                    )
                raters.append(
                    {
                        "id": label,
                        "overall": float(parsed.get("overall") or 0),
                        "node_scores": parsed.get("node_scores")
                        if isinstance(parsed.get("node_scores"), dict)
                        else {},
                        "feedback_nodes": parsed.get("feedback_nodes")
                        if isinstance(parsed.get("feedback_nodes"), dict)
                        else {},
                        "director_authoring_feedback": str(parsed.get("director_authoring_feedback") or "")[:800],
                        "director_review_feedback": str(parsed.get("director_review_feedback") or "")[:800],
                        "graph_design": str(parsed.get("graph_design") or "")[:800],
                        "model": result.get("model"),
                    }
                )
            except DesignerLlmError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("Dual rater %s failed", label, exc_info=True)
                raise DesignerLlmError(
                    f"Chat model request failed for dual rater {label}: {exc}",
                    code=LLM_API_ERROR,
                ) from exc
        if not raters:
            raise DesignerLlmError(
                "Chat model did not return dual-rater feedback.",
                code=LLM_API_ERROR,
            )
        overalls = [float(r.get("overall") or 0) for r in raters]
        agg_overall = sum(overalls) / max(1, len(overalls))
        # Merge node feedback from both raters.
        feedback_nodes: dict[str, str] = {}
        for r in raters:
            for nid, text in (r.get("feedback_nodes") or {}).items():
                prev = feedback_nodes.get(str(nid), "")
                chunk = str(text or "").strip()
                if not chunk:
                    continue
                feedback_nodes[str(nid)] = (prev + " | " + chunk).strip(" |")[:1200]
        aggregated = {
            "raters": raters,
            "aggregated_overall": round(agg_overall, 2),
            "feedback_nodes": feedback_nodes,
            "director_authoring_feedback": " || ".join(
                str(r.get("director_authoring_feedback") or "") for r in raters
            )[:1600],
            "director_review_feedback": " || ".join(
                str(r.get("director_review_feedback") or "") for r in raters
            )[:1600],
            "graph_design": " || ".join(str(r.get("graph_design") or "") for r in raters)[:1600],
            "apply_on": "run_again_only",
        }
        meta = dict(graph.get("metadata") or {})
        meta["dual_rater_aggregate"] = aggregated
        graph["metadata"] = meta
        # Fold into director review suggestions for persistence.
        suggestions = dict(director_review.get("suggestions") or {})
        suggestions.update(feedback_nodes)
        if aggregated["director_authoring_feedback"]:
            suggestions["director_authoring"] = aggregated["director_authoring_feedback"]
        if aggregated["graph_design"]:
            suggestions["graph_design"] = aggregated["graph_design"]
        recommendations = [
            str(aggregated.get("director_authoring_feedback") or "").strip(),
            str(aggregated.get("director_review_feedback") or "").strip(),
            str(aggregated.get("graph_design") or "").strip(),
            *[f"{nid}: {txt}" for nid, txt in list(feedback_nodes.items())[:12]],
        ]
        aggregated["aggregated_recommendations"] = [r for r in recommendations if r][:20]
        director_review = dict(director_review)
        director_review["suggestions"] = suggestions
        director_review["dual_raters"] = aggregated
        director_review["aggregated_recommendations"] = aggregated["aggregated_recommendations"]
        director_review["aggregated_overall"] = aggregated["aggregated_overall"]
        return director_review

    async def assign_dual_raters(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_final: dict[str, Any],
        director_review: dict[str, Any],
        node_states: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Public alias: two independent raters → dual_raters + aggregated_recommendations."""
        return await self.dual_rate_final(
            graph,
            agent_feedback=agent_feedback,
            director_final=director_final,
            director_review=director_review,
            node_states=node_states,
        )


    async def finalize(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        director_review: dict[str, Any],
        optimize_for: str,
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
                    "Does this image match a usable cinematic reference/keyframe for a short film? "
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
        )
        prompt = json.dumps(
            {
                "agent_feedback": agent_feedback,
                "director_review": director_review,
                "optimize_for": optimize_for,
                "user_prompt": graph.get("description"),
                "vision_notes": vision_notes,
                "rating_modality": (graph.get("metadata") or {}).get("rating_modality"),
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            optimize_for="quality",
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
    """Ensure each shot/frame has unique shot_action + generate prompt (text-only)."""
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
        if role not in {"frame", "clip", "keyframe"}:
            continue
        idx = int(cfg.get("shot_index") or 0) or 1
        action = str(cfg.get("shot_action") or "").strip()
        camera = str(cfg.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        cfg["camera"] = camera
        if not action:
            timeline = str(cfg.get("timeline") or "").strip()
            action = (
                f"Film only the {timeline or f'shot {idx}'} time window. "
                "Do not restage another window from a new camera angle."
            )
            cfg["shot_action"] = action
            notes.append(f"{node.get('id')}: filled missing shot_action from time window")
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "").strip()
        marker = f"shot {idx}"
        cast_names = [str(x) for x in (cfg.get("cast_names") or []) if str(x).strip()]
        cast_who = ", ".join(cast_names)
        lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else None
        if not lock and action:
            lock = _infer_continuity_lock(action)
            cfg["continuity_lock"] = lock
        clause = _continuity_prompt_clause(lock if isinstance(lock, dict) else None)
        if not prompt or marker not in prompt.lower():
            gen["prompt"] = (
                f"Film {marker} only. Camera {camera}. Action: {action}. "
                + (f"Focus cast on screen: {cast_who}. " if cast_who else "")
                + "Must differ from sibling shots."
                + clause
            )
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: refreshed generate.prompt")
        elif clause and "CONSISTENCY LOCK" not in prompt:
            gen["prompt"] = (prompt + clause)[:1200]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: appended consistency lock")
        if role == "clip":
            cfg["max_video_calls"] = 1
            if idx > 1 and not cfg.get("continuity_frame_node_id"):
                cfg["continuity_frame_node_id"] = f"n_frame_{idx - 1}"
                notes.append(f"{node.get('id')}: linked consistency from prior keyframe")
        if role in {"frame", "keyframe"}:
            cfg["max_image_calls"] = 1
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
    for shot in analysis.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        blob = f"{shot.get('action') or ''} {shot.get('keyframe_prompt') or ''}"
        focus = _focus_character_ids(blob, characters)
        old = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        if focus and focus != old:
            old_set, new_set = set(old), set(focus)
            if old and old_set.issubset(new_set) and len(new_set) > len(old_set):
                continue
            notes.append(
                f"analysis shot {shot.get('shot_index')}: "
                f"{shot.get('character_ids')} -> {focus}"
            )
            shot["character_ids"] = focus
    meta["script_analysis"] = analysis

    # Refresh approved storyboard so the handler writes the corrected focus cast.
    try:
        from jiuwenswarm.server.runtime.designer.smart_graph import _write_storyboard_markdown

        planned = list(analysis.get("shots") or [])
        if planned:
            sb_md = _write_storyboard_markdown(
                planned,
                characters,
                style_lock=(
                    analysis.get("style_lock")
                    if isinstance(analysis.get("style_lock"), dict)
                    else {}
                ),
            )
            meta["approved_storyboard"] = sb_md
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
        if role not in {"frame", "clip", "keyframe"}:
            continue
        blob = f"{cfg.get('shot_action') or ''} {(cfg.get('generate') or {}).get('prompt') or ''}"
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
            # Prefer SOLO identity sheets (one character_id) over combined compose aids.
            solo_nodes: list[str] = []
            for cid in focus:
                for other in graph.get("nodes") or []:
                    if not isinstance(other, dict):
                        continue
                    oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                    if _role_key(other) != "character_design":
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
                    if _role_key(other) != "character_design":
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
            costume_parts = [
                f"{id_to_name.get(cid, cid)}: "
                + next(
                    (
                        str((o.get("config") or {}).get("costume_lock") or "")
                        for o in (graph.get("nodes") or [])
                        if str(o.get("id")) in (cfg.get("character_node_ids") or [])
                        and str(((o.get("config") or {}).get("character_id") or "")) == cid
                    ),
                    str(
                        next(
                            (
                                c.get("description") or ""
                                for c in characters
                                if str(c.get("id")) == cid
                            ),
                            "",
                        )
                    )[:120],
                )
                for cid in focus
            ]
            costume_lock = "; ".join(p for p in costume_parts if p).strip("; ")[:480]
            identity_refs = {
                "character_ids": list(focus),
                "character_node_ids": list(cfg.get("character_node_ids") or []),
                "cast_names": list(cfg.get("cast_names") or []),
                "costume_lock": costume_lock,
                "scene_node_id": "n_scene",
                "prior_keyframe_node_id": cfg.get("prior_keyframe_node_id"),
                "keyframe_strategy": cfg.get("keyframe_strategy") or "compose_from_solo_refs",
            }
            cfg["identity_refs"] = identity_refs
            cfg["costume_lock"] = costume_lock
            cfg["director_task"] = (
                f"Use identity_refs sheets {identity_refs['character_node_ids']} "
                f"({', '.join(cfg.get('cast_names') or [])}). "
                f"Costume lock: {costume_lock}. Do not redesign wardrobe."
            )
            node["config"] = cfg
    graph["metadata"] = meta
    return notes


def _ensure_all_solos_precede_keyframes(graph: DesignerExecutionGraph) -> list[str]:
    """Every identity solo sheet is a data predecessor of every keyframe node.

    Guarantees all character specs finish before any compose/edit keyframe runs.
    """
    notes: list[str] = []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    solo_ids = [
        nid
        for nid, node in by_id.items()
        if _role_key(node) == "character_design"
        and not bool((node.get("config") or {}).get("combined_cast"))
    ]
    if not solo_ids:
        return notes
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in (graph.get("edges") or [])
        if isinstance(e, dict)
    }
    for node in nodes:
        if _role_key(node) not in {"frame", "keyframe"}:
            continue
        fid = str(node.get("id") or "")
        if not fid:
            continue
        cfg = dict(node.get("config") or {})
        inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
        changed = False
        for sid in solo_ids:
            if sid not in inputs:
                inputs.append(sid)
                changed = True
            if (sid, fid) not in edge_pairs:
                graph.setdefault("edges", []).append(
                    {
                        "id": f"e_solo_{sid}_{fid}",
                        "source": sid,
                        "target": fid,
                        "kind": "data",
                        "label": "identity_solo",
                    }
                )
                edge_pairs.add((sid, fid))
                notes.append(f"{fid}: solo_gate <- {sid}")
                changed = True
        if changed:
            cfg["inputs"] = list(dict.fromkeys(inputs))
            node["config"] = cfg
    return notes


def _identity_consistency_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Director gate: solos first; every KF composes; same-setting prompt handoff."""
    notes: list[str] = []
    solo_by_cid: dict[str, str] = {}
    costume_by_cid: dict[str, str] = {}
    all_solo_ids: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if _role_key(node) != "character_design":
            continue
        if cfg.get("combined_cast"):
            continue
        nid = str(node.get("id") or "")
        if nid:
            all_solo_ids.append(nid)
        oids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        if not oids and cfg.get("character_id"):
            oids = [str(cfg.get("character_id"))]
        if len(oids) == 1:
            solo_by_cid[oids[0]] = nid
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

    notes.extend(_ensure_all_solos_precede_keyframes(graph))

    frame_nodes = sorted(
        [
            n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and _role_key(n) in {"frame", "keyframe"}
        ],
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0),
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
        solo_nodes = [solo_by_cid[cid] for cid in cids if cid in solo_by_cid]
        if solo_nodes and list(cfg.get("character_node_ids") or []) != solo_nodes:
            cfg["character_node_ids"] = solo_nodes
            notes.append(f"{node.get('id')}: identity_refs -> solo sheets {solo_nodes}")
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
        if detailed and (
            not costume_lock
            or (
                "top=" not in costume_lock
                and "bottom=" not in costume_lock
                and "outfit=" not in costume_lock
            )
        ):
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
        strategy = "compose_from_solo_refs"
        is_master = False
        handoff_from = None
        if assign_strategy and role in {"frame", "keyframe"}:
            prior_in_set = prev_frame_by_setting.get(setting_id)
            if prior_in_set:
                is_master = False
                handoff_from = scene_master_by_setting.get(setting_id) or prior_in_set
                cfg["scene_prompt_handoff_from"] = handoff_from
                cfg["is_scene_master"] = False
                cfg["scene_master_frame_id"] = handoff_from
                cfg.pop("prior_keyframe_node_id", None)
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
                cfg.pop("prior_keyframe_node_id", None)
                cfg.pop("scene_prompt_handoff_from", None)
                cfg["is_scene_master"] = True
                cfg["scene_master_frame_id"] = str(node.get("id") or "")
                notes.append(
                    f"{node.get('id')}: SCENE MASTER compose for setting {setting_id}"
                )
            cfg["keyframe_strategy"] = strategy
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
            cfg.get("scene_master_frame_id")
            or scene_master_by_setting.get(setting_id)
            or ""
        ).strip() or None
        identity_refs = {
            "character_ids": cids,
            "character_node_ids": list(cfg.get("character_node_ids") or solo_nodes),
            "cast_names": names or list(cfg.get("cast_names") or []),
            "costume_lock": costume_lock,
            "scene_node_id": None,
            "master_scene_node_id": None,
            "scene_master_frame_id": master_frame,
            "is_scene_master": bool(cfg.get("is_scene_master")),
            "prior_keyframe_node_id": None,
            "scene_prompt_handoff_from": cfg.get("scene_prompt_handoff_from") or handoff_from,
            "keyframe_strategy": "compose_from_solo_refs",
            "setting_id": setting_id,
            "view_key": cfg.get("view_key"),
            "spatial_lock": cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else None,
            "occupancy": cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else None,
            "scene_continuity_mode": "scene_card_plus_clip_shots",
            "scene_specs": specs,
            "all_solo_node_ids": list(all_solo_ids),
        }
        cfg["identity_refs"] = identity_refs
        if names:
            cfg["cast_names"] = identity_refs["cast_names"]
        # Sensible LLM-style node names (Brief: … / Scene N: Shot M: …).
        from jiuwenswarm.server.runtime.designer.node_labels import (
            derive_shot_name,
            derive_story_name,
            label_character,
            label_clip,
            label_shot,
        )

        story_name = derive_story_name(
            analysis=analysis,
            prompt=str(graph.get("description") or ""),
            graph_title=str(graph.get("title") or ""),
        )
        scene_n = setting_num.get(setting_id, 1)
        shot_i = int(cfg.get("shot_index") or 0)
        # Per-setting shot ordinal from film order.
        shot_n = 0
        for fn in frame_nodes:
            fsid = str((fn.get("config") or {}).get("setting_id") or "set_1").strip() or "set_1"
            fi = int((fn.get("config") or {}).get("shot_index") or 0)
            if fsid != setting_id:
                continue
            if fi <= shot_i:
                shot_n += 1
        shot_n = max(1, shot_n or shot_i or 1)
        shot_name = derive_shot_name(
            {
                "title": cfg.get("shot_title"),
                "action": cfg.get("shot_action"),
                "keyframe_prompt": (cfg.get("generate") or {}).get("prompt")
                if isinstance(cfg.get("generate"), dict)
                else "",
            },
            fallback_index=shot_n,
        )
        if role in {"frame", "keyframe"} and shot_i:
            label = label_shot(
                scene_number=scene_n, shot_number=shot_n, shot_name=shot_name
            )
            node["label"] = label
            cfg["agent_name"] = label
        elif role == "clip" and shot_i:
            label = label_clip(
                scene_number=scene_n, clip_number=shot_n, clip_name=shot_name
            )
            node["label"] = label
            cfg["agent_name"] = label
        cfg["director_task"] = (
            f"LOCKS: solo sheets {identity_refs['character_node_ids']} "
            f"(all solos ready: {all_solo_ids}). Costume lock: {costume_lock}. "
            f"Strategy=compose_from_solo_refs for setting {setting_id}. "
            f"Scene master/handoff={master_frame}. "
            "Respect scene_specs hierarchical views; no empty plates; no cross-setting."
        )
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "")
        lock_bits = []
        if costume_lock and "Costume lock" not in prompt and "CLOTHING LOCK" not in prompt:
            lock_bits.append(
                f"IDENTITY sheets={identity_refs['character_node_ids']}. "
                f"CLOTHING LOCK: {costume_lock}."
            )
        if "STRATEGY=" not in prompt:
            lock_bits.append(f"STRATEGY=compose_from_solo_refs setting={setting_id}.")
        if specs and "SCENE SPECS" not in prompt:
            lock_bits.append(
                f"SCENE SPECS: scene={specs.get('scene_name') or specs.get('place')}; lighting={specs.get('lighting')}; "
                f"objects={', '.join(str(x) for x in (specs.get('objects') or [])[:5])}; "
                f"views={list((specs.get('views') or {}).keys())}."
            )
        if handoff_from and "SCENE PROMPT HANDOFF" not in prompt:
            lock_bits.append(
                f"SCENE PROMPT HANDOFF from {handoff_from} — keep architecture; change view/cast only."
            )
        if lock_bits:
            gen["prompt"] = (prompt + " " + " ".join(lock_bits)).strip()[:1800]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: identity/scene lock clause in generate.prompt")
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
    from jiuwenswarm.server.runtime.designer.node_labels import (
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
        if role != "character_design":
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

    meta["scene_continuity_mode"] = "scene_card_plus_clip_shots"
    meta["scene_masters"] = dict(scene_master_by_setting)
    meta["scene_locks"] = dict(scene_locks)
    graph["metadata"] = meta
    return notes




async def write_run_feedback(
    *,
    graph: DesignerExecutionGraph,
    run_id: str,
    agent_feedback: dict[str, dict[str, Any]],
    director_plan: dict[str, Any] | None,
    director_review: dict[str, Any],
    director_final: dict[str, Any],
    optimize_for: str,
) -> str:
    """Persist the run report in the feedback store read by Run again."""
    graph_id = str(graph.get("graph_id") or "")
    payload = {
        "schema_version": "designer-feedback.v1",
        "optimize_for": optimize_for,
        "agents": agent_feedback,
        "director_plan": director_plan or {},
        "director": {
            "suggestions": director_final.get("suggestions") or {},
            "final": {
                "scores": director_final.get("scores") or {},
                "node_reports": director_final.get("node_reports") or {},
                "summary": director_final.get("summary") or "",
                "suggestions": director_final.get("suggestions") or {},
                "rating_modality": director_final.get("rating_modality") or "text_only",
                "vision_used": bool(director_final.get("vision_used")),
            },
            "review": {
                "scores": director_review.get("scores") or {},
                "summary": director_review.get("summary") or "",
                "pipeline_notes": director_review.get("pipeline_notes") or "",
                "suggestions": director_review.get("suggestions") or {},
                "rating_modality": director_review.get("rating_modality") or "text_only",
                "vision_used": bool(director_review.get("vision_used")),
                "dual_raters": director_review.get("dual_raters") or {},
                "aggregated_recommendations": director_review.get("aggregated_recommendations")
                or (director_review.get("dual_raters") or {}).get("aggregated_recommendations")
                or [],
                "aggregated_overall": director_review.get("aggregated_overall"),
            },
        },
        "dual_raters": director_review.get("dual_raters") or {},
        "aggregated_recommendations": director_review.get("aggregated_recommendations")
        or (director_review.get("dual_raters") or {}).get("aggregated_recommendations")
        or [],
        "final": {
            "aggregated_score": director_final.get("aggregated_score"),
            "dual_rater_overall": director_review.get("aggregated_overall"),
            "summary": director_final.get("summary"),
            "improvement_plan": director_final.get("improvement_plan"),
            "apply_on": "run_again_only",
        },
    }
    feedback_path = save_feedback(graph_id, run_id, payload)
    meta = dict(graph.get("metadata") or {})
    meta["last_feedback_path"] = str(feedback_path)
    meta["last_feedback_run_id"] = run_id
    meta["last_aggregated_score"] = director_final.get("aggregated_score")
    meta.pop("last_report_path", None)
    graph["metadata"] = meta
    return str(feedback_path)
