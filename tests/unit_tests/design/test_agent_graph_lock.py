<<<<<<< HEAD
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Agent edits respect the same execution lock as the canvas."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
from jiuwenswarm.server.runtime.design.context import StoreNodeSpawner


@pytest.mark.parametrize("active", [True, False])
def test_agent_edit_is_blocked_only_while_a_trigger_is_active(active):
    graph = normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_locked", "project_id": "p_locked", "title": "before", "nodes": [], "edges": [],
    })
    store = SimpleNamespace(get_graph=Mock(return_value=graph), save_graph=Mock(side_effect=lambda g: g))
    coordinator = SimpleNamespace(is_active=lambda: active)
    pushed = Mock()
    spawner = StoreNodeSpawner(store, "g_locked", lambda: coordinator, on_graph_update=pushed)
    if active:
        with pytest.raises(RuntimeError, match="executing"):
            spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        store.save_graph.assert_not_called()
        pushed.assert_not_called()
    else:
        result = spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        assert result["title"] == "after"
        store.save_graph.assert_called_once()
        pushed.assert_called_once()
||||||| e487e9a30
=======
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Agent edits respect the same execution lock as the canvas."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
from jiuwenswarm.server.runtime.design.context import StoreNodeSpawner


@pytest.mark.parametrize("active", [True, False])
def test_agent_edit_is_blocked_only_while_a_trigger_is_active(active):
    graph = normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_locked", "project_id": "p_locked", "title": "before", "nodes": [], "edges": [],
    })
    store = SimpleNamespace(get_graph=Mock(return_value=graph), save_graph=Mock(side_effect=lambda g: g))
    coordinator = SimpleNamespace(is_active=lambda: active)
    pushed = Mock()
    spawner = StoreNodeSpawner(store, "g_locked", lambda: coordinator, on_graph_update=pushed)
    if active:
        with pytest.raises(RuntimeError, match="executing"):
            spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        store.save_graph.assert_not_called()
        pushed.assert_not_called()
    else:
        result = spawner.apply_agent_graph_patch("g_locked", {"title": "after"})
        assert result["title"] == "after"
        store.save_graph.assert_called_once()
        pushed.assert_called_once()


@pytest.mark.asyncio
async def test_node_run_reports_a_trigger_refused_before_the_node_started():
    refused = {
        "status": "failed",
        "error": "ValueError: upstream not ready: n_brief",
        "node_states": {"n_music": {"status": "pending"}},
    }
    store = SimpleNamespace(get_graph=Mock(return_value={"graph_id": "g_bgm"}))
    coordinator = SimpleNamespace(is_active=lambda: False, start=AsyncMock(return_value=refused))
    spawner = StoreNodeSpawner(store, "g_bgm", lambda: coordinator)

    result = await spawner.spawn_node_agent("run_bgm", "n_music")

    assert result == "ValueError: upstream not ready: n_brief"
>>>>>>> origin/develop
