# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Graphs saved by another build (pipeline "character_design") still load."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    PIPELINE_CHARACTER,
    DesignerGraphValidationError,
    normalize_execution_graph,
    normalize_node_config,
)


def test_character_design_pipeline_reads_as_character() -> None:
    assert normalize_node_config({"pipeline": "character_design", "role": "image"})["pipeline"] == PIPELINE_CHARACTER
    assert normalize_node_config({"role": "character_design"}) == {"pipeline": PIPELINE_CHARACTER, "role": "image"}


def test_graph_with_aliased_node_loads() -> None:
    graph = normalize_execution_graph(
        {
            "schema_version": "designer-execution-graph.v1",
            "graph_id": "graph_alias",
            "project_id": "default",
            "title": "t",
            "nodes": [
                {"id": "n_character", "type": "image", "label": "Character 1",
                 "config": {"pipeline": "character_design", "role": "image"}},
            ],
            "edges": [],
        }
    )
    assert graph["nodes"][0]["config"]["pipeline"] == PIPELINE_CHARACTER


def test_unknown_pipeline_is_still_rejected() -> None:
    with pytest.raises(DesignerGraphValidationError, match="unsupported node pipeline"):
        normalize_node_config({"pipeline": "not_a_pipeline"})
