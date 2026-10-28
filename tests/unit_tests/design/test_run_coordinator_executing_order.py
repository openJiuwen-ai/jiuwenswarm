# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""_executing_nodes order: graph order, so node_states seeds predictably.

The caller pre-seeds run.node_states by iterating the return value; that
dict's key order lands in the persisted run JSON. A set here leaked a
per-process hash order into durable data.
"""

from __future__ import annotations

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.design.run_coordinator import RunCoordinator


def _graph() -> DesignerExecutionGraph:
    # Node order deliberately not topological: the contract is graph order,
    # not execution order.
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "graph_ut_order",
        "project_id": "ut",
        "nodes": [
            {"id": nid, "type": "text", "config": {"prompt": nid}}
            for nid in ("n_brief", "n_compose", "n_character", "n_scene", "n_clip", "n_shot")
        ],
        "edges": [
            {"id": "e1", "source": "n_brief", "target": "n_character"},
            {"id": "e2", "source": "n_brief", "target": "n_scene"},
            {"id": "e3", "source": "n_character", "target": "n_clip"},
            {"id": "e4", "source": "n_scene", "target": "n_clip"},
            {"id": "e5", "source": "n_clip", "target": "n_compose"},
            {"id": "e6", "source": "n_scene", "target": "n_shot"},
        ],
    })


def test_full_run_executes_every_node_in_graph_order():
    ids = RunCoordinator._executing_nodes(_graph(), node_ids=None, cascade=False)
    assert ids == ["n_brief", "n_compose", "n_character", "n_scene", "n_clip", "n_shot"]


def test_single_node_trigger_returns_only_the_target():
    ids = RunCoordinator._executing_nodes(_graph(), node_ids=["n_scene"], cascade=False)
    assert ids == ["n_scene"]


def test_cascade_returns_target_plus_downstream_closure_in_graph_order():
    ids = RunCoordinator._executing_nodes(_graph(), node_ids=["n_character"], cascade=True)
    # Downstream closure of n_character is n_clip -> n_compose; output keeps
    # graph order (n_compose is listed before n_character in the graph).
    assert ids == ["n_compose", "n_character", "n_clip"]


def test_node_states_seed_follows_graph_order():
    graph = _graph()
    executing_ids = RunCoordinator._executing_nodes(graph, node_ids=None, cascade=False)
    run: dict = {}
    for node_id in executing_ids:
        run.setdefault("node_states", {}).setdefault(
            node_id, {"status": "pending", "started_at": None, "completed_at": None}
        )
    assert list(run["node_states"]) == [
        "n_brief", "n_compose", "n_character", "n_scene", "n_clip", "n_shot"
    ]
