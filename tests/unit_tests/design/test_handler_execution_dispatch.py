# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""ComfyUI nodes execute handlers through the live coordinator backend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge
from jiuwenswarm.server.runtime.design.media_guarantee import MediaGuaranteeBackend
from jiuwenswarm.server.runtime.designer.handlers.types import NodeResult


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_handler_produces_assets_without_calling_an_agent(monkeypatch, tmp_path, kind):
    node = {"id": "n_comfy", "type": kind, "config": {"delegate": "handler", "is_comfyui": True, "role": kind}}
    graph = {"graph_id": "g_handler", "nodes": [node], "edges": []}
    run = {"run_id": "run_handler", "node_states": {}}
    bridge = DesignEventBridge(run, None, graph=graph)
    inner = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("agent must not run")))
    suffix = "png" if kind == "image" else "mp4"
    (tmp_path / f"out.{suffix}").write_bytes(b"media" * 256)
    ref = {"kind": kind, "mime_type": f"{kind}/{suffix}", "uri": (tmp_path / f"out.{suffix}").as_uri()}
    handler = SimpleNamespace(execute=AsyncMock(return_value=NodeResult(output_ref=ref, message="generated")))
    monkeypatch.setattr("jiuwenswarm.server.runtime.designer.handlers.get_node_handler", lambda _: handler)
    backend = MediaGuaranteeBackend(inner, graph=graph, run=run, bridge=bridge, media_root=tmp_path)

    result = await backend.run("compiled prompt", {"label": "n_comfy"}, {"type": "object"})

    assert result.skipped is not True
    assert result.structured["output_ref"] == ref
    inner.run.assert_not_called()
    handler.execute.assert_awaited_once()
    ctx = handler.execute.call_args.args[1]
    assert ctx.graph is graph
    assert ctx.run is run
    assert ctx.media_dir == tmp_path
    # Unified write: the handler's product lands in the node's current slots
    # for every trigger type (no candidate fields anymore).
    state = run["node_states"]["n_comfy"]
    assert state["output_ref"] == ref
    assert state["output_refs"] == [ref]


async def test_handler_failure_is_reported_without_retrying_an_agent(monkeypatch):
    graph = {"graph_id": "g_handler", "nodes": [{"id": "n_comfy", "type": "video",
             "config": {"delegate": "handler", "is_comfyui": True}}]}
    run = {"run_id": "run_handler", "node_states": {}}
    bridge = DesignEventBridge(run, None, graph=graph)
    inner = SimpleNamespace(run=AsyncMock())
    handler = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("vLLM service unavailable")))
    monkeypatch.setattr("jiuwenswarm.server.runtime.designer.handlers.get_node_handler", lambda _: handler)
    backend = MediaGuaranteeBackend(inner, graph=graph, run=run, bridge=bridge)

    result = await backend.run("prompt", {"label": "n_comfy"}, None)

    assert result.skipped is True
    inner.run.assert_not_called()
    handler.execute.assert_awaited_once()
    assert run["node_states"]["n_comfy"]["error"] == "vLLM service unavailable"


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("outcome", ["none", "empty", "wrong_family", "text_stub"])
async def test_handler_invalid_media_fails_without_publishing_or_retrying(monkeypatch, tmp_path, kind, outcome):
    node = {"id": "n_comfy", "type": kind, "config": {"delegate": "handler", "is_comfyui": True, "role": kind}}
    graph = {"graph_id": "g_handler", "nodes": [node], "edges": []}
    run = {"run_id": "run_handler", "node_states": {}}
    bridge = DesignEventBridge(run, None, graph=graph)
    publish_result = Mock(wraps=bridge.on_node_result)
    monkeypatch.setattr(bridge, "on_node_result", publish_result)
    inner = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("agent must not run")))
    output = None
    if outcome == "empty":
        output = NodeResult(message="generation returned no media")
    elif outcome in {"wrong_family", "text_stub"}:
        output_kind = ("video" if kind == "image" else "image") if outcome == "wrong_family" else kind
        suffix = ("mp4" if output_kind == "video" else "png") if outcome == "wrong_family" else "md"
        path = tmp_path / f"output.{suffix}"
        path.write_bytes(b"media" * 256)
        output = NodeResult(output_ref={"kind": output_kind, "uri": path.as_uri()})
    handler = SimpleNamespace(execute=AsyncMock(return_value=output))
    monkeypatch.setattr("jiuwenswarm.server.runtime.designer.handlers.get_node_handler", lambda _: handler)
    backend = MediaGuaranteeBackend(inner, graph=graph, run=run, bridge=bridge, media_root=tmp_path)

    result = await backend.run("prompt", {"label": "n_comfy"}, {"type": "object"})

    assert result.skipped is True
    state = run["node_states"]["n_comfy"]
    assert state["status"] == "failed"
    assert "handler did not produce required media" in state["error"]
    assert bridge.produced_for("n_comfy") is None
    publish_result.assert_not_called()
    handler.execute.assert_awaited_once()
    inner.run.assert_not_called()


async def test_coordinator_executes_real_comfyui_handler_and_repeats_single_node(monkeypatch, tmp_path):
    from openjiuwen.agent_teams.workflow.engine.backends import MockBackend

    from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
    from jiuwenswarm.server.runtime.design import run_coordinator
    from jiuwenswarm.server.runtime.designer import graph_store
    from jiuwenswarm.server.runtime.designer.handlers import comfyui_nodes

    for name in ("graphs", "runs", "workspace", "workflows"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(graph_store, "_graphs_dir", lambda: tmp_path / "graphs")
    monkeypatch.setattr(graph_store, "_runs_dir", lambda: tmp_path / "runs")
    # Native swarmflow layout: journals / WAL / compiled scripts stay in the
    # test tree instead of the owning team's session directory.
    monkeypatch.setattr(run_coordinator, "workflow_run_dir", lambda *args: tmp_path / "workflows")
    monkeypatch.setattr(
        run_coordinator, "workflow_run_journal_path", lambda *args, **kwargs: tmp_path / "journal.jsonl"
    )
    monkeypatch.setattr(
        run_coordinator, "workflow_run_wal_path", lambda *args, **kwargs: tmp_path / "wal.log"
    )
    monkeypatch.setattr(comfyui_nodes, "graph_workspace_dir", lambda _: tmp_path / "workspace")
    for name in ("IMAGE_GEN_API_KEY", "IMAGE_GEN_API_BASE", "IMAGE_GEN_MODEL_NAME", "IMAGE_GEN_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)
    # The legacy executor is gone: the coordinator only runs with a bound
    # design context (the strict post-refactor contract). The registry name
    # is imported into run_coordinator's own namespace, so patch there.
    bound = SimpleNamespace(
        worker_base_spec=object(),
        worker_build_context=None,
        expert_specs={},
        team_name="ut_team",
    )
    monkeypatch.setattr(
        run_coordinator, "get_design_context_registry",
        lambda: SimpleNamespace(for_graph=lambda _: bound),
    )
    worker = MockBackend()
    monkeypatch.setattr(worker, "run", AsyncMock(side_effect=AssertionError("agent must not run")))
    build_worker = Mock(return_value=worker)
    monkeypatch.setattr(run_coordinator, "_build_team_worker", build_worker)
    calls = []

    def generate(prompt, **kwargs):
        calls.append((prompt, kwargs))
        path = tmp_path / f"generated-{len(calls)}.png"
        path.write_bytes(b"media" * 256)
        return {"image_path": str(path)}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.vllm_omni_gen.invoke_vllm_omni_image_generation_sync",
        generate,
    )
    graph = normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1", "graph_id": "g_comfy_engine",
        "project_id": "p_comfy_engine", "title": "ComfyUI handler",
        "metadata": {"session_id": "ut_session"},
        "nodes": [{"id": "n_comfy", "type": "image", "config": {
            "is_comfyui": True, "role": "image", "delegate": "handler",
            "generate": {"prompt": "a red boat"},
            "comfyui": {"class_type": "VLLMOmniGenerateImage", "fields": {
                "url": "http://omni.test/v1", "model": "qwen-image", "width": 512, "height": 512,
            }},
        }}], "edges": [],
    })
    store = graph_store.DesignerGraphStore()
    store.save_graph(graph)
    coordinator = run_coordinator.RunCoordinator(store, graph["graph_id"], media_root=tmp_path)
    first = await coordinator.start(graph)
    assert first["status"] == "completed", first
    assert first["node_states"]["n_comfy"]["output_ref"]["uri"].endswith("generated-1.png")
    second = await coordinator.start(graph, node_ids=["n_comfy"])
    assert second["status"] == "completed", second
    assert second["run_id"] == first["run_id"]
    state = second["node_states"]["n_comfy"]
    assert state["version"] == 2
    # Single-node rerun: the fresh product replaces the current slots and the
    # previous version is archived into history.
    assert state["output_ref"]["uri"].endswith("generated-2.png")
    assert state["output_refs"][0]["uri"].endswith("generated-2.png")
    assert state["history"][-1]["output_ref"]["uri"].endswith("generated-1.png")
    assert len(calls) == 2
    assert all(call[1]["api_base"] == "http://omni.test/v1" for call in calls)
    assert build_worker.call_count == 2
    worker.run.assert_not_called()
