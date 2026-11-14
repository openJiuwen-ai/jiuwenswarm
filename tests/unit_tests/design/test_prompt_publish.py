# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scene/character prompts must reach the canvas before image generation finishes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit_tests.design.graph_fixtures import make_pipeline_graph


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

    return DesignerGraphStore()


def test_persist_and_publish_writes_prompt_before_returning(designer_store, monkeypatch):
    from jiuwenswarm.server.runtime.designer import prompt_publish

    graph = make_pipeline_graph(project_id="proj_prompt01", prompt="a quiet kitchen")
    scene = next(node for node in graph["nodes"] if node["id"] == "n_scene")
    scene["config"]["prompt"] = "Furniture in an empty room."
    scene["config"]["generate"] = {"prompt": "Furniture in an empty room."}
    saved = designer_store.save_graph(graph)

    # A concurrent layout save moved the scene card while generation is running.
    moved = designer_store.get_graph(saved["graph_id"])
    assert moved is not None
    moved_scene = next(node for node in moved["nodes"] if node["id"] == "n_scene")
    moved_scene["layout"] = {**(moved_scene.get("layout") or {}), "x": 99, "y": 42}
    designer_store.save_graph(moved)

    live = {**saved, "nodes": [dict(node) for node in saved["nodes"]]}
    live_scene = next(node for node in live["nodes"] if node["id"] == "n_scene")
    live_scene["config"] = dict(live_scene.get("config") or {})
    live_scene["layout"] = {**(live_scene.get("layout") or {}), "x": 10, "y": 10}

    pushed: list[dict] = []
    monkeypatch.setattr(prompt_publish, "_push_saved_graph", lambda graph: pushed.append(graph))

    real_prompt = "A sunlit kitchen with two people at the table, warm afternoon light."
    result = prompt_publish.persist_and_publish_node_prompt(live, "n_scene", real_prompt)

    assert result is not None
    assert pushed and str(pushed[0]["nodes"][0].get("id") or "")  # push fired
    stored = designer_store.get_graph(saved["graph_id"])
    assert stored is not None
    scene_cfg = next(node for node in stored["nodes"] if node["id"] == "n_scene")
    assert scene_cfg["config"]["last_wan_prompt"] == real_prompt
    assert scene_cfg["config"]["generate"]["prompt"] == real_prompt
    assert scene_cfg["layout"]["x"] == 99
    assert scene_cfg["layout"]["y"] == 42
    # The in-memory execution node is stamped too, so later completion saves agree.
    assert live_scene["config"]["last_wan_prompt"] == real_prompt


def test_emit_prompt_artifact_prefers_callback(monkeypatch):
    from jiuwenswarm.server.runtime.designer import prompt_publish
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    calls: list[str] = []
    monkeypatch.setattr(
        prompt_publish,
        "persist_and_publish_node_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": []},
        run_id="r1",
        node_id="n_scene",
        on_prompt_artifact=calls.append,
    )
    prompt_publish.emit_prompt_artifact(ctx, "  real scene prompt  ")
    assert calls == ["real scene prompt"]


def test_emit_prompt_artifact_falls_back_to_persist(monkeypatch):
    from jiuwenswarm.server.runtime.designer import prompt_publish
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    seen: list[tuple] = []
    monkeypatch.setattr(
        prompt_publish,
        "persist_and_publish_node_prompt",
        lambda graph, node_id, prompt=None: seen.append((graph["graph_id"], node_id, prompt)),
    )
    graph = {"graph_id": "g2", "nodes": []}
    ctx = NodeExecutionContext(graph=graph, run_id="r1", node_id="n_scene")
    prompt_publish.emit_prompt_artifact(ctx, "kitchen with people")
    assert seen == [("g2", "n_scene", "kitchen with people")]


@pytest.mark.asyncio
async def test_require_image_publishes_prompt_before_the_model_returns(
    designer_store, monkeypatch, tmp_path: Path
):
    from jiuwenswarm.server.runtime.designer.handlers import image_nodes
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    graph = make_pipeline_graph(project_id="proj_prompt02", prompt="empty hall")
    scene = next(node for node in graph["nodes"] if node["id"] == "n_scene")
    scene["config"]["prompt"] = "Furniture, walls, windows as a clear empty room."
    saved = designer_store.save_graph(graph)
    out = tmp_path / "scene.png"
    out.write_bytes(b"\x89PNG\r\n\x1a\n")

    seen_during_call: list[str] = []

    async def fake_generate(prompt, **kwargs):
        stored = designer_store.get_graph(saved["graph_id"])
        cfg = next(node for node in stored["nodes"] if node["id"] == "n_scene")["config"]
        seen_during_call.append(str(cfg.get("last_wan_prompt") or ""))
        return {"image_path": str(out)}

    monkeypatch.setattr(image_nodes.handler_io, "generate_designer_image", fake_generate)
    monkeypatch.setattr(image_nodes.handler_io, "note_media_settings_gap", lambda *a, **k: None)

    ctx = NodeExecutionContext(graph=saved, run_id="run_1", node_id="n_scene")
    real = "A crowded train hall with warm overhead lamps and commuters."
    await image_nodes._require_image(prompt=real, stem="scene", ctx=ctx)
    assert seen_during_call == [real]
