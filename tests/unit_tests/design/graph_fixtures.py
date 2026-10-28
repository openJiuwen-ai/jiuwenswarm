# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Designer graph fixtures for unit tests (not production bootstrap)."""

from __future__ import annotations

from jiuwenswarm.common.schema.designer_graph import (
    EDGE_KIND_DATA,
    GRAPH_SOURCE_PROMPT,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    PIPELINE_BRIEF,
    PIPELINE_CHARACTER,
    PIPELINE_CLIP,
    PIPELINE_COMPOSE,
    PIPELINE_SCENE,
    PIPELINE_STORYBOARD,
    SCHEMA_VERSION,
    DesignerExecutionGraph,
    DesignerGraphEdge,
    DesignerGraphNode,
    new_graph_id,
    normalize_execution_graph,
    utc_now_ms,
)


def make_pipeline_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
) -> DesignerExecutionGraph:
    """Minimal brief→character/scene/storyboard→clip→compose DAG for tests."""
    graph_id = new_graph_id()
    now = utc_now_ms()
    prompt_text = prompt.strip()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
    nodes: list[DesignerGraphNode] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "Text 1",
            "config": {
                "role": NODE_TYPE_TEXT,
                "pipeline": PIPELINE_BRIEF,
                "prompt": prompt_text,
            },
            "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "Image 1",
            "config": {
                "role": NODE_TYPE_IMAGE,
                "pipeline": PIPELINE_CHARACTER,
                "inputs": ["n_brief"],
            },
            "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "Scene 1: Place",
            "config": {
                "role": NODE_TYPE_IMAGE,
                "pipeline": PIPELINE_SCENE,
                "inputs": ["n_brief", "n_storyboard"],
            },
            "layout": {"x": 760, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "Table 1",
            "config": {
                "role": NODE_TYPE_TABLE,
                "pipeline": PIPELINE_STORYBOARD,
                "inputs": ["n_brief"],
            },
            "layout": {"x": 400, "y": 440, "width": 280, "height": 160},
        },
        {
            "id": "n_clip_1",
            "type": NODE_TYPE_VIDEO,
            "label": "Video 1",
            "config": {
                "role": NODE_TYPE_VIDEO,
                "pipeline": PIPELINE_CLIP,
                "clip_index": 1,
                "inputs": ["n_character", "n_scene", "n_storyboard"],
            },
            "layout": {"x": 1120, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "Video 2",
            "config": {
                "role": NODE_TYPE_VIDEO,
                "pipeline": PIPELINE_COMPOSE,
                "inputs": ["n_clip_1"],
            },
            "layout": {"x": 1480, "y": 240, "width": 280, "height": 160},
        },
    ]
    edges: list[DesignerGraphEdge] = [
        {"id": "e_brief_character", "source": "n_brief", "target": "n_character", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_storyboard", "source": "n_brief", "target": "n_storyboard", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_scene", "source": "n_brief", "target": "n_scene", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_scene", "source": "n_storyboard", "target": "n_scene", "kind": EDGE_KIND_DATA},
        {"id": "e_character_n_clip_1", "source": "n_character", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_scene_n_clip_1", "source": "n_scene", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_n_clip_1", "source": "n_storyboard", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_n_clip_1_compose", "source": "n_clip_1", "target": "n_compose", "kind": EDGE_KIND_DATA},
    ]
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title,
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "scene_consistency_mode": "scene_specs_plus_clips",
        },
        "created_at": now,
        "updated_at": now,
    }
    return normalize_execution_graph(graph)
