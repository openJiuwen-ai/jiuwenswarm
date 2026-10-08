# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build prompt-aware Designer graphs from cast/shot analysis.

Quality layout (default, forward-only):
  Brief → Storyboard → solo cast sheets
  → Scene specs per setting_id (room only, no people)
  → Shots-as-shots: every shot is Wan R2V from on-screen solos plus that empty plate
  → optional Speech/Music → Film (ffmpeg assemble)

``scene_continuity_mode = scene_card_plus_clip_shots``. Solo sheets are identity
locks. Director prunes any node that cannot reach ``n_compose`` and re-edits
Brief / Storyboard / locks afterward.
"""

from __future__ import annotations

import re
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    EDGE_KIND_DATA,
    GRAPH_SOURCE_PROMPT,
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    SCHEMA_VERSION,
    DesignerExecutionGraph,
    normalize_execution_graph,
    new_graph_id,
    node_pipeline,
    utc_now_ms,
)
from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata

_MAX_SPLIT_CHARS = 12
_IMAGE_SIZE = "1K"  # cost-save: ~1024 class, not 2K/4K


def _duration_sec_for_graph(prompt: str, analysis: dict[str, Any], characters: list[dict[str, Any]]) -> int:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        film_duration_for_graph,
    )

    stamped = film_duration_for_graph(prompt, analysis)
    if stamped >= 1:
        return stamped
    return max(6, min(24, max(1, len(characters)) * 3 + 4))


def default_spatial_lock(scene: dict[str, Any] | None = None) -> dict[str, str]:
    """Geography lock without baking in a church/interior template."""
    scene = scene if isinstance(scene, dict) else {}
    return {
        "setting": str(scene.get("name") or "Primary setting"),
        "architecture": str(scene.get("description") or "keep one coherent place"),
        "static_rule": (
            "STATIC OBJECTS LOCKED to the scene specs: landmarks, terrain, buildings, "
            "props, and light direction stay fixed across hierarchical views "
            "(front/left/right/side/top/bottom). Only camera/framing and on-screen cast change. "
            "Never invent an empty environment plate; never borrow architecture from another setting_id."
        ),
        "crowd_rule": (
            "No scene specs. Scene master prompt + solos define the scene. Later "
            "same-setting keyframes reuse the scene specs; keep extras silhouette unless "
            "storyboard exits them. Featured cast are distinct people — never clone faces."
        ),
    }


def _character_display_name(character: dict[str, Any], prompt: str) -> str:
    from jiuwenswarm.server.runtime.designer.script_analysis import infer_primary_subject_name

    name = str(character.get("name") or "").strip()
    if name.lower() in {"", "lead"}:
        return infer_primary_subject_name(prompt)
    return name or infer_primary_subject_name(prompt)


def apply_runtime_delegate(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Stamp creative nodes as LLM agents with tools.

    Do not demote to heuristic handlers when chat credentials are missing —
    the first ``call_model_tool`` raises ``DesignerLlmError`` instead.
    User-reference uploads remain force-handler / read-only.
    """
    from jiuwenswarm.common.schema.designer_graph import (
        CONFIG_DELEGATE_AGENT,
        CONFIG_DELEGATE_HANDLER,
        is_comfyui_node,
    )
    from jiuwenswarm.server.runtime.designer.user_references import (
        is_user_reference_node,
    )

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            continue
        if is_user_reference_node(node) or is_comfyui_node(node) or config.get("force_handler"):
            config["delegate"] = CONFIG_DELEGATE_HANDLER
            config["force_handler"] = True
            config["skip_llm"] = True
            if is_user_reference_node(node):
                config["read_only"] = True
                config["immutable_source"] = True
            continue
        config.pop("force_handler", None)
        config.pop("skip_llm", None)
        config["delegate"] = CONFIG_DELEGATE_AGENT
        config["kind"] = "agent"
    return normalize_execution_graph(graph)


def _edge(
    eid: str,
    source: str,
    target: str,
    *,
    kind: str = EDGE_KIND_DATA,
    label: str | None = None,
) -> dict[str, Any]:
    edge: dict[str, Any] = {"id": eid, "source": source, "target": target, "kind": kind}
    if label:
        edge["label"] = label
    return edge


def find_non_contributing_node_ids(graph: DesignerExecutionGraph) -> list[str]:
    """Node ids that cannot reach the final compose sink (does not mutate graph)."""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    ids = {str(n.get("id")) for n in nodes}
    if not ids:
        return []
    sinks = {i for i in ids if i == "n_compose" or i.startswith("n_compose")}
    if not sinks:
        sinks = {
            str(n.get("id"))
            for n in nodes
            if node_pipeline(n) == NODE_ROLE_COMPOSE
        }
    if not sinks:
        return []
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s in ids and t in ids:
            preds[t].add(s)
    contributing: set[str] = set()
    stack = list(sinks)
    while stack:
        cur = stack.pop()
        if cur in contributing:
            continue
        contributing.add(cur)
        for p in preds.get(cur) or []:
            if p not in contributing:
                stack.append(p)
    return sorted(ids - contributing)


def prune_non_contributing_nodes(graph: DesignerExecutionGraph) -> list[str]:
    """Remove nodes/edges that cannot reach the final compose (or any sink).

    Forward-only graphs must not keep orphan leaves — except ``user_added`` nodes,
    which Director keeps and warns about on Run instead of deleting.
    Returns pruned node ids (user_added orphans are NOT pruned).
    """
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    ids = {str(n.get("id")) for n in nodes}
    if not ids:
        return []
    # Prefer compose as the sole required sink; else keep nodes that reach any video sink.
    sinks = {i for i in ids if i == "n_compose" or i.startswith("n_compose")}
    if not sinks:
        sinks = {
            str(n.get("id"))
            for n in nodes
            if node_pipeline(n) == NODE_ROLE_COMPOSE
        }
    if not sinks:
        return []
    # Reverse adjacency: target ← sources
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s in ids and t in ids:
            preds[t].add(s)
    contributing: set[str] = set()
    stack = list(sinks)
    while stack:
        cur = stack.pop()
        if cur in contributing:
            continue
        contributing.add(cur)
        for p in preds.get(cur) or []:
            if p not in contributing:
                stack.append(p)
    user_added = {
        str(n.get("id"))
        for n in nodes
        if isinstance((n.get("config") or {}), dict)
        and (
            bool((n.get("config") or {}).get("user_added"))
            or bool(str((n.get("config") or {}).get("user_reference_id") or "").strip())
        )
    }
    orphan = ids - contributing
    preserved = sorted(orphan & user_added)
    pruned = sorted(orphan - user_added)
    meta = dict(graph.get("metadata") or {})
    if preserved:
        meta["non_contributing_user_nodes"] = preserved
        meta["contribution_warning"] = (
            "User-added nodes do not feed the final shot/compose: "
            + ", ".join(preserved)
            + ". Connect them into the pipeline if you want them in the film."
        )
    else:
        meta.pop("non_contributing_user_nodes", None)
        # Keep warning only when still relevant
        if not orphan:
            meta.pop("contribution_warning", None)
    if not pruned and not preserved:
        graph["metadata"] = meta
        return []
    keep = contributing | user_added
    if pruned:
        graph["nodes"] = [n for n in nodes if str(n.get("id")) in keep]
        graph["edges"] = [
            e
            for e in edges
            if str(e.get("source") or "") in keep
            and str(e.get("target") or "") in keep
        ]
    notes = list(meta.get("prune_notes") or [])
    notes.extend([f"pruned:{nid}" for nid in pruned])
    if preserved:
        notes.extend([f"kept_user_orphan:{nid}" for nid in preserved])
    meta["prune_notes"] = notes[-40:]
    graph["metadata"] = meta
    return pruned


def _write_storyboard_markdown(
    shots: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    *,
    style_lock: dict[str, Any] | None = None,
) -> str:
    id_to_name = {str(c.get("id")): str(c.get("name") or c.get("id")) for c in characters}
    # Hierarchical: scenes (setting_id) → keyframes/shots.
    by_set: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        if sid not in by_set:
            by_set[sid] = []
            order.append(sid)
        by_set[sid].append(shot)
    lines = [
        "# Storyboard Scenario",
        "",
        (
            "Visual style: "
            + str((style_lock or {}).get("look") or (style_lock or {}).get("medium") or "").strip()
        )
        if style_lock
        else "",
        "",
        "Hierarchy: **Scene (setting_id)** → **Keyframes/shots**.",
        "Different scenes = different places. First keyframe of each scene authors the "
        "**scene specs + master prompt** (compose scene + only on-screen cast) — not an "
        "empty plate. Later same-scene keyframes **compose again from character solos** "
        "using that shared scene prompt/view locks (architecture locked). "
        "Never borrow another setting_id. Not every cast member is in every scene.",
        "",
    ]
    for sid in order:
        scene_shots = by_set.get(sid) or []
        scene_text = ""
        for shot in scene_shots:
            scene_text = str(shot.get("setting_description") or "").strip()
            if scene_text:
                break
        lines.append(f"## Scene `{sid}`" + (f" — {scene_text}" if scene_text else ""))
        lines.append("")
        for shot in scene_shots:
            idx = int(shot.get("shot_index") or 0)
            strategy = str(shot.get("keyframe_strategy") or "")
            visible = [
                id_to_name.get(str(cid), str(cid))
                for cid in (
                    shot.get("on_screen")
                    or shot.get("visible_cast_ids")
                    or shot.get("character_ids")
                    or []
                )
            ]
            offscreen = [
                id_to_name.get(str(cid), str(cid))
                for cid in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
            ]
            featured = [
                id_to_name.get(str(cid), str(cid))
                for cid in (shot.get("featured_cast_ids") or [])
            ]
            actions = shot.get("cast_actions") if isinstance(shot.get("cast_actions"), dict) else {}
            doing_lines = [
                f"{id_to_name.get(str(cid), str(cid))}: {act}"
                for cid, act in actions.items()
                if str(act).strip()
            ]
            crowd = shot.get("crowd_lock") if isinstance(shot.get("crowd_lock"), dict) else {}
            lines.append(f"### Shot {idx} — {shot.get('title') or f'Shot {idx}'}")
            lines.append(f"- Timeline: {shot.get('timeline') or ''}")
            lines.append(f"- Strategy: `{strategy}`")
            lines.append(f"- Camera: {shot.get('camera') or ''}")
            lines.append(f"- On screen (visible): {', '.join(visible) or '—'}")
            lines.append(f"- Offscreen (in scene, not in frame): {', '.join(offscreen) or '—'}")
            lines.append(f"- Featured (camera focus): {', '.join(featured) or '—'}")
            if doing_lines:
                lines.append(f"- Doing: {'; '.join(doing_lines)}")
            lines.append(f"- Action: {shot.get('action') or shot.get('keyframe_prompt') or ''}")
            if shot.get("scene_distinctness"):
                lines.append(f"- Scene note: {shot.get('scene_distinctness')}")
            speech_line = str(shot.get("speech_line") or "").strip()
            by_char = shot.get("speech_by_character") if isinstance(shot.get("speech_by_character"), dict) else {}
            if by_char:
                bits = "; ".join(f"{cid}: {line}" for cid, line in by_char.items() if str(line).strip())
                if bits:
                    lines.append(f"- Speech by character: {bits}")
            elif speech_line:
                lines.append(f"- Speech: {speech_line}")
            if shot.get("language_lock"):
                lines.append(f"- Language lock: {shot.get('language_lock')}")
            if crowd:
                lines.append(
                    f"- Crowd lock: present={crowd.get('present')}; "
                    f"{str(crowd.get('density') or '')}"
                )
            done = shot.get("already_done") or []
            if done:
                lines.append(f"- Already done: {'; '.join(str(x) for x in done[:8])}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _shot_budget(analysis: dict[str, Any], shots: list[dict[str, Any]]) -> int:
    """Honor explicit target_shot_count as a HARD ceiling — never invent extra keyframes."""
    n = len(shots) or 1
    try:
        target = int(analysis.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        target = 0
    # Also honor user-prompt N-shot / N分镜 language stamped on analysis.
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        prompt = str(
            analysis.get("user_prompt")
            or analysis.get("summary")
            or ""
        )
        explicit = _explicit_shot_count_from_prompt(prompt)
        if explicit >= 1:
            target = explicit if target < 1 else min(target, explicit)
    except Exception:  # noqa: BLE001
        pass
    if target >= 1:
        return max(1, min(target, n))
    return max(1, n)


def _ensure_characters_referenced(
    characters: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> None:
    """Only attach uncovered cast to a shot that already lists them on_screen/ids.

    Never dump onto shot 1 by lexical score — that puts later-meet cast into the
    establishing beat (e.g. woman into alone-in-office).
    """
    covered = {
        str(cid)
        for s in shots
        if isinstance(s, dict)
        for cid in (
            list(s.get("on_screen") or [])
            + list(s.get("visible_cast_ids") or [])
            + list(s.get("character_ids") or [])
            + list(s.get("offscreen") or [])
            + list(s.get("off_screen_cast_ids") or [])
        )
        if str(cid)
    }
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid in covered or not shots:
            continue
        # Already listed somewhere under another field — sync character_ids only.
        placed = False
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            listed = {
                str(x)
                for x in (
                    list(shot.get("on_screen") or [])
                    + list(shot.get("visible_cast_ids") or [])
                    + list(shot.get("offscreen") or [])
                    + list(shot.get("off_screen_cast_ids") or [])
                )
                if str(x)
            }
            if cid not in listed:
                continue
            shot["character_ids"] = list(
                dict.fromkeys([*(shot.get("character_ids") or []), cid])
            )
            covered.add(cid)
            placed = True
            break
        if not placed:
            # Leave uncovered — Director/validators must reject or Director must list them.
            continue


def _plan_cast_sheets(
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    *,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], dict[str, list[str]], str]:
    """Plan solo cast postcard nodes and per-shot node refs.

    Identity rule: one solo sheet per character (canonical look). Combined
    multi-person sheets are not used — quality path always composes from solos.
    """
    id_to_char = {str(c.get("id")): c for c in characters if str(c.get("id") or "")}
    sheets: list[dict[str, Any]] = []
    budget = max(_MAX_SPLIT_CHARS, len(characters))

    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid not in id_to_char:
            continue
        if len(sheets) >= budget:
            break
        name = _character_display_name(ch, prompt) or cid
        desc = str(ch.get("description") or "").strip()
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            enrich_character_clothing,
        )

        costume = enrich_character_clothing(ch) or (
            desc[:240] if desc else f"canonical look for {name}"
        )
        sheets.append(
            {
                "character_ids": [cid],
                "character_names": [name],
                "combined_cast": False,
                "identity_source": True,
                "label": name[:48],
                "prompt_body": (
                    f"{name}: {desc}. Wearing {costume}."
                    if desc
                    else f"{name}. Wearing {costume}."
                ),
                "costume_lock": costume[:320],
            }
        )

    if not sheets and characters:
        ch = characters[0]
        name = _character_display_name(ch, prompt)
        sheets.append(
            {
                "character_ids": [str(ch.get("id") or "char_1")],
                "character_names": [name],
                "combined_cast": False,
                "identity_source": True,
                "label": name[:48],
                "prompt_body": f"{name}: {ch.get('description')}",
                "costume_lock": str(ch.get("description") or name)[:240],
            }
        )

    solo_count = len(sheets)
    for solo_i, sheet in enumerate(sheets, start=1):
        if solo_count == 1:
            sheet["node_id"] = "n_character"
        else:
            sheet["node_id"] = f"n_character_{solo_i}"

    solo_by_id: dict[str, str] = {}
    costume_by_id: dict[str, str] = {}
    for sheet in sheets:
        ids = [str(x) for x in sheet["character_ids"]]
        if len(ids) == 1:
            solo_by_id[ids[0]] = str(sheet["node_id"])
            costume_by_id[ids[0]] = str(sheet.get("costume_lock") or "")

    shot_to_nodes: dict[str, list[str]] = {}
    for shot in shots:
        idx = str(int(shot.get("shot_index") or 0))
        cids = [
            str(x)
            for x in (shot.get("character_ids") or [])
            if str(x) in id_to_char
        ]
        cids = list(dict.fromkeys(cids))
        nodes = [solo_by_id[cid] for cid in cids if cid in solo_by_id]
        if not nodes:
            nodes = [str(s["node_id"]) for s in sheets]
        shot_to_nodes[idx] = list(dict.fromkeys(nodes))

    layout = "single" if solo_count <= 1 else "solo_first"
    for sheet in sheets:
        sheet["_costume_by_id"] = costume_by_id
    return sheets, shot_to_nodes, layout


def ensure_combined_cast_reach_compose(graph: DesignerExecutionGraph) -> list[str]:
    """Safety net: every combined-cast node must be an edge source into the DAG.

    If a combined sheet never sources an edge to a scene/shot/compose/storyboard,
    wire it to ``n_clip_1`` (then scene / compose / legacy frame) and append to that
    target's ``config.inputs``.
    """
    notes: list[str] = []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    ids = set(by_id)
    if not ids:
        return notes

    sink_roles = {
        NODE_ROLE_SCENE,
        NODE_ROLE_FRAME,
        "keyframe",
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_ROLE_STORYBOARD,
        "scene",
        "frame",
        "clip",
        "compose",
        "storyboard",
    }
    wired_sources: set[str] = set()
    edge_keys: set[tuple[str, str]] = set()
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s not in ids or t not in ids:
            continue
        edge_keys.add((s, t))
        trole = node_pipeline(by_id[t])
        if (
            trole in sink_roles
            or t in {"n_compose", "n_storyboard", "n_scene"}
            or t.startswith("n_scene_")
            or t.startswith("n_frame_")
            or t.startswith("n_clip_")
        ):
            wired_sources.add(s)

    combined_ids = [
        str(sheet_id)
        for sheet_id, node in by_id.items()
        if bool((node.get("config") or {}).get("combined_cast"))
        or sheet_id.startswith("n_cast_")
    ]
    has_compose = "n_compose" in ids or any(
        node_pipeline(by_id[i]) == NODE_ROLE_COMPOSE
        for i in ids
    )
    fallback = next(
        (
            cand
            for cand in (
                "n_clip_1",
                "n_scene_1",
                "n_scene",
                "n_frame_1",
                "n_compose",
            )
            if cand in ids
        ),
        next(
            (
                i
                for i in sorted(ids)
                if node_pipeline(by_id[i]) == NODE_ROLE_COMPOSE
            ),
            None,
        ),
    )
    if not fallback and not has_compose:
        return notes

    changed = False
    for cid in combined_ids:
        if cid in wired_sources:
            continue
        target = fallback
        if not target or target not in ids:
            continue
        if (cid, target) not in edge_keys:
            edges.append(_edge(f"e_{cid}_{target}", cid, target))
            edge_keys.add((cid, target))
        tnode = by_id[target]
        tcfg = dict(tnode.get("config") or {})
        inputs = list(tcfg.get("inputs") or [])
        if cid not in inputs:
            inputs.append(cid)
            tcfg["inputs"] = inputs
            tnode["config"] = tcfg
        notes.append(f"{cid}: safety-wired -> {target}")
        changed = True
        wired_sources.add(cid)

    if changed:
        graph["edges"] = edges
        graph["nodes"] = nodes
    return notes


def _cameras_compatible(a: str, b: str) -> bool:
    """True when sequential keyframe edit is safer than a full recompose."""
    la = (a or "").strip().lower()
    lb = (b or "").strip().lower()
    if not la or not lb:
        return False
    if la == lb:
        return True
    # Treat generic medium/eye-level variants as compatible.
    mediumish = ("medium", "eye-level", "eye level")
    if any(m in la for m in mediumish) and any(m in lb for m in mediumish):
        if "close" in la or "close" in lb or "wide" in la or "wide" in lb:
            return False
        return True
    return False


def _costume_lock_for_ids(
    characters: list[dict[str, Any]], character_ids: list[str]
) -> str:
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        costume_lock_for_ids,
        enrich_character_clothing,
    )

    for ch in characters:
        if isinstance(ch, dict):
            enrich_character_clothing(ch)
    detailed = costume_lock_for_ids(characters, character_ids)
    if detailed:
        return detailed
    parts: list[str] = []
    id_to = {str(c.get("id")): c for c in characters if isinstance(c, dict)}
    for cid in character_ids:
        ch = id_to.get(str(cid))
        if not ch:
            continue
        name = str(ch.get("name") or cid)
        desc = str(ch.get("description") or ch.get("costume_lock") or "").strip()
        parts.append(f"{name}: {desc[:200]}" if desc else name)
    return "; ".join(parts)[:720]


def _ensure_setting_ids(
    shots: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> None:
    """Stamp setting_id on every shot.

    Prefer explicit setting_id / scene_id. Only inherit the previous setting when
    the shot clearly stays in the same place; meet/leave/exterior language gets a
    new setting id so later cast is not folded into the office ensemble.
    """
    default = "set_1"
    if scenes:
        default = str(scenes[0].get("id") or "set_1").strip() or "set_1"
    scene_ids = [
        str(s.get("id") or "").strip()
        for s in scenes
        if isinstance(s, dict) and str(s.get("id") or "").strip()
    ]
    prev = default
    new_place_re = re.compile(
        r"\b("
        r"meet|meets|meeting|later|then|outside|street|exterior|outdoor|"
        r"leaves?|leaving|exit|exits|arrive|arrives|another (place|room|location)|"
        r"new (place|scene|location)|cut to|elsewhere"
        r")\b",
        re.I,
    )
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if sid:
            prev = sid
            shot["setting_id"] = sid
            continue
        blob = f"{shot.get('action') or ''} {shot.get('keyframe_prompt') or ''} {shot.get('title') or ''}"
        if i > 0 and new_place_re.search(blob):
            # Prefer next unused scene id from analysis; else synthesize.
            used = {
                str(s.get("setting_id") or "")
                for s in shots
                if isinstance(s, dict) and s.get("setting_id")
            }
            candidate = next((x for x in scene_ids if x not in used and x != prev), "")
            if not candidate:
                candidate = f"set_{i + 1}"
            sid = candidate
        else:
            sid = prev
        shot["setting_id"] = sid
        prev = sid


def build_smart_video_graph(
    *,
    project_id: str,
    prompt: str,
    analysis: dict[str, Any],
    title: str | None = None,
) -> DesignerExecutionGraph:
    """Lean multi-shot video DAG: few image gens + brief/storyboard.

    Creative nodes are always stamped as LLM agents (``delegate=agent``).
    """
    from jiuwenswarm.server.runtime.designer.pipeline.keyframe_policy import (
        apply_compose_solos_setting_policy,
    )

    prompt_text = prompt.strip()
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        build_reference_led_video_graph,
        reference_led_active,
    )

    if reference_led_active(analysis):
        graph = build_reference_led_video_graph(
            project_id=project_id,
            prompt=prompt_text,
            analysis=analysis,
            title=title,
        )
        graph = normalize_execution_graph(graph)
        prune_non_contributing_nodes(graph)
        return attach_skills_metadata(graph, prompt_text)
    from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

    analysis = ensure_audio_locks_on_analysis(dict(analysis or {}), prompt_text)
    analysis["user_prompt"] = prompt_text
    from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
        ensure_style_lock,
    )

    # Establish one style authority before the production bible / brief are built.
    # Media leaves receive this same value; they never infer their own medium.
    film_style = ensure_style_lock(
        analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else None,
        prompt=prompt_text,
    )
    analysis["style_lock"] = dict(film_style)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            apply_shot_scope,
        )

        analysis = apply_shot_scope(analysis, prompt_text)
    except Exception:  # noqa: BLE001
        pass
    from jiuwenswarm.server.runtime.designer.node_labels import (
        derive_shot_name,
        derive_story_name,
        label_brief,
        label_character,
        label_clip,
        label_compose,
        label_scene,
        label_storyboard,
    )

    story_name = derive_story_name(
        analysis=analysis,
        prompt=prompt_text,
        graph_title=str(title or ""),
    )
    analysis["story_name"] = story_name
    characters = [item for item in (analysis.get("characters") or []) if isinstance(item, dict)]
    scenes = [item for item in (analysis.get("scenes") or []) if isinstance(item, dict)]
    shots = [item for item in (analysis.get("shots") or []) if isinstance(item, dict)]
    audio = dict(analysis.get("audio") or {})
    missing = [
        name
        for name, values in (
            ("characters", characters),
            ("scenes", scenes),
            ("shots", shots),
        )
        if not values
    ]
    if missing:
        from jiuwenswarm.server.runtime.designer.model_tools import (
            DesignerLlmError,
            LLM_API_ERROR,
        )

        raise DesignerLlmError(
            "Chat model analysis did not return usable "
            + ", ".join(missing)
            + "; refusing to synthesize a deterministic execution graph.",
            code=LLM_API_ERROR,
        )
    shots = shots[: _shot_budget(analysis, shots)]
    analysis["target_shot_count"] = len(shots)
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
    _ensure_characters_referenced(characters, shots)
    _ensure_setting_ids(shots, scenes)
    analysis["characters"] = characters
    analysis["scenes"] = scenes
    analysis["shots"] = shots
    analysis = apply_compose_solos_setting_policy(analysis)
    characters = list(analysis.get("characters") or characters)
    scenes = list(analysis.get("scenes") or scenes)
    shots = list(analysis.get("shots") or shots)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
            ensure_shot_start_end_states,
        )

        shots = ensure_shot_start_end_states(shots)
        analysis["shots"] = shots
    except Exception:  # noqa: BLE001
        pass
    # Continuity: scene specs + on-screen solos; storyboard start/end owns continuity.
    analysis["scene_continuity_mode"] = "scene_card_plus_clip_shots"
    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
        infer_aspect_lock,
        resolve_clip_video_format,
    )
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        resolution_mentioned,
    )

    aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
    if not aspect:
        aspect = infer_aspect_lock(prompt_text)
    asked_res = resolution_mentioned(prompt_text)
    if asked_res:
        analysis["video_resolution"] = asked_res
    elif not str(analysis.get("video_resolution") or "").strip():
        analysis["video_resolution"] = str(aspect.get("video_resolution") or "")
    film_video_size, film_video_res = resolve_clip_video_format(
        ratio=str(aspect.get("ratio") or "16:9"),
        user_resolution=asked_res,
        director_resolution=str(
            analysis.get("video_resolution") or aspect.get("video_resolution") or ""
        ),
    )
    analysis["video_resolution"] = film_video_res
    analysis["video_size"] = film_video_size
    aspect = dict(aspect)
    aspect["video_size"] = film_video_size
    aspect["video_resolution"] = film_video_res
    analysis["aspect_lock"] = aspect

    cast_sheets, shot_cast_nodes, cast_layout = _plan_cast_sheets(
        characters, shots, prompt=prompt_text
    )
    film_duration = _duration_sec_for_graph(prompt_text, analysis, characters)
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
            build_production_bible,
        )

        bible = build_production_bible(analysis, user_prompt=prompt_text)
        analysis["production_bible"] = bible
    except Exception:  # noqa: BLE001
        pass

    graph_id = new_graph_id()
    now = utc_now_ms()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]

    nodes: list[dict[str, Any]] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": label_brief(story_name),
            "config": {
                "role": NODE_ROLE_BRIEF,
                "prompt": prompt_text,
                "tools": ["call_model", "write_artifact"],
                "agent_name": label_brief(story_name),
                "kind": "agent",
                "skill_id": "brief",
                "delegate": "agent",
                "director_task": (
                    "Author a DETAILED creative brief from the user prompt: every named "
                    "character with wardrobe/face locks, scene geography, language/speech, "
                    "opening blocking, motion/consistency rules, shot-view coverage, audio, "
                    "and one explicit visual style. Preserve the user's style wording; if "
                    "none can be inferred, use the cartoonish default. "
                    "Preserve every named shot. Obey and include the PRODUCTION LOCK SPECS."
                ),
            },
            "layout": {"x": 40, "y": 220, "width": 260, "height": 140},
        }
    ]
    edges: list[dict[str, Any]] = []

    # Storyboard after brief (director will gate fidelity before cast/scene run).
    sb_cfg: dict[str, Any] = {
        "role": NODE_ROLE_STORYBOARD,
        "prompt": prompt_text,
        "planned_shots": shots,
        "inputs": ["n_brief"],
        "agent_name": label_storyboard(story_name),
        "kind": "agent",
        "skill_id": "storyboard",
        "delegate": "agent",
        "director_task": (
            "Build a time-coherent DETAILED storyboard from the approved brief: "
            "materialize its full narrative/content arc and timed speech plan; every shot "
            "must advance action, information, product proof, or emotion without filler, "
            "repeated action, or duplicate coverage. Include per-shot duration, camera/view, "
            "on-screen cast, full blocking/action, "
            "exact speech_line, language lock, consistency forbids "
            "and the Brief's visual style in every composed-scene description "
            "(do not undo a completed shot). Each row is THAT window in full detail — "
            "not a camera restage of the whole prompt, and not a stripped one-liner."
        ),
        "tools": ["call_model", "write_artifact"],
    }
    nodes.append(
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": label_storyboard(story_name),
            "config": sb_cfg,
            "layout": {"x": 340, "y": 220, "width": 280, "height": 150},
        }
    )
    edges.append(_edge("e_brief_storyboard", "n_brief", "n_storyboard"))

    char_node_ids: list[str] = []
    sheet_by_id: dict[str, dict[str, Any]] = {}
    for i, sheet in enumerate(cast_sheets, start=1):
        nid = str(sheet["node_id"])
        char_node_ids.append(nid)
        sheet_by_id[nid] = sheet
        names = [str(n) for n in sheet.get("character_names") or []]
        # Namecard: character display name (Director-approved via analysis cast).
        display = (names[0] if names else str(sheet.get("label") or f"Character {i}")).strip()
        label = label_character(i, display)
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            extract_clothing_parts,
        )

        wardrobe_parts = extract_clothing_parts(
            f"{sheet.get('costume_lock') or ''} {sheet.get('prompt_body') or ''}"
        )
        wardrobe = ", ".join(
            str(v).strip() for v in wardrobe_parts.values() if str(v).strip()
        )
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
                compose_character_sheet_prompt,
            )

            seed_cfg = {
                "role": NODE_ROLE_CHARACTER_DESIGN,
                "character_name": names[0] if names else display,
                "character_names": names,
                "costume_lock": wardrobe or str(sheet.get("costume_lock") or ""),
                "image_size": _IMAGE_SIZE,
            }
            prompt = compose_character_sheet_prompt(cfg=seed_cfg, graph=None, seed="")
        except Exception:  # noqa: BLE001
            prompt = (
                f"One person only: {display}, full or three-quarter body on a plain "
                "empty studio backdrop. Solid neutral background, identity and costume only. "
                + (f"Wearing {wardrobe}. " if wardrobe else "")
                + "One clear image."
            )
        agent = label
        nodes.append(
            {
                "id": nid,
                "type": NODE_TYPE_IMAGE,
                "label": label,
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "prompt": prompt,
                    "generate": {"prompt": prompt},
                    "character_id": (sheet["character_ids"][0] if len(sheet["character_ids"]) == 1 else None),
                    "character_ids": list(sheet["character_ids"]),
                    "character_name": (names[0] if names else display),
                    "character_names": names,
                    "combined_cast": False,
                    "identity_source": True,
                    "costume_lock": str(sheet.get("costume_lock") or ""),
                    "style_lock": dict(film_style),
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": ["n_brief", "n_storyboard"],
                    "agent_name": agent,
                    "kind": "agent",
                    "skill_id": "character",
                    "tools": ["call_image_model", "read_upstream", "call_model"],
                    "delegate": "agent",
                    "director_task": (
                        f"Solo identity sheet for {display}. "
                        "Write a positive Qwen-ready studio portrait from the locks "
                        "(face, wardrobe, style, aspect) — no LOCK banners or negatives. "
                        "Then call_image_model with that prompt only."
                    ),
                },
                "layout": {"x": 680, "y": float(40 + (i - 1) * 160), "width": 240, "height": 140},
            }
        )
        edges.append(_edge(f"e_sb_{nid}", "n_storyboard", nid))
        edges.append(_edge(f"e_brief_{nid}", "n_brief", nid))

    # Spatial lock text (weak env hint only). Scene specs are built per setting_id;
    # shots use them as Wan reference images (last env ref) with solos as character1…
    scene_base = scenes[0] if scenes else {"id": "scene_1", "name": "Setting", "description": ""}
    spatial_lock = default_spatial_lock(scene_base if isinstance(scene_base, dict) else None)
    prior_lock = analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else {}
    for k, v in prior_lock.items():
        if str(v).strip():
            spatial_lock[str(k)] = str(v).strip()[:400]
    setting_scene_names = (
        analysis.get("setting_scene_names")
        if isinstance(analysis.get("setting_scene_names"), dict)
        else {}
    )
    spatial_by_setting: dict[str, dict[str, Any]] = {}
    for sid, scene_text in setting_scene_names.items():
        sid_s = str(sid).strip() or "set_1"
        base = dict(spatial_lock)
        base["setting"] = sid_s
        if str(scene_text).strip():
            text = str(scene_text).strip()[:400]
            base["architecture"] = text
            base["static_rule"] = (
                f"SETTING `{sid_s}` place lock: {text[:220]}. "
                "Same-setting edits keep this architecture; other setting_ids must look different."
            )
        spatial_by_setting[sid_s] = base

    def _lock_line_for(sid: str) -> str:
        lock = spatial_by_setting.get(sid) or spatial_lock
        return (
            f"SPATIAL LOCK (text hint only — not an empty plate): setting={lock.get('setting')}; "
            f"{lock.get('architecture')}; {lock.get('static_rule')} "
            f"{lock.get('crowd_rule')}"
        )

    scene_locks_meta: dict[str, Any] = (
        analysis.get("scene_locks")
        if isinstance(analysis.get("scene_locks"), dict)
        else {}
    )
    setting_order: list[str] = []
    for _shot in shots:
        _sid = str((_shot or {}).get("setting_id") or "set_1").strip() or "set_1"
        if _sid not in setting_order:
            setting_order.append(_sid)
    setting_num = {sid: i + 1 for i, sid in enumerate(setting_order)}
    shot_ord_by_setting: dict[str, int] = {sid: 0 for sid in setting_order}

    scene_by_id: dict[str, dict[str, Any]] = {}
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        for key in (sc.get("id"), sc.get("setting_id")):
            sid_k = str(key or "").strip()
            if sid_k:
                scene_by_id.setdefault(sid_k, sc)

    def _scene_name_for_setting(sid: str) -> str:
        sc = scene_by_id.get(sid) or {}
        name = str(sc.get("scene_name") or sc.get("name") or sc.get("place") or "").strip()
        if name:
            return name
        scene_text = str(setting_scene_names.get(sid) or "").strip()
        if scene_text:
            return scene_text
        for sh in shots:
            if str(sh.get("setting_id") or "") != sid:
                continue
            title = str(sh.get("title") or "").strip()
            if title:
                return title
        return f"Scene {setting_num.get(sid, 1)}"

    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _cast_id_maps,
        resolve_cast_token_list,
    )

    _valid_cast, _by_name_cast = _cast_id_maps(characters)
    id_to_node_early: dict[str, str] = {}
    for s in cast_sheets:
        if s.get("combined_cast"):
            continue
        ids = [str(x) for x in (s.get("character_ids") or []) if str(x)]
        if len(ids) == 1 and s.get("node_id"):
            id_to_node_early[ids[0]] = str(s["node_id"])

    def _ensemble_cids_for_setting(sid: str) -> list[str]:
        first = next(
            (
                sh
                for sh in shots
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") == sid
            ),
            None,
        )
        raw: list[Any] = []
        if isinstance(first, dict):
            occ0 = first.get("occupancy") if isinstance(first.get("occupancy"), dict) else {}
            raw = list(
                first.get("on_screen")
                or first.get("character_ids")
                or occ0.get("must_appear")
                or []
            )
        if not raw:
            for sh in shots:
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") != sid:
                    continue
                raw.extend(sh.get("on_screen") or sh.get("character_ids") or [])
        return resolve_cast_token_list(raw, valid_ids=_valid_cast, by_name=_by_name_cast)

    scene_id_by_setting: dict[str, str] = {}
    scene_master_by_setting: dict[str, str] = {}
    for sid in setting_order:
        scene_num = setting_num.get(sid, 1)
        scene_nid = f"n_scene_{scene_num}"
        scene_id_by_setting[sid] = scene_nid
        scene_master_by_setting[sid] = scene_nid
        scene_specs_setting = (
            scene_locks_meta.get(sid)
            if isinstance(scene_locks_meta.get(sid), dict)
            else {}
        )
        shot_spatial_scene = spatial_by_setting.get(sid) or spatial_lock
        lock_line_scene = _lock_line_for(sid)
        scene_name = _scene_name_for_setting(sid)
        scene_label = label_scene(scene_number=scene_num, scene_name=scene_name)
        sc_rec = scene_by_id.get(sid) or {}
        env_desc = str(
            sc_rec.get("description")
            or sc_rec.get("name")
            or setting_scene_names.get(sid)
            or scene_name
        ).strip()[:400]
        ensemble_cids = _ensemble_cids_for_setting(sid)
        ensemble_nids = [id_to_node_early[c] for c in ensemble_cids if c in id_to_node_early]
        ensemble_names = [
            str(c.get("name") or c.get("id"))
            for c in characters
            if str(c.get("id")) in set(ensemble_cids)
        ]
        first_shot = next(
            (
                sh
                for sh in shots
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") == sid
            ),
            {},
        )
        opening_action = str(
            (first_shot or {}).get("action") or (first_shot or {}).get("keyframe_prompt") or ""
        )[:280]
        opening_cast = ", ".join(ensemble_names) or "named cast"
        tod: dict[str, str] = {}
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                infer_time_of_day_lock,
            )
            from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
                compose_scene_specs_prompt,
                visual_place_name,
            )

            tod = infer_time_of_day_lock(
                prompt_text,
                str(
                    (scene_specs_setting or {}).get("scene_name")
                    or env_desc
                    or ""
                ),
            )
            if isinstance(scene_specs_setting, dict) and scene_specs_setting.get("time_of_day"):
                tod["time_of_day"] = str(scene_specs_setting.get("time_of_day"))
            if isinstance(scene_specs_setting, dict) and scene_specs_setting.get("lighting"):
                tod["lighting"] = str(scene_specs_setting.get("lighting"))
            place_name = visual_place_name(
                (scene_specs_setting or {}).get("scene_name")
            ) or visual_place_name(env_desc)
            seed_cfg = {
                "role": NODE_ROLE_SCENE,
                "setting_id": sid,
                "scene_specs": {
                    **(scene_specs_setting or {}),
                    "scene_name": place_name,
                },
                "spatial_lock": shot_spatial_scene,
                "time_of_day_lock": tod,
                "style_lock": dict(film_style) if isinstance(film_style, dict) else {},
                "image_size": _IMAGE_SIZE,
            }
            scene_prompt = compose_scene_specs_prompt(cfg=seed_cfg, graph=None, seed="")
        except Exception:  # noqa: BLE001
            tod = {}
            scene_prompt = "One empty setting. The setting is empty. One clear image."
        _ = (opening_cast, opening_action, ensemble_nids, lock_line_scene)
        scene_inputs = ["n_brief", "n_storyboard"]
        nodes.append(
            {
                "id": scene_nid,
                "type": NODE_TYPE_IMAGE,
                "label": scene_label,
                "config": {
                    "role": NODE_ROLE_SCENE,
                    "setting_id": sid,
                    "composed_scene": False,
                    "style_lock": dict(film_style),
                    "on_screen": [],
                    "character_ids": [],
                    "character_node_ids": [],
                    "cast_names": [],
                    "scene_specs": scene_specs_setting or None,
                    "spatial_lock": shot_spatial_scene,
                    "time_of_day_lock": tod if isinstance(tod, dict) else None,
                    "is_scene_master": True,
                    "generate": {"prompt": scene_prompt},
                    "prompt": scene_prompt,
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": scene_inputs,
                    "agent_name": scene_label,
                    "kind": "agent",
                    "skill_id": "scene",
                    "tools": ["call_image_model", "read_upstream"],
                    "delegate": "agent",
                    "director_task": (
                        f"Empty environment plate for setting {sid}. "
                        "Write a positive Qwen-ready plate prompt from the locks "
                        "(place, lighting, style, aspect) — no LOCK banners or negatives. "
                        "Then call_image_model with that prompt only."
                    ),
                },
                "layout": {"x": 940, "y": float(40 + (scene_num - 1) * 180), "width": 240, "height": 140},
            }
        )
        for src in scene_inputs:
            edges.append(_edge(f"e_{src}_{scene_nid}", src, scene_nid))

    shot_ids: list[str] = []
    prev_shot_global = ""
    prev_shot_by_setting: dict[str, str] = {}
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    for shot in shots:
        idx = int(shot.get("shot_index") or (len(shot_ids) + 1))
        shot_id = f"n_clip_{idx}"
        shot_ids.append(shot_id)
        setting_id = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        scene_nid = scene_id_by_setting.get(setting_id) or ""
        focus_char_nodes = list(dict.fromkeys(shot_cast_nodes.get(str(idx), [])))
        focus_char_nodes = [
            nid
            for nid in focus_char_nodes
            if not bool(sheet_by_id.get(nid, {}).get("combined_cast"))
        ]
        occ0 = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        visible_cids = resolve_cast_token_list(
            shot.get("on_screen")
            or shot.get("visible_cast_ids")
            or occ0.get("must_appear")
            or shot.get("compose_cast_ids")
            or shot.get("character_ids")
            or [],
            valid_ids=_valid_cast,
            by_name=_by_name_cast,
        )
        offscreen_cids = resolve_cast_token_list(
            shot.get("offscreen")
            or shot.get("off_screen_cast_ids")
            or occ0.get("offscreen")
            or [],
            valid_ids=_valid_cast,
            by_name=_by_name_cast,
        )
        offscreen_cids = [c for c in offscreen_cids if c not in visible_cids]
        focus_cids = list(dict.fromkeys(visible_cids))
        featured_cids = [
            str(x)
            for x in (shot.get("featured_cast_ids") or focus_cids[:1] or [])
            if str(x)
        ]
        featured_cids = resolve_cast_token_list(
            featured_cids or focus_cids[:1],
            valid_ids=_valid_cast,
            by_name=_by_name_cast,
        ) or list(focus_cids[:1])
        cast_actions = (
            shot.get("cast_actions")
            if isinstance(shot.get("cast_actions"), dict)
            else (occ0.get("cast_actions") if isinstance(occ0.get("cast_actions"), dict) else {})
        )
        shot["character_ids"] = list(dict.fromkeys(focus_cids))
        shot["on_screen"] = list(shot["character_ids"])
        shot["offscreen"] = [c for c in offscreen_cids if c not in shot["character_ids"]]
        id_to_node: dict[str, str] = {}
        for s in cast_sheets:
            if s.get("combined_cast"):
                continue
            ids = [str(x) for x in (s.get("character_ids") or []) if str(x)]
            if len(ids) == 1 and s.get("node_id"):
                id_to_node[ids[0]] = str(s["node_id"])
        rebuilt = [id_to_node[c] for c in focus_cids if c in id_to_node]
        focus_char_nodes = list(dict.fromkeys(rebuilt)) if rebuilt else []
        focus_names = [
            str(c.get("name"))
            for c in characters
            if str(c.get("id")) in focus_cids
        ]
        if not focus_names:
            for nid in focus_char_nodes:
                focus_names.extend(sheet_by_id.get(nid, {}).get("character_names") or [])
            focus_names = list(dict.fromkeys([n for n in focus_names if n]))
        cast_who = ", ".join(focus_names) or "main cast"
        doing_line = "; ".join(
            f"{(next((c.get('name') for c in characters if str(c.get('id'))==cid), cid))}:"
            f" {cast_actions[cid]}"
            for cid in focus_cids
            if cast_actions.get(cid)
        )
        camera = str(shot.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        shot["camera"] = camera
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
        if not action:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                window_beat,
            )

            action = window_beat(prompt_text, idx, max(1, len(shots)))
            shot["action"] = action
        action = action[:800]
        costume_lock = _costume_lock_for_ids(characters, focus_cids)
        from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
            enrich_shot_staging,
            staging_lock_clause,
        )

        staging = enrich_shot_staging(shot, characters)
        staging_bits = staging_lock_clause(
            positioning_lock=staging.get("positioning_lock") or "",
            action_lock=staging.get("action_lock") or "",
            relationship_lock=staging.get("relationship_lock") or "",
            shot_index=idx,
            setting_id=setting_id,
            for_clip=True,
        )
        shot_spatial = spatial_by_setting.get(setting_id) or spatial_lock
        lock_line = _lock_line_for(setting_id)
        keyframe_strategy = "clip_from_scene_and_solos"
        # Storyboard-owned consistency: deps = storyboard + on-screen solos + scene only.
        # No prior-shot edge — same-setting shots can run concurrently.
        clip_inputs = ["n_storyboard", *focus_char_nodes]
        if scene_nid:
            clip_inputs.append(scene_nid)
        clip_inputs = list(dict.fromkeys([x for x in clip_inputs if x]))
        y = 40 + (idx - 1) * 160
        occupancy = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        already_done = [
            str(x)
            for x in (shot.get("already_done") or [])
            if str(x).strip()
        ]
        # First shot of a setting must not inherit prior-room already_done notes.
        if not prev_shot_by_setting.get(setting_id):
            already_done = []
        elif setting_id:
            # Drop notes that clearly name a different setting_id.
            filtered: list[str] = []
            for note in already_done:
                low = note.lower()
                if "setting=" in low and f"setting={setting_id.lower()}" not in low:
                    continue
                filtered.append(note)
            already_done = filtered
        scene_specs = (
            shot.get("scene_specs")
            if isinstance(shot.get("scene_specs"), dict)
            else (scene_locks_meta.get(setting_id) if isinstance(scene_locks_meta.get(setting_id), dict) else {})
        )
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            ANGLE_VIEWS,
            user_asked_coverage,
        )

        raw_view = str(shot.get("view_key") or "").strip()
        relation = str(shot.get("shot_relation") or "").strip().lower()
        show_angle = raw_view.lower() in ANGLE_VIEWS and (
            user_asked_coverage(prompt_text) or relation == "angle_variant"
        )
        view_key = raw_view if show_angle else ""
        shot["view_key"] = view_key
        timeline = str(shot.get("timeline") or "").strip() or f"{(idx - 1) * 5:.1f}-{idx * 5:.1f}s"
        specs_line = ""
        if scene_specs:
            views = scene_specs.get("views") if isinstance(scene_specs.get("views"), dict) else {}
            view_line = ""
            if show_angle:
                view_line = str(views.get(view_key) or "")[:220]
            specs_line = (
                f"SCENE SPECS `{setting_id}`: scene={scene_specs.get('scene_name') or scene_specs.get('place')}; "
                f"lighting={scene_specs.get('lighting')}; "
                f"objects={', '.join(str(x) for x in (scene_specs.get('objects') or [])[:6])}; "
                f"crowd={scene_specs.get('crowd')}; "
                f"{scene_specs.get('coherence_rule')}; "
                + (f"ACTIVE {view_line}. " if view_line else "")
            )
        consistency = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
        if not consistency:
            from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock

            consistency = infer_continuity_lock(action)
            shot["continuity_lock"] = consistency
        cont_bits = ", ".join(f"{k}={v}" for k, v in consistency.items()) if consistency else ""
        occ_bits = ""
        if occupancy:
            occ_bits = (
                f"OCCUPANCY: must_appear={occupancy.get('must_appear') or focus_cids}; "
                f"offscreen={occupancy.get('offscreen') or offscreen_cids}; "
                f"must_not_appear={occupancy.get('must_not_appear')}; "
                f"featured={occupancy.get('featured') or featured_cids}. "
                f"doing={occupancy.get('doing') or doing_line}. "
                f"{str(occupancy.get('rule') or '')[:280]} "
            )
        crowd = shot.get("crowd_lock") if isinstance(shot.get("crowd_lock"), dict) else {}
        if not crowd and isinstance(occupancy.get("crowd_lock"), dict):
            crowd = occupancy["crowd_lock"]
        crowd_bits = ""
        if crowd:
            crowd_bits = (
                f"CROWD LOCK: present={crowd.get('present')}; "
                f"density={crowd.get('density')}; {str(crowd.get('rule') or '')[:220]} "
            )
        done_bits = ""
        scene_bits = ""
        if shot.get("scene_distinctness"):
            scene_bits = f"SCENE: {str(shot.get('scene_distinctness'))[:220]} "
        elif shot.get("setting_lock") and isinstance(shot.get("setting_lock"), dict):
            scene_bits = f"SCENE LOCK: {str((shot.get('setting_lock') or {}).get('rule') or '')[:220]} "
        detail_bits = (
            "DETAIL REQUIRED: screen L/R for each person in frame, gaze target, "
            "motion direction, relative props/landmarks. "
        )
        staging_prompt = (staging_bits + " ") if staging_bits else ""
        solo_ref_list = ", ".join(focus_char_nodes) or "none"
        first_of_setting = int(shot_ord_by_setting.get(setting_id) or 0) == 0
        layout_bits = (
            f"Reference shot, setting {setting_id}: character sheets {solo_ref_list} "
            f"for {cast_who}, then scene specs {scene_nid or 'scene'} as the room. "
            "Place those people into that empty room for THIS storyboard shot. "
            "Keep the film STYLE LOCK. "
        )
        if not first_of_setting:
            layout_bits += (
                f"CONTINUATION of setting {setting_id}: same room, same faces, same wardrobe. "
                "This window continues the story. "
            )
        if show_angle and view_key:
            shot_head = (
                f"Film shot {idx} (setting {setting_id}, timeline {timeline}, view {view_key}). "
            )
        else:
            shot_head = (
                f"Film shot {idx} (setting {setting_id}, timeline {timeline}). "
                "THIS time window only. "
            )
        shot_prompt_body = (
            shot_head
            + layout_bits
            + (f"WHO DOES WHAT: {doing_line}. " if doing_line else "")
            + f"People in frame: {cast_who}. "
            f"{lock_line} {specs_line} {scene_bits}"
            f"{staging_prompt}"
            f"Action: {action}. Camera: {camera}. "
            + (f"CONSISTENCY: {cont_bits}. " if cont_bits else "")
            + occ_bits
            + crowd_bits
            + done_bits
            + detail_bits
            + "ANTI-CLONE: one instance per named person. "
            + f"STRATEGY={keyframe_strategy}. first_of_setting={first_of_setting}. "
            + "Keep clothing / language / occupancy / position locks. "
            + "Keep this Wan prompt ≤4000 characters. "
            + "Real motion — animate this shot only."
        )
        identity_refs = {
            "character_ids": focus_cids,
            "character_node_ids": list(focus_char_nodes),
            "cast_names": list(focus_names),
            "costume_lock": costume_lock,
            "scene_node_id": scene_nid or None,
            "master_scene_node_id": scene_nid or None,
            "scene_master_frame_id": None,
            "is_scene_master": False,
            "prior_keyframe_node_id": None,
            "scene_prompt_handoff_from": scene_nid or None,
            "keyframe_strategy": keyframe_strategy,
            "setting_id": setting_id,
            "view_key": view_key,
            "spatial_lock": shot_spatial,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "on_screen": list(focus_cids),
            "offscreen": list(shot.get("offscreen") or []),
            "cast_actions": cast_actions or None,
            "setting_lock": shot.get("setting_lock")
            if isinstance(shot.get("setting_lock"), dict)
            else None,
            "scene_distinctness": shot.get("scene_distinctness"),
            "scene_continuity_mode": "scene_card_plus_clip_shots",
            "scene_specs": scene_specs or None,
            "first_of_setting": first_of_setting,
            "composed_scene": False,
            "style_lock": dict(film_style),
        }
        scene_n = setting_num.get(setting_id, 1)
        shot_ord_by_setting[setting_id] = int(shot_ord_by_setting.get(setting_id) or 0) + 1
        shot_n = shot_ord_by_setting[setting_id]
        shot_name = derive_shot_name(shot, fallback_index=idx)
        clip_label = label_clip(
            scene_number=scene_n, clip_number=shot_n, clip_name=shot_name
        )
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            normalize_speech_by_character,
            speech_line_from_by_character,
        )

        speech_by_character = normalize_speech_by_character(shot, characters)
        speech_line = speech_line_from_by_character(speech_by_character) or str(
            shot.get("speech_line") or shot.get("dialogue") or ""
        ).strip()
        # Film-wide / shot time-of-day on every shot config (story weave reads this).
        tod_shot: dict[str, str] = {}
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                infer_time_of_day_lock,
            )

            tod_shot = infer_time_of_day_lock(
                prompt_text,
                str(
                    (scene_specs or {}).get("scene_name")
                    or shot.get("setting_description")
                    or action
                    or ""
                ),
            )
            if isinstance(shot.get("time_of_day_lock"), dict):
                tod_shot = {
                    **tod_shot,
                    **{
                        k: str(v)
                        for k, v in shot["time_of_day_lock"].items()
                        if str(v).strip()
                    },
                }
            if isinstance(scene_specs, dict):
                if scene_specs.get("time_of_day") and (
                    not tod_shot.get("time_of_day")
                    or tod_shot.get("time_of_day") == "unspecified"
                ):
                    tod_shot["time_of_day"] = str(scene_specs.get("time_of_day"))
                if scene_specs.get("lighting") and not tod_shot.get("lighting"):
                    tod_shot["lighting"] = str(scene_specs.get("lighting"))
                # Keep specs lighting aligned with ToD when still generic.
                if tod_shot.get("lighting") and (
                    not scene_specs.get("lighting")
                    or "motivated key light"
                    in str(scene_specs.get("lighting") or "").lower()
                ):
                    scene_specs = dict(scene_specs)
                    scene_specs["lighting"] = tod_shot["lighting"]
                    if tod_shot.get("time_of_day"):
                        scene_specs.setdefault("time_of_day", tod_shot["time_of_day"])
        except Exception:  # noqa: BLE001
            tod_shot = {}
        shot_cfg: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": idx,
            "shot_title": shot.get("title"),
            "shot_action": action,
            "timeline": timeline,
            "camera": camera,
            "setting_id": setting_id,
            "view_key": view_key,
            "scene_node_id": scene_nid or None,
            "scene_specs": scene_specs or None,
            "character_ids": focus_cids,
            "on_screen": list(focus_cids),
            "offscreen": list(shot.get("offscreen") or []),
            "character_node_ids": focus_char_nodes,
            "cast_names": focus_names,
            "identity_refs": identity_refs,
            "costume_lock": costume_lock,
            "positioning_lock": staging.get("positioning_lock"),
            "action_lock": staging.get("action_lock"),
            "relationship_lock": staging.get("relationship_lock"),
            "blocking": shot.get("blocking") if isinstance(shot.get("blocking"), dict) else None,
            "cast_actions": cast_actions or None,
            "continuity_lock": consistency or None,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "already_done": already_done or None,
            "speech_line": speech_line,
            "speech_by_character": speech_by_character,
            "language_lock": str(
                shot.get("language_lock")
                or (analysis.get("language_lock") if isinstance(analysis, dict) else "")
                or (audio.get("language_lock") if isinstance(audio, dict) else "")
                or "en"
            ),
            "bgm_lock": (
                shot.get("bgm_lock")
                if isinstance(shot.get("bgm_lock"), dict)
                else (
                    analysis.get("bgm_lock")
                    if isinstance(analysis.get("bgm_lock"), dict)
                    else (audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else None)
                )
            ),
            "spatial_lock": shot_spatial,
            "time_of_day_lock": tod_shot or None,
            "master_scene_node_id": scene_nid or None,
            "keyframe_strategy": keyframe_strategy,
            "first_of_setting": first_of_setting,
            "composed_scene": False,
            "style_lock": dict(film_style),
            "video_size": film_video_size,
            "video_resolution": film_video_res,
            "generate": {"prompt": shot_prompt_body},
            "max_video_calls": 1,
            "inputs": clip_inputs,
            "agent_name": clip_label,
            "kind": "agent",
            "skill_id": "clip",
            "tools": ["call_video_model", "read_upstream"],
            "delegate": "agent",
            "director_task": (
                f"Shot {idx}: on-screen character sheets plus scene specs {scene_nid}. "
                "Write ONE positive story-form video prompt from THIS storyboard row "
                "(start_state → action/camera/speech → end_state). "
                "No LOCK banners, no negatives, no prior-shot paste. "
                "Then call_video_model with that prompt only."
            ),
        }
        # Storyboard owns consistency — never wire prior shot as a schedule/data parent.
        shot_cfg.pop("continuity_clip_node_id", None)
        shot_cfg["previous_clip_handoff_ready"] = False
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
                stamp_shot_states_on_clip_cfg,
            )

            shot_cfg = stamp_shot_states_on_clip_cfg(shot_cfg, shot=shot)
        except Exception:  # noqa: BLE001
            pass
        from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
            compose_practice_prompt,
        )

        seed_prompt_cfg = dict(shot_cfg)
        seed_prompt_cfg.pop("style_lock", None)
        shot_cfg["generate"] = {
            "prompt": compose_practice_prompt(
                cfg=seed_prompt_cfg,
                graph={"metadata": {"script_analysis": analysis}, "nodes": nodes},
                action=action,
                camera=camera,
            )
        }
        nodes.append(
            {
                "id": shot_id,
                "type": NODE_TYPE_VIDEO,
                "label": clip_label,
                "config": shot_cfg,
                "layout": {"x": 1320, "y": float(y), "width": 240, "height": 140},
            }
        )
        for src in clip_inputs:
            edges.append(_edge(f"e_{src}_{shot_id}", src, shot_id))
        prev_shot_by_setting[setting_id] = shot_id
        prev_shot_global = shot_id

    # Audio routing records backend capabilities but keeps sound clip-native.
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

    backends = detect_audio_backends()
    can_speech = bool(backends.get("can_speech"))
    can_music = bool(backends.get("can_music"))
    # Keep requested sound in clip-native prompts. Audio generation is not
    # implemented, so generated graphs must never add speech/music canvas nodes.
    clip_embedded = False
    if audio.get("policy") != "silent":
        want_speech = bool(audio.get("include_speech"))
        want_music = bool(
            audio.get("include_music")
            or audio.get("policy") in {"optional_music", "music", "speech_and_music"}
        )
        clip_embedded = want_speech or want_music
        if clip_embedded:
            from jiuwenswarm.server.runtime.designer.audio_locks import stamp_audio_fields_on_clip_config

            for n in nodes:
                if not isinstance(n, dict):
                    continue
                cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                if str(cfg.get("role") or "") != NODE_ROLE_CLIP:
                    continue
                idx = int(cfg.get("shot_index") or 0)
                shot_row = next(
                    (
                        s
                        for s in shots
                        if isinstance(s, dict) and int(s.get("shot_index") or 0) == idx
                    ),
                    {},
                )
                cfg = stamp_audio_fields_on_clip_config(
                    dict(cfg),
                    shot=shot_row if isinstance(shot_row, dict) else {},
                    analysis=analysis,
                    meta={"audio_intent": audio, "audio_routing": {"can_speech": can_speech, "can_music": can_music}},
                    clip_embedded=True,
                )
                n["config"] = cfg
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                    chain_prior_speech_across_clips,
                )

                # nodes is a flat list; wrap briefly as a graph for the chain helper.
                chain_prior_speech_across_clips({"nodes": nodes})
            except Exception:  # noqa: BLE001
                pass

    compose_inputs = list(shot_ids)
    nodes.append(
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": label_compose(story_name),
            "config": {
                "role": NODE_ROLE_COMPOSE,
                "inputs": compose_inputs,
                "agent_name": label_compose(story_name),
                "kind": "agent",
                "skill_id": "compose",
                "audio_policy": audio.get("policy"),
                "tools": ["ffmpeg_compose", "mix_audio", "read_upstream", "call_model"],
                "delegate": "agent",
                "director_task": (
                    "Output a real non-empty .mp4 only — never markdown. Use ffmpeg_compose tool."
                ),
            },
            "layout": {"x": 1620, "y": 180, "width": 260, "height": 150},
        }
    )
    for src in compose_inputs:
        edges.append(_edge(f"e_{src}_compose", src, "n_compose"))

    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title or "Designer Project",
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,  # type: ignore[typeddict-item]
        "edges": edges,  # type: ignore[typeddict-item]
        "metadata": {
            "bootstrap": "designer.graph.smart_video.quality.v5",
            "scenario": "video",
            "skill_guided": True,
            "script_analysis": analysis,
            "audio_intent": audio,
            "audio_routing": {
                "clip_embedded": bool(clip_embedded),
                "can_speech": can_speech,
                "can_music": can_music,
                "can_video_audio": bool(backends.get("can_video_audio", True)),
                "video_audio_model": str(backends.get("video_audio_model") or ""),
                "speech_nodes": False,
                "music_nodes": False,
            },
            "language_lock": str(
                analysis.get("language_lock") or audio.get("language_lock") or "en"
            ),
            "bgm_lock": (
                analysis.get("bgm_lock")
                if isinstance(analysis.get("bgm_lock"), dict)
                else (audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else {})
            ),
            "prefer_clip_native_audio": bool(clip_embedded),
            "prefer_wan3_clip_audio": bool(clip_embedded),  # legacy alias
            "scene_continuity_mode": "scene_card_plus_clip_shots",
            "scene_masters": dict(scene_master_by_setting),
            "scene_locks": dict(scene_locks_meta),
            # Storyboard must not rebuild the node set (that spawned extra nodes).
            "freeze_shot_topology": True,
            "combined_cast": False,
            "cast_layout": cast_layout,
            "spatial_lock": spatial_lock,
            "spatial_lock_by_setting": spatial_by_setting,
            "max_shots": len(shots),
            "target_shot_count": len(shots),
            "image_size": _IMAGE_SIZE,
            "max_image_calls_per_node": 1,
        },
        "created_at": now,
        "updated_at": now,
    }
    graph = normalize_execution_graph(graph)
    prune_non_contributing_nodes(graph)
    return attach_skills_metadata(graph, prompt_text)
