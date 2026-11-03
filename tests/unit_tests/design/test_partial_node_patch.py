# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Partial edits preserve a node's generation configuration and position."""

from copy import deepcopy

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    DesignerGraphValidationError, apply_graph_patch, normalize_execution_graph,
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
