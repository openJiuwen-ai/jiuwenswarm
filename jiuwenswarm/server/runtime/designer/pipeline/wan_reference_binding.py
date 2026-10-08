# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Wan reference-mode binding labels (scene specs + character solos).

Alibaba Wan R2V maps ``reference_urls`` order → ``character1``, ``character2``, …
(each person ref = one subject). Every clip, including the first of a setting:

  On-screen solo sheets first (character1…), then the scene specs last
  as the room. No composed still and no keyframe as first_frame.
  Prior-clip last frames are not attached.
"""

from __future__ import annotations

from typing import Any


def _scene_label_from_graph(
    graph: dict[str, Any] | None,
    *,
    scene_nid: str,
    setting_id: str,
    cfg: dict[str, Any],
) -> str:
    graph = graph if isinstance(graph, dict) else {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != scene_nid:
            continue
        label = str(node.get("label") or "").strip()
        if label.lower().startswith("scene "):
            return label
        cfg_n = node.get("config") if isinstance(node.get("config"), dict) else {}
        num = 1
        if scene_nid.startswith("n_scene_"):
            suffix = scene_nid.rsplit("_", 1)[-1]
            if suffix.isdigit():
                num = int(suffix)
        name = str(
            cfg_n.get("setting_id")
            or setting_id
            or cfg.get("shot_title")
            or f"Scene {num}"
        ).strip()
        return f"Scene {num}: {name}"
    num = 1
    if scene_nid.startswith("n_scene_"):
        suffix = scene_nid.rsplit("_", 1)[-1]
        if suffix.isdigit():
            num = int(suffix)
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    masters = meta.get("scene_masters") if isinstance(meta.get("scene_masters"), dict) else {}
    for sid, nid in masters.items():
        if str(nid) == scene_nid:
            setting_id = str(sid)
            break
    place = str(setting_id or "Scene").replace("set_", "Scene ").replace("scene_", "Scene ")
    return f"Scene {num}: {place}"


def build_wan_reference_binding(
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
    prior_last_frame: bool = False,
    prior_last_frame_count: int = 0,
) -> str:
    """Return a short Wan attach-order / label block for clip prompts (reference mode)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    chain = (
        cfg.get("scene_last_frame_chain")
        if isinstance(cfg.get("scene_last_frame_chain"), list)
        else []
    )
    n_hist = int(prior_last_frame_count or 0) or len(
        [c for c in chain if isinstance(c, dict) and str(c.get("path") or "").strip()]
    )
    if prior_last_frame or bool(cfg.get("use_prior_last_frame")) or n_hist > 0:
        prior_last_frame = True
        n_hist = max(1, n_hist) if prior_last_frame else n_hist
    scene_nid = str(
        cfg.get("scene_node_id")
        or ((cfg.get("identity_refs") or {}) if isinstance(cfg.get("identity_refs"), dict) else {}).get(
            "scene_node_id"
        )
        or ""
    ).strip()
    setting_id = str(cfg.get("setting_id") or "").strip()
    cast_names = [str(x).strip() for x in (cfg.get("cast_names") or []) if str(x).strip()]
    if not cast_names:
        irefs = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        cast_names = [str(x).strip() for x in (irefs.get("cast_names") or []) if str(x).strip()]
    char_ids = [str(x).strip() for x in (cfg.get("character_ids") or cfg.get("on_screen") or []) if str(x).strip()]
    cast_actions = cfg.get("cast_actions") if isinstance(cfg.get("cast_actions"), dict) else {}

    scene_label = ""
    if scene_nid or setting_id:
        scene_label = _scene_label_from_graph(
            graph, scene_nid=scene_nid or "n_scene_1", setting_id=setting_id, cfg=cfg
        )

    lines = [
        "[REFERENCE MODE — character sheets, then empty scene]:",
        "Attach order: on-screen solo sheets as character1, character2, … "
        "then the scene specs last as the room.",
        "The scene specs has no people. Place character1… into that room for this shot. "
        "Match faces, wardrobe, and the film STYLE LOCK.",
    ]
    ref_i = 1
    if prior_last_frame and n_hist > 0:
        lines.append(
            f"- Prior ending stills are not attached ({n_hist} kept as story state only). "
            "This shot continues in the same room."
        )

    names = cast_names or char_ids
    for i, name in enumerate(names, start=1):
        action = ""
        if char_ids and i <= len(char_ids):
            action = str(cast_actions.get(char_ids[i - 1]) or "").strip()
        who = f"character{i}"
        bit = (
            f"- reference {ref_i} / {who}: {name} — solo identity sheet "
            "(face + wardrobe). One instance."
        )
        if action:
            bit += f" Action this window: {action}."
        lines.append(bit)
        ref_i += 1

    if not names:
        lines.append(
            f"- reference {ref_i}+ : on-screen character solo sheets "
            "(bind as character1, character2, …)."
        )
        ref_i += 1
    if scene_label:
        lines.append(
            f"- last reference: {scene_label} — empty environment plate "
            "(architecture, furniture, light). No people in that image."
        )
    lines.append(
        "In the motion prompt, name character1/character2 for people. "
        "Keep the full prompt ≤4000 characters."
    )
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
            contact_anti_penetration_clause,
            wan_r2v_prompt_formula,
            wan_reference_media_rules,
        )

        lines.append(wan_r2v_prompt_formula())
        lines.append(wan_reference_media_rules(prior_ending_count=n_hist))
        lines.append(contact_anti_penetration_clause(for_clip=True))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def clip_is_first_of_setting(
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
) -> bool:
    """True for the first clip of this setting_id (scene specs + solos)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    flag = cfg.get("first_of_setting")
    if flag is True:
        return True
    if flag is False:
        return False
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    scene_card = (
        str(meta.get("scene_continuity_mode") or "") == "scene_card_plus_clip_shots"
        or bool(str(cfg.get("scene_node_id") or "").strip())
    )
    if not scene_card:
        return False
    sid = str(cfg.get("setting_id") or "").strip()
    idx = int(cfg.get("shot_index") or 0) or 0
    earlier_same = False
    any_earlier_clip = False
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        oc = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(oc.get("role") or "") != "clip":
            continue
        other_idx = int(oc.get("shot_index") or 0) or 0
        if other_idx <= 0 or (idx and other_idx >= idx):
            continue
        any_earlier_clip = True
        other_sid = str(oc.get("setting_id") or "").strip()
        if sid and other_sid and other_sid != sid:
            continue
        earlier_same = True
        break
    if earlier_same:
        return False
    # Isolated shot 2+ in a unit graph is a later clip, not a setting opener.
    if idx > 1 and not any_earlier_clip:
        return False
    return True


def clip_uses_scene_card(cfg: dict[str, Any] | None, graph: dict[str, Any] | None = None) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    if str(meta.get("scene_continuity_mode") or "") == "scene_card_plus_clip_shots":
        return True
    strategy = str(
        cfg.get("keyframe_strategy")
        or ((cfg.get("identity_refs") or {}) if isinstance(cfg.get("identity_refs"), dict) else {}).get(
            "keyframe_strategy"
        )
        or ""
    )
    return strategy == "clip_from_scene_and_solos" or bool(str(cfg.get("scene_node_id") or "").strip())
