# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A user-requested storyboard regeneration must actually re-author the storyboard.

Both paths used to conspire to make "重新生成分镜脚本。每个分镜至少 4 秒" a no-op:

* the handler replayed ``metadata.approved_storyboard`` verbatim on every run, so
  the written file was byte-identical to the previous one, and
* the node's own prompt was only a fallback behind the brief, so the new
  requirement never reached the model anyway.
"""

from jiuwenswarm.server.runtime.designer.handlers import text_nodes
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    _storyboard_rerun_requested,
    _storyboard_source,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext


def _ctx(*, run: dict | None = None) -> NodeExecutionContext:
    return NodeExecutionContext(
        graph={"graph_id": "graph_1", "nodes": [], "edges": []},
        run_id="run_1",
        node_id="n_storyboard",
        run=run if run is not None else {},
    )


def _node(prompt: str = "") -> dict:
    return {
        "id": "n_storyboard",
        "type": "table",
        "label": "Storyboard",
        "config": {"role": "storyboard", "pipeline": "storyboard", "prompt": prompt},
    }


def test_single_node_rerun_of_storyboard_rewrites_it() -> None:
    ctx = _ctx(
        run={"metadata": {"single_node_rerun": True, "scope_node_ids": ["n_storyboard"]}}
    )
    assert _storyboard_rerun_requested(ctx, _node()) is True


def test_plain_pipeline_execution_still_replays_the_approved_storyboard() -> None:
    assert _storyboard_rerun_requested(_ctx(run={"metadata": {}}), _node()) is False
    assert _storyboard_rerun_requested(_ctx(run={}), _node()) is False
    assert _storyboard_rerun_requested(_ctx(), _node()) is False


def test_rerun_scoped_to_another_node_is_not_this_node() -> None:
    ctx = _ctx(run={"metadata": {"single_node_rerun": True, "scope_node_ids": ["n_clip_1"]}})
    assert _storyboard_rerun_requested(ctx, _node()) is False


def test_single_node_rerun_without_a_scope_applies_to_the_node() -> None:
    ctx = _ctx(run={"metadata": {"single_node_rerun": True}})
    assert _storyboard_rerun_requested(ctx, _node()) is True


def test_malformed_run_metadata_does_not_crash() -> None:
    ctx = _ctx(run={"metadata": "not-a-dict"})
    assert _storyboard_rerun_requested(ctx, _node()) is False


def test_storyboard_source_carries_the_chat_requirement(monkeypatch) -> None:
    monkeypatch.setattr(
        text_nodes, "role_output_text", lambda ctx, role: "鹦鹉从树枝飞向另一棵树"
    )
    node = _node("鹦鹉从树枝飞向另一棵树\n分镜要求：每个分镜时长至少 4 秒")
    source = _storyboard_source(_ctx(), node)
    assert "至少 4 秒" in source
    assert "鹦鹉从树枝飞向另一棵树" in source
    # The Brief still carries the old per-shot timings, and the storyboard
    # instruction says to copy those exactly — so the new ask has to be marked
    # authoritative or the regenerated storyboard silently keeps the old timings.
    assert "authoritative" in source


def test_storyboard_source_does_not_repeat_an_unchanged_prompt(monkeypatch) -> None:
    monkeypatch.setattr(text_nodes, "role_output_text", lambda ctx, role: "unchanged text")
    assert _storyboard_source(_ctx(), _node("unchanged text")) == "unchanged text"


def test_storyboard_source_falls_back_to_the_node_prompt(monkeypatch) -> None:
    monkeypatch.setattr(text_nodes, "role_output_text", lambda ctx, role: "")
    assert _storyboard_source(_ctx(), _node("node only")) == "node only"


async def test_handler_reauthors_instead_of_replaying_the_approved_text(
    monkeypatch, tmp_path
) -> None:
    approved = "# Storyboard Scenario\n\n### Shot 1\n- Timeline: 0-2.5s\n"
    monkeypatch.setattr(
        text_nodes,
        "role_output_text",
        lambda ctx, role: "",
    )
    monkeypatch.setattr(text_nodes, "_sync_style_authority", lambda *a, **k: None)
    monkeypatch.setattr(text_nodes, "_stamp_bible_on_text", lambda text, ctx: text)
    monkeypatch.setattr(
        text_nodes, "sync_shot_nodes_from_storyboard_markdown", lambda *a, **k: None
    )
    monkeypatch.setattr(
        text_nodes, "write_workspace_text", lambda name, text: tmp_path / f"{name}.md"
    )

    async def fake_complete(prompt, **kwargs):
        return "# Storyboard Scenario\n\n### Shot 1\n- Timeline: 0-4s\n"

    monkeypatch.setattr(text_nodes, "complete_designer_node_text", fake_complete)
    from jiuwenswarm.server.runtime.designer.handlers import common

    monkeypatch.setattr(common, "role_output_text", lambda ctx, role: "")

    node = _node("每个分镜至少 4 秒")
    graph = {
        "graph_id": "graph_1",
        "nodes": [node],
        "edges": [],
        "metadata": {"approved_storyboard": approved},
    }

    plain = NodeExecutionContext(graph=graph, run_id="run_plain", node_id="n_storyboard", run={})
    result = await text_nodes.StoryboardNodeHandler().execute(node, plain)
    assert "storyboard written (director)" == result.message

    rerun = NodeExecutionContext(
        graph=graph,
        run_id="run_rerun",
        node_id="n_storyboard",
        run={"metadata": {"single_node_rerun": True, "scope_node_ids": ["n_storyboard"]}},
    )
    result = await text_nodes.StoryboardNodeHandler().execute(node, rerun)
    assert result.message == "storyboard table written"
