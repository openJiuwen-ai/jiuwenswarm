# -*- coding: utf-8 -*-
"""产物 Provider mock 与 AgentServer 服务域联调回归。"""

import asyncio
import contextlib
import json
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.mock_artifact_provider import MockArtifactProvider
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers


class FakeRequest:
    def __init__(self, method, params=None, session_id=None):
        self.req_method = method
        self.params = params or {}
        self.session_id = session_id


@pytest.fixture
def artifact_context(tmp_path: Path):
    context = build_rsi_service_context(tmp_path / "tasks")
    context.install_mock_artifact_adapters()
    pushes: list[dict] = []
    handlers = RsiAgentServerHandlers(
        context,
        send_push=lambda message: (pushes.append(message), True)[1],
        harness_refs_provider=lambda: None,
    )
    return context, handlers, pushes


async def _wait_for_status(context, task_id: str, expected: str) -> None:
    for _ in range(100):
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


@pytest.mark.asyncio
async def test_program_provider_closes_service_loop(artifact_context, tmp_path: Path):
    context, handlers, pushes = artifact_context
    program = tmp_path / "seed_program"
    program.mkdir()
    (program / "main.py").write_text("print('seed')\n", encoding="utf-8")

    session_id = "sess-program-e2e"
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
        "scenario": "ARTIFACT",
        "artifact_type": "PROGRAM",
        "name": "program-service-e2e",
        "artifact_path": str(program),
        "model_refs": {"optimizer": "mock-optimizer"},
        "max_iterations": 2,
    }, session_id=session_id))
    assert created["ok"] is True
    task_id = created["payload"]["task_id"]

    validation = handlers.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
        "scenario": "ARTIFACT",
        "artifact_type": "PROGRAM",
        "input_file": str(program),
    }))
    assert validation["ok"] is True
    assert validation["payload"]["valid"] is True

    started = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    assert started["ok"] is True
    await _wait_for_status(context, task_id, "COMPLETED")

    detail = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))
    assert detail["ok"] is True
    assert detail["payload"]["progress"]["iteration"] == 2
    assert detail["payload"]["best_artifact"]["artifact_id"].endswith(":artifact:2")

    report = handlers.handle(FakeRequest(ReqMethod.RSI_REPORT_GET, {"task_id": task_id}))
    assert report["ok"] is True
    assert report["payload"]["status"] == "COMPLETED"

    tree = handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))
    assert tree["ok"] is True
    assert len(tree["payload"]["nodes"]) == 7
    assert tree["payload"]["nodes"][1]["type"] == "ADOPTED"
    assert tree["payload"]["nodes"][1]["changes"][0]["element"] == "PROGRAM"
    iteration_nodes = [node for node in tree["payload"]["nodes"] if node["iteration"] > 0]
    assert len({node["parent_id"] for node in iteration_nodes if node["iteration"] == 2}) == 3
    assert all(node["extra"]["content"]["kind"] == "mock_artifact_candidate" for node in iteration_nodes)
    assert all(node["snapshot_artifact_id"] for node in iteration_nodes)
    tree_pushes = [
        item for item in pushes
        if item["payload"]["event_type"] == "rsi.training.tree.delta"
    ]
    assert len(tree_pushes) == 6
    assert all(len(item["payload"]["nodes"]) == 1 for item in tree_pushes)

    usage = handlers.handle(FakeRequest(ReqMethod.RSI_USAGE_GET, {"task_id": task_id}))
    assert usage["ok"] is True
    assert len(usage["payload"]["per_iteration"]) == 2

    downloaded = handlers.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {"task_id": task_id}))
    assert downloaded["ok"] is True
    assert downloaded["payload"]["kind"] == "artifact_package"
    assert downloaded["payload"]["is_best"] is True
    assert Path(downloaded["payload"]["path"]).is_file()
    assert Path(downloaded["payload"]["path"]).stat().st_size < 100_000
    with zipfile.ZipFile(downloaded["payload"]["path"]) as archive:
        assert "mock-optimization.json" in archive.namelist()
        manifest = json.loads(archive.read("mock-optimization.json"))
    assert manifest["source"]["read"] is False

    event_types = [item["payload"]["event_type"] for item in pushes]
    assert "rsi.training.progress" in event_types
    assert "rsi.training.tree.delta" in event_types
    assert event_types.count("rsi.training.status.changed") >= 3
    progress_push = next(
        item for item in pushes if item["payload"]["event_type"] == "rsi.training.progress"
    )
    assert progress_push["payload"]["progress"]["iteration"] == 1
    assert progress_push["payload"]["usage"]["call_count"] == 1
    terminal_status = [
        item for item in pushes
        if item["payload"]["event_type"] == "rsi.training.status.changed"
        and item["payload"]["status"] == "COMPLETED"
    ]
    assert terminal_status
    assert all(item.get("session_id") == session_id for item in pushes)
    await _stop_worker(context)


@pytest.mark.asyncio
async def test_paper_instruction_only_and_control_boundary(artifact_context):
    context, handlers, _ = artifact_context
    context.worker._ensure_runner = lambda: None  # noqa: SLF001 - keep task queued
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
        "scenario": "ARTIFACT",
        "artifact_type": "PAPER",
        "name": "paper-instruction-only",
        "optimization_instruction": "improve the abstract",
        "model_refs": {"optimizer": "mock-optimizer"},
    }))
    assert created["ok"] is True
    task_id = created["payload"]["task_id"]

    started = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    assert started["payload"]["status"] == "QUEUED"
    paused = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_PAUSE, {"task_id": task_id}))
    assert paused["ok"] is True
    assert paused["payload"]["status"] == "PAUSED"
    assert context.store.get(task_id).status == "PAUSED"

    terminated = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_TERMINATE, {"task_id": task_id}))
    assert terminated["ok"] is True
    await _wait_for_status(context, task_id, "TERMINATED")


@pytest.mark.asyncio
async def test_paper_provider_closes_instruction_only_service_loop(artifact_context):
    context, handlers, _ = artifact_context
    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
        "scenario": "artifact",
        "artifact_type": "paper",
        "name": "paper-service-e2e",
        "optimization_instruction": "improve the abstract",
        "model_refs": {"optimizer": "mock-optimizer"},
        "max_iterations": 1,
    }))
    assert created["ok"] is True
    task_id = created["payload"]["task_id"]

    started = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    assert started["ok"] is True
    await _wait_for_status(context, task_id, "COMPLETED")

    detail = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))
    assert detail["ok"] is True
    assert detail["payload"]["config"]["optimization_instruction"] == "improve the abstract"
    assert detail["payload"]["best_artifact"]["artifact_id"].endswith(":artifact:1")

    report = handlers.handle(FakeRequest(ReqMethod.RSI_REPORT_GET, {"task_id": task_id}))
    assert report["ok"] is True
    assert report["payload"]["status"] == "COMPLETED"

    downloaded = handlers.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {"task_id": task_id}))
    assert downloaded["ok"] is True
    assert downloaded["payload"]["kind"] == "artifact_package"
    assert downloaded["payload"]["is_directory"] is True
    assert Path(downloaded["payload"]["path"]).is_dir()
    assert "download_url" not in downloaded["payload"]
    await _stop_worker(context)


@pytest.mark.asyncio
async def test_provider_snapshots_survive_service_context_restart(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    context = build_rsi_service_context(tasks_root)
    context.install_mock_artifact_adapters()
    handlers = RsiAgentServerHandlers(context, harness_refs_provider=lambda: None)
    program = tmp_path / "restart_program"
    program.mkdir()
    (program / "main.py").write_text("print('seed')\n", encoding="utf-8")

    created = handlers.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
        "scenario": "ARTIFACT",
        "artifact_type": "PROGRAM",
        "name": "provider-restart-e2e",
        "artifact_path": str(program),
        "model_refs": {"optimizer": "mock-optimizer"},
        "max_iterations": 2,
    }))
    task_id = created["payload"]["task_id"]
    handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
    await _wait_for_status(context, task_id, "COMPLETED")
    await _stop_worker(context)

    restarted = build_rsi_service_context(tasks_root)
    restarted.install_mock_artifact_adapters()
    restarted_handlers = RsiAgentServerHandlers(restarted, harness_refs_provider=lambda: None)

    detail = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_TASK_GET, {"task_id": task_id}))
    assert detail["ok"] is True
    assert detail["payload"]["status"] == "COMPLETED"
    assert detail["payload"]["progress"]["iteration"] == 2
    assert detail["payload"]["best_artifact"]["artifact_id"].endswith(":artifact:2")

    tree = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))
    assert tree["ok"] is True
    assert len(tree["payload"]["nodes"]) == 7

    usage = restarted_handlers.handle(FakeRequest(ReqMethod.RSI_USAGE_GET, {"task_id": task_id}))
    assert usage["ok"] is True
    assert usage["payload"]["usage"]["call_count"] == 2
    assert usage["payload"]["per_iteration"] == []

    downloaded = restarted_handlers.handle(
        FakeRequest(ReqMethod.RSI_ARTIFACT_DOWNLOAD, {"task_id": task_id})
    )
    assert downloaded["ok"] is True
    assert Path(downloaded["payload"]["path"]).is_file()


@pytest.mark.asyncio
async def test_mock_provider_does_not_rewind_terminal_state(tmp_path: Path):
    provider = MockArtifactProvider(tmp_path / "tasks", "program")
    task_id = "rsi-terminal"
    state = provider._new_state(task_id, 2)  # noqa: SLF001 - seed durable provider state
    state.update({"status": "completed", "best_node_id": "node-2"})
    provider._save_state(task_id, state)  # noqa: SLF001 - seed durable provider state

    paused = await provider.pause(task_id)
    terminated = await provider.terminate(task_id)

    assert paused.status == "completed"
    assert terminated.status == "completed"
    snapshot = provider.read_state(task_id)
    assert snapshot.status == "completed"
    assert snapshot.best_node_id == "node-2"
