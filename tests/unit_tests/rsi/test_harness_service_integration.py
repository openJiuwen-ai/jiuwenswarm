# -*- coding: utf-8 -*-
"""Harness Provider mock and AgentServer service-loop integration tests."""

import asyncio
import contextlib
import json
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import MockHarnessProvider
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers


class FakeRequest:
    def __init__(self, method, params=None, session_id=None):
        self.req_method = method
        self.params = params or {}
        self.session_id = session_id


async def _wait_for_status(context, task_id: str, expected: str) -> None:
    for _ in range(200):
        if context.store.get(task_id).status == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {expected}")


async def _stop_worker(context) -> None:
    runner = context.worker._run_task  # noqa: SLF001 - test lifecycle cleanup
    if runner is not None and not runner.done():
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


@pytest.fixture
def harness_context(tmp_path: Path):
    context = build_rsi_service_context(tmp_path / "tasks")
    context.install_mock_rsi_adapters()
    pushes: list[dict] = []
    handlers = RsiAgentServerHandlers(
        context,
        send_push=lambda message: (pushes.append(message), True)[1],
        harness_refs_provider=lambda: None,
    )
    return context, handlers, pushes


def _create_params(dataset: Path, **overrides):
    params = {
        "scenario": "HARNESS",
        "name": "harness-service-e2e",
        "input_file": str(dataset),
        "model_refs": {"optimizer": "mock-optimizer", "tester": "mock-tester"},
        "max_iterations": 2,
        "search_width": 2,
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_mock_harness_provider_closes_service_loop(harness_context, tmp_path: Path):
    context, handlers, pushes = harness_context
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"cases": [{"case_id": "a"}, {"case_id": "b"}]}), encoding="utf-8")

    session_id = "sess-harness-e2e"
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, _create_params(dataset), session_id=session_id))
    assert created["ok"] is True
    task_id = created["payload"]["task_id"]

    validation = handlers.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
        "scenario": "HARNESS",
        "input_file": str(dataset),
    }))
    assert validation == {
        "ok": True,
        "payload": {"valid": True, "sample_count": 2, "errors": []},
    }

    started = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    assert started["ok"] is True
    await _wait_for_status(context, task_id, "COMPLETED")

    detail = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))
    assert detail["payload"]["progress"]["iteration"] == 2
    assert detail["payload"]["best_artifact"]["artifact_id"].endswith(":artifact:2:1")

    report = handlers.handle(FakeRequest(ReqMethod.RSI_REPORT_GET, {"task_id": task_id}))
    assert report["payload"]["status"] == "COMPLETED"
    tree = handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))
    # search_width is deprecated and ignored by the service; the mock provider
    # now sees the engine default (1), so 2 iterations produce root + 2 nodes.
    assert len(tree["payload"]["nodes"]) == 3
    assert tree["payload"]["nodes"][1]["type"] == "ADOPTED"
    iteration_nodes = [node for node in tree["payload"]["nodes"] if node["iteration"] > 0]
    assert all(node["extra"]["content"]["kind"] == "mock_harness_candidate" for node in iteration_nodes)
    assert all(node["snapshot_artifact_id"] for node in iteration_nodes)
    usage = handlers.handle(FakeRequest(ReqMethod.RSI_USAGE_GET, {"task_id": task_id}))
    assert usage["payload"]["usage"]["call_count"] == 2

    provider = context.adapters["HARNESS"].provider
    assert provider.read_state(task_id).best_node_id.endswith(":node:2:1")

    downloaded = handlers.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {"task_id": task_id}))
    assert downloaded["payload"]["kind"] == "harness_plugin"
    assert Path(downloaded["payload"]["path"]).is_file()
    assert downloaded["payload"]["download_url"].startswith("/file-api/download?token=")
    assert downloaded["payload"]["download_token"]
    assert Path(downloaded["payload"]["path"]).stat().st_size < 100_000
    with zipfile.ZipFile(downloaded["payload"]["path"]) as archive:
        assert {"README.md", "mock-optimization.json", "node.json"}.issubset(archive.namelist())
        manifest = json.loads(archive.read("mock-optimization.json"))
    assert manifest["mock"] is True
    assert manifest["harness_refs"]["read"] is False

    candidate_download = handlers.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {
        "task_id": task_id,
        "artifact_id": iteration_nodes[-1]["snapshot_artifact_id"],
    }))
    assert candidate_download["ok"] is True
    assert Path(candidate_download["payload"]["path"]).is_file()

    event_types = [item["payload"]["event_type"] for item in pushes]
    assert "rsi.training.progress" in event_types
    assert "rsi.training.tree.delta" in event_types
    assert event_types.count("rsi.training.status.changed") >= 3
    assert all(item.get("session_id") == session_id for item in pushes)
    await _stop_worker(context)


@pytest.mark.asyncio
async def test_mock_harness_snapshots_survive_context_restart(harness_context, tmp_path: Path):
    context, handlers, _ = harness_context
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"cases": [{"case_id": "a"}]}', encoding="utf-8")
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, _create_params(
        dataset,
        max_iterations=1,
        search_width=1,
    )))
    task_id = created["payload"]["task_id"]
    handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    await _wait_for_status(context, task_id, "COMPLETED")
    await _stop_worker(context)

    restarted = build_rsi_service_context(context.tasks_root)
    restarted.install_mock_rsi_adapters()
    restarted_handlers = RsiAgentServerHandlers(restarted)

    detail = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))
    assert detail["ok"] is True
    assert detail["payload"]["status"] == "COMPLETED"
    assert detail["payload"]["progress"]["iteration"] == 1
    tree = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))
    assert len(tree["payload"]["nodes"]) == 2
    usage = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_USAGE_GET, {"task_id": task_id}))
    assert usage["payload"]["usage"]["call_count"] == 1
    downloaded = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {"task_id": task_id}))
    assert Path(downloaded["payload"]["path"]).is_file()
    assert downloaded["payload"]["download_token"]


@pytest.mark.asyncio
async def test_mock_harness_pause_resume_uses_provider_control(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    context = build_rsi_service_context(tasks_root)
    context.install_mock_rsi_adapters()
    context.register_adapters({
        "HARNESS": HarnessEngineAdapter(MockHarnessProvider(tasks_root, iteration_delay=0.02)),
    })
    handlers = RsiAgentServerHandlers(context)
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"cases": [{"case_id": "a"}]}', encoding="utf-8")
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, _create_params(dataset, max_iterations=2)))
    task_id = created["payload"]["task_id"]

    handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    await _wait_for_status(context, task_id, "RUNNING")
    paused = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_PAUSE, {"task_id": task_id}))
    assert paused["ok"] is True
    await _wait_for_status(context, task_id, "PAUSED")

    resumed = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_RESUME, {"task_id": task_id}))
    assert resumed["ok"] is True
    await _wait_for_status(context, task_id, "COMPLETED")
    await _stop_worker(context)
