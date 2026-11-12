# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Wan reference-mode predicates (scene specs + character specs).

Alibaba Wan R2V maps ``reference_urls`` order → ``character1``, ``character2``, …
(each person ref = one subject). Every clip, including the first of a setting:

  On-screen character spec sheets first (character1…), then the scene specs last
  as the room. No composed image and no scene-spec as first_frame.
  Prior-clip last frames are not attached.
"""

from __future__ import annotations

from typing import Any


def clip_uses_scene_specs(cfg: dict[str, Any] | None, graph: dict[str, Any] | None = None) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    if str(meta.get("scene_consistency_mode") or "") == "scene_specs_plus_clips":
        return True
    strategy = str(
        cfg.get("clip_strategy")
        or ((cfg.get("identity_refs") or {}) if isinstance(cfg.get("identity_refs"), dict) else {}).get(
            "clip_strategy"
        )
        or ""
    )
    return strategy == "clip_from_scene_and_character_specs" or bool(str(cfg.get("scene_node_id") or "").strip())
