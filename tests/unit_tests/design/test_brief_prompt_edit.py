# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A chat edit of the brief prompt reaches the brief after the director approved one."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_TYPE_TEXT,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.design.context import StoreNodeSpawner
from jiuwenswarm.server.runtime.designer.handlers import text_nodes
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

_APPROVED = "# Brief\n\nThe barista opens the cafe door on a sunny morning."


def _graph(approved: str = _APPROVED) -> dict:
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_brief",
        "project_id": "p_brief",
        "title": "brief",
        "description": "A barista opens the cafe door.",
        "nodes": [
            {
                "id": "n_brief",
                "type": NODE_TYPE_TEXT,
                "label": "Brief",
                "config": {"role": "brief", "prompt": "Write the brief."},
            },
            {
                "id": "n_note",
                "type": NODE_TYPE_TEXT,
                "label": "Note",
                "config": {"prompt": "A note."},
            },
        ],
        "edges": [],
        "metadata": {"approved_brief": approved} if approved else {},
    })


def _patch(graph: dict, node_id: str, prompt: str) -> dict:
    store = SimpleNamespace(get_graph=Mock(return_value=graph), save_graph=Mock(side_effect=lambda g: g))
    spawner = StoreNodeSpawner(store, "g_brief", lambda: SimpleNamespace(is_active=lambda: False))
    node = next(node for node in graph["nodes"] if node["id"] == node_id)
    edited = {
        "id": node_id,
        "type": node["type"],
        "label": node["label"],
        "config": {**node["config"], "prompt": prompt},
    }
    saved = spawner.apply_agent_graph_patch("g_brief", {"upsert_nodes": [edited]})
    return {node["id"]: node["config"] for node in saved["nodes"]}


def test_brief_prompt_patch_is_kept_as_user_edit():
    configs = _patch(_graph(), "n_brief", "Write the brief. Set the story on a rainy day.")

    assert configs["n_brief"]["user_edit_prompt"] == "Write the brief. Set the story on a rainy day."


@pytest.mark.parametrize(
    ("approved", "node_id", "prompt"),
    [
        ("", "n_brief", "Write the brief. Set the story on a rainy day."),
        (_APPROVED, "n_note", "A rainy note."),
        (_APPROVED, "n_brief", "Write the brief."),
    ],
    ids=["no_approved_brief", "other_node", "same_prompt"],
)
def test_other_patches_leave_no_user_edit(approved, node_id, prompt):
    configs = _patch(_graph(approved), node_id, prompt)

    assert all("user_edit_prompt" not in cfg for cfg in configs.values())


async def _run_brief(graph: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[str]]:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    prompts: list[str] = []

    async def complete(prompt: str, *, delegate: str = "") -> str:
        prompts.append(prompt)
        return "# Brief\n\nThe barista opens the cafe door on a rainy morning."

    monkeypatch.setattr(text_nodes, "complete_designer_node_text", complete)
    node = next(node for node in graph["nodes"] if node["id"] == "n_brief")
    result = await text_nodes.BriefNodeHandler().execute(
        node,
        NodeExecutionContext(graph=graph, run_id="run_brief", node_id="n_brief"),
    )
    assert result.output_ref is not None
    return (tmp_path / Path(result.output_ref["label"])).read_text(encoding="utf-8"), prompts


@pytest.mark.asyncio
async def test_brief_rerun_revises_the_approved_brief_with_the_edit(tmp_path, monkeypatch):
    graph = _graph()
    graph["nodes"][0]["config"]["user_edit_prompt"] = "Set the story on a rainy day."

    text, prompts = await _run_brief(graph, tmp_path, monkeypatch)

    assert "rainy morning" in text
    assert "sunny morning" not in text
    assert len(prompts) == 1
    assert "Set the story on a rainy day." in prompts[0]
    assert _APPROVED in prompts[0]


@pytest.mark.asyncio
async def test_brief_rerun_without_edit_keeps_the_approved_brief(tmp_path, monkeypatch):
    text, prompts = await _run_brief(_graph(), tmp_path, monkeypatch)

    assert "sunny morning" in text
    assert prompts == []
