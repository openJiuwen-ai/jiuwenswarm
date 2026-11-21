"""覆盖三类 Provider 共存、模型接线和产物任务执行的恢复行为。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.model_resolver import RsiModelConfigResolver
from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters




def test_artifact_model_builder_uses_the_exact_configured_model():
    model = object()
    built = []
    resolver = RsiModelConfigResolver(
        config_loader=lambda: {}, zen_loader=lambda: [],
        defaults_loader=lambda _: [
            {"model_client_config": {"model_name": "first"}, "model_config_obj": {}},
            {"model_client_config": {"model_name": "selected"}, "model_config_obj": {"temperature": 0.2}},
        ],
        model_builder=lambda client, request: built.append((client, request)) or model,
    )
    assert resolver.build_model("selected") is model
    assert built == [({"model_name": "selected"}, {"temperature": 0.2})]


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_type", ["PAPER", "PROGRAM"])
async def test_artifact_task_runs_through_worker_and_projects_results(tmp_path, artifact_type):
    ctx = build_rsi_service_context(tmp_path / "tasks")
    ctx.register_adapters(build_rsi_adapters(ctx.tasks_root, mode="mock"))
    for kind in ("PAPER", "PROGRAM"):
        provider = ctx.adapters[f"ARTIFACT:{kind}"].provider
        provider.node_delay = 0
        provider.iteration_delay = 0
    source = tmp_path / "source"
    source.mkdir()
    created = ctx.task_service.create({
        "scenario": "ARTIFACT", "artifact_type": artifact_type, "name": "restored artifact",
        "artifact_path": str(source), "model_refs": {"optimizer": "selected"}, "max_iterations": 1,
    })
    task_id = created["task_id"]
    try:
        ctx.worker.enqueue(task_id)
        async with asyncio.timeout(5):
            while ctx.store.get(task_id).status not in {"COMPLETED", "FAILED"}:
                await asyncio.sleep(0.01)
        task = ctx.store.get(task_id)
        assert task.status == "COMPLETED", task.config
        tree = ctx.tree_service.get({"task_id": task_id}, adapter=ctx.adapter_for_task(task_id))
        assert tree["nodes"]
        assert any(node.get("snapshot_artifact_id") for node in tree["nodes"])
        assert any(
            Path(node["extra"]["artifact_path"]).exists()
            for node in tree["nodes"] if (node.get("extra") or {}).get("artifact_path")
        )
        if artifact_type == "PROGRAM":
            assert any(node.get("score") is not None for node in tree["nodes"])
    finally:
        await ctx.worker.aclose()
