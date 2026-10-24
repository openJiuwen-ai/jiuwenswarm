# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Partial edits preserve a node's generation configuration and position."""

from copy import deepcopy

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    DesignerGraphValidationError, apply_graph_patch, normalize_execution_graph,
    node_agent_template, node_pipeline, node_role,
)


def _graph():
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_patch", "project_id": "p_patch", "title": "patch",
        "nodes": [{"id": "n_image", "type": "image", "label": "Still",
                   "config": {"interaction_mode": "generate", "prompt": "old",
                              "generate": {"prompt": "old", "model": "image01", "seed": 7}},
                   "layout": {"x": 330, "y": 220},
                   "output_ref": {"kind": "image", "uri": "file:///old.png"}}],
        "edges": [],
    })


def test_partial_config_keeps_nested_settings_layout_and_output():
    graph = _graph()
    before = deepcopy(graph)
    result = apply_graph_patch(graph, {"upsert_nodes": [{
        "id": "n_image", "type": "image", "config": {"generate": {"prompt": "new"}},
    }]})
    node = result["nodes"][0]
    assert node["config"]["generate"] == {"prompt": "new", "model": "image01", "seed": 7}
    assert node["config"]["interaction_mode"] == "generate"
    assert node["layout"] == before["nodes"][0]["layout"]
    assert node["output_ref"] == before["nodes"][0]["output_ref"]
    assert graph == before


def test_existing_node_can_be_renamed_without_repeating_type_or_config():
    result = apply_graph_patch(_graph(), {"upsert_nodes": [{"id": "n_image", "label": "New still"}]})
    assert result["nodes"][0]["label"] == "New still"
    assert result["nodes"][0]["type"] == "image"


def test_explicit_output_clear_and_empty_list_replace_existing_values():
    graph = _graph()
    graph["nodes"][0]["config"]["materials"] = [{"uri": "file:///ref.png"}]
    result = apply_graph_patch(graph, {"upsert_nodes": [{
        "id": "n_image", "output_ref": None, "config": {"materials": []}, "layout": {"x": 440},
    }]})
    assert result["nodes"][0]["output_ref"] is None
    assert result["nodes"][0]["config"]["materials"] == []
    assert result["nodes"][0]["layout"] == {"x": 440, "y": 220}


def test_new_node_still_requires_a_type():
    with pytest.raises(DesignerGraphValidationError, match="node.type"):
        apply_graph_patch(_graph(), {"upsert_nodes": [{"id": "new"}]})


@pytest.mark.parametrize(
    ("node_id", "old_type", "new_type", "new_template"),
    [
        ("n_image", "image", "video", "clip"),
        ("n_character", "image", "video", "clip"),
        ("n_clip_1", "video", "image", "character"),
    ],
)
def test_type_change_replaces_old_execution_config_and_dispatch(
    node_id: str, old_type: str, new_type: str, new_template: str,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import resolve_handler_key

    graph = _graph()
    old = graph["nodes"][0]
    old["id"] = node_id
    old["type"] = old_type
    old["config"]["role"] = old_type
    old["config"]["agent_template"] = "old-custom-template"
    graph = normalize_execution_graph(graph)
    before = deepcopy(graph)

    result = apply_graph_patch(graph, {"upsert_nodes": [{"id": node_id, "type": new_type}]})
    node = result["nodes"][0]
    assert node["type"] == new_type
    assert node_role(node) == new_type
    assert node_pipeline(node) == ""
    assert node_agent_template(node) == new_template
    assert resolve_handler_key(node) == new_type
    assert node["config"] == {"role": new_type}
    assert node["layout"] == {}
    assert node.get("output_ref") is None
    assert graph == before


def test_type_change_uses_explicit_replacement_config() -> None:
    from jiuwenswarm.server.runtime.designer.handlers import resolve_handler_key

    patch = {
        "id": "n_image", "type": "video", "label": "New clip",
        "config": {"pipeline": "clip", "agent_template": "custom-video",
                   "generate": {"model": "video01", "prompt": "new clip"}},
        "layout": {"x": 500, "y": 100},
    }
    node = apply_graph_patch(_graph(), {"upsert_nodes": [patch]})["nodes"][0]
    assert node_role(node) == "video"
    assert node_pipeline(node) == "clip"
    assert node_agent_template(node) == "custom-video"
    assert resolve_handler_key(node) == "clip"
    assert node["config"]["generate"] == patch["config"]["generate"]
    assert node["label"] == "New clip"
    assert node["layout"] == patch["layout"]
    assert node.get("output_ref") is None


def test_same_type_with_whitespace_keeps_partial_update_semantics() -> None:
    graph = _graph()
    node = apply_graph_patch(graph, {"upsert_nodes": [{
        "id": "n_image", "type": " image ", "label": "Renamed",
    }]})["nodes"][0]
    assert node["type"] == "image"
    assert node["config"] == graph["nodes"][0]["config"]
    assert node["layout"] == graph["nodes"][0]["layout"]


def test_empty_object_patch_keeps_existing_config_and_layout() -> None:
    graph = _graph()
    node = apply_graph_patch(graph, {"upsert_nodes": [{
        "id": "n_image", "config": {}, "layout": {},
    }]})["nodes"][0]
    assert node == graph["nodes"][0]


@pytest.mark.parametrize("node_type", [None, "", "unknown", 42])
def test_existing_node_rejects_invalid_explicit_type(node_type: object) -> None:
    with pytest.raises(DesignerGraphValidationError, match="node.type|node type"):
        apply_graph_patch(_graph(), {"upsert_nodes": [{"id": "n_image", "type": node_type}]})
