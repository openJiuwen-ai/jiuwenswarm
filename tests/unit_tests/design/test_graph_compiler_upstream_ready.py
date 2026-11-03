# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single-node rerun fail-fast when an ancestor has no produced output.

Old-executor semantics (executor.py _create_scoped_rerun): refuse a
single-node rerun whose ancestor is not completed-with-output instead of
silently rerunning the ancestor chain.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
from jiuwenswarm.server.runtime.design.graph_compiler import GraphScriptCompiler


def _graph():
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_upstream_ready",
        "project_id": "ut",
        "nodes": [
            {"id": "n_up", "type": "text", "label": "up",
             "config": {"prompt": "u", "pipeline": "brief"}},
            {"id": "n_down", "type": "text", "label": "down",
             "config": {"prompt": "d", "pipeline": "brief"}},
        ],
        "edges": [{"id": "e1", "source": "n_up", "target": "n_down"}],
    })


def _handler_graph():
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_handler_upstream",
        "project_id": "ut",
        "nodes": [
            {"id": "n_ref", "type": "text", "label": "ref",
             "config": {"prompt": "r", "pipeline": "brief",
                        "delegate": "handler", "skip_llm": True,
                        "read_only": True}},
            {"id": "n_down", "type": "text", "label": "down",
             "config": {"prompt": "d", "pipeline": "brief"}},
        ],
        "edges": [{"id": "e1", "source": "n_ref", "target": "n_down"}],
    })


def test_single_rerun_refuses_when_upstream_failed():
    run_outputs = {"n_up": {"status": "failed", "text": None, "error": "boom"}}
    with pytest.raises(ValueError, match="upstream not ready: n_up"):
        GraphScriptCompiler().compile(
            _graph(), run_outputs, node_ids=["n_down"]
        )


def test_single_rerun_refuses_when_upstream_never_ran():
    with pytest.raises(ValueError, match="upstream not ready: n_up"):
        GraphScriptCompiler().compile(_graph(), {}, node_ids=["n_down"])


def test_single_rerun_compiles_when_upstream_frozen():
    run_outputs = {"n_up": {"status": "completed", "text": "upstream text"}}
    script = GraphScriptCompiler().compile(_graph(), run_outputs, node_ids=["n_down"])
    assert 'label="n_down"' in script
    assert 'label="n_up"' not in script


def test_single_rerun_allows_uploaded_handler_upstream():
    script = GraphScriptCompiler().compile(
        _handler_graph(), {}, node_ids=["n_down"]
    )
    assert 'label="n_down"' in script


def test_cascade_rerun_refuses_when_upstream_failed():
    run_outputs = {"n_up": {"status": "failed", "text": None, "error": "boom"}}
    with pytest.raises(ValueError, match="upstream not ready: n_up"):
        GraphScriptCompiler().compile(
            _graph(), run_outputs, node_ids=["n_down"], cascade=True
        )


def test_cascade_rerun_compiles_when_upstream_frozen():
    run_outputs = {"n_up": {"status": "completed", "text": "upstream text"}}
    script = GraphScriptCompiler().compile(
        _graph(), run_outputs, node_ids=["n_down"], cascade=True
    )
    assert 'label="n_down"' in script
    assert 'label="n_up"' not in script


def test_full_run_still_executes_a_failed_upstream():
    run_outputs = {"n_up": {"status": "failed", "text": None, "error": "boom"}}
    script = GraphScriptCompiler().compile(_graph(), run_outputs)
    assert 'label="n_up"' in script
