# -*- coding: utf-8 -*-
"""AgentServer restart recovery from the durable RSI workspace."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, TaskStatus, utcnow_iso
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers


class FakeRequest:
    def __init__(self, method: Any, params: dict[str, Any] | None = None) -> None:
        self.req_method = method
        self.params = params or {}
        self.session_id = None


class SnapshotAdapter:
    """Local snapshot adapter used at the existing Provider seam."""

    supports_resume = True

    def __init__(self, *, status: str = "running", tree: dict[str, Any] | None = None) -> None:
        self.status = status
        self.tree = tree

    def read_state(self, task_id: str) -> Any:
        return SimpleNamespace(task_id=task_id, status=self.status)

    def get_tree(self, task_id: str) -> dict[str, Any] | None:
        del task_id
        return self.tree


def _create_task(tasks_root: Path, status: TaskStatus) -> tuple[Any, str]:
    context = build_rsi_service_context(tasks_root)
    task_id = "rsi-recovery"
    context.store.create(
        RsiTask(
            task_id=task_id,
            name="restart recovery",
            scenario="HARNESS",
            status=TaskStatus.CREATED.value,
            created_at=utcnow_iso(),
            input_file=str(tasks_root / "dataset.json"),
            model_refs={"optimizer": "mock", "tester": "mock"},
            run_dir=str(tasks_root / task_id / "run"),
        )
    )
    if status != TaskStatus.CREATED:
        context.store.update_status(
            task_id,
            [TaskStatus.CREATED.value],
            TaskStatus.QUEUED.value,
            cause="test setup",
        )
    if status == TaskStatus.RUNNING:
        context.store.update_status(
            task_id,
            [TaskStatus.QUEUED.value],
            TaskStatus.RUNNING.value,
            cause="test setup",
        )
    elif status == TaskStatus.PAUSED:
        context.store.update_status(
            task_id,
            [TaskStatus.QUEUED.value],
            TaskStatus.PAUSED.value,
            cause="test setup",
        )
    return context, task_id


def _persist_rich_tree(context: Any, task_id: str) -> None:
    context.projector.register_root(task_id, baseline=0.5)
    context.projector.on_provider_node(
        task_id,
        {
            "node_id": "C1",
            "iteration": 1,
            "parent_id": "ROOT",
            "type": "ADOPTED",
            "adopted": True,
            "score": 0.9,
            "summary": "workspace 中的阶段描述",
            "snapshot_artifact_id": "snapshot-C1",
            "changes": [
                {
                    "group": "PROMPT",
                    "operation": "UPDATE",
                    "target": "system_prompt",
                    "summary": "保留服务侧变更说明",
                }
            ],
            "extra": {"source": "event"},
        },
    )
    context.projector.on_progress_metric(
        task_id,
        {"iteration": 1, "total_iterations": 3, "score": 0.9, "baseline": 0.5},
    )


def test_progress_metric_is_saved_in_tree_snapshot(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    context, task_id = _create_task(tasks_root, TaskStatus.PAUSED)

    _persist_rich_tree(context, task_id)

    snapshot = json.loads((tasks_root / task_id / "tree.json").read_text(encoding="utf-8"))
    assert snapshot["metric"]["iteration"] == 1
    assert snapshot["metric"]["total_iterations"] == 3


def test_tree_get_restores_workspace_snapshot_without_provider(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    original, task_id = _create_task(tasks_root, TaskStatus.PAUSED)
    _persist_rich_tree(original, task_id)

    restarted = build_rsi_service_context(tasks_root)
    handlers = RsiAgentServerHandlers(restarted)

    response = handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))

    assert response["ok"] is True
    assert response["payload"]["iteration"] == 1
    nodes = {node["node_id"]: node for node in response["payload"]["nodes"]}
    assert set(nodes) == {"ROOT", "C1"}
    assert nodes["C1"]["description"] == "workspace 中的阶段描述"
    assert nodes["C1"]["snapshot_artifact_id"] == "snapshot-C1"


def test_tree_get_merges_provider_snapshot_without_erasing_workspace_details(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    original, task_id = _create_task(tasks_root, TaskStatus.PAUSED)
    _persist_rich_tree(original, task_id)
    adapter = SnapshotAdapter(
        tree={
            "nodes": [
                {
                    "node_id": "C1",
                    "iteration": 1,
                    "parent_id": None,
                    "type": "CANDIDATE",
                    "adopted": True,
                    "score": 0.9,
                    "summary": "candidate_passed_batch_gate",
                    "reason": "candidate_passed_batch_gate",
                    "snapshot_artifact_id": None,
                    "changes": [],
                    "extra": {"source": "provider"},
                },
                {
                    "node_id": "C2",
                    "iteration": 2,
                    "parent_id": "C1",
                    "type": "REJECTED",
                    "adopted": False,
                    "score": 0.4,
                    "summary": "Provider 补回的新节点",
                    "reason": "score_regressed",
                    "changes": [],
                },
            ],
            "depth": 2,
            "iteration": 2,
        }
    )
    restarted = build_rsi_service_context(tasks_root, adapters={"HARNESS": adapter})
    handlers = RsiAgentServerHandlers(restarted)

    response = handlers.handle(FakeRequest(ReqMethod.RSI_TREE_GET, {"task_id": task_id}))

    assert response["ok"] is True
    assert response["payload"]["iteration"] == 2
    nodes = {node["node_id"]: node for node in response["payload"]["nodes"]}
    assert set(nodes) == {"ROOT", "C1", "C2"}
    assert nodes["C1"]["parent_id"] == "ROOT"
    assert nodes["C1"]["description"] == "workspace 中的阶段描述"
    assert nodes["C1"]["snapshot_artifact_id"] == "snapshot-C1"
    assert nodes["C1"]["changes"][0]["target"] == "system_prompt"
    assert nodes["C1"]["failure_reason"] is None
    assert nodes["C1"]["extra"] == {"source": "provider"}
    assert nodes["C2"]["parent_id"] == "C1"


def test_running_task_becomes_paused_and_can_be_enqueued_for_resume(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    original, task_id = _create_task(tasks_root, TaskStatus.RUNNING)
    _persist_rich_tree(original, task_id)
    restarted = build_rsi_service_context(
        tasks_root,
        adapters={"HARNESS": SnapshotAdapter(status="running")},
    )

    handlers = RsiAgentServerHandlers(restarted)

    recovered = restarted.store.get(task_id)
    assert recovered.status == TaskStatus.PAUSED.value
    assert recovered.status_history[-1]["cause"] == "agentserver_restart.execution_detached"

    restarted.worker._ensure_runner = lambda: None  # noqa: SLF001 - assert queueing, not engine resume
    response = handlers.handle(FakeRequest(ReqMethod.RSI_TRAINING_RESUME, {"task_id": task_id}))
    assert response["ok"] is True
    assert response["payload"]["status"] == TaskStatus.QUEUED.value
    assert restarted.worker._queue.qsize() == 1  # noqa: SLF001 - durable recovery must not auto-enqueue


def test_running_task_uses_provider_terminal_status_during_recovery(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _, task_id = _create_task(tasks_root, TaskStatus.RUNNING)
    restarted = build_rsi_service_context(
        tasks_root,
        adapters={"HARNESS": SnapshotAdapter(status="completed")},
    )

    RsiAgentServerHandlers(restarted)

    recovered = restarted.store.get(task_id)
    assert recovered.status == TaskStatus.COMPLETED.value
    assert recovered.status_history[-1]["cause"] == "provider_snapshot.completed"
    assert restarted.worker._queue.qsize() == 0  # noqa: SLF001 - recovery never starts engine work


def test_queued_task_becomes_paused_once_without_being_requeued(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _, task_id = _create_task(tasks_root, TaskStatus.QUEUED)
    restarted = build_rsi_service_context(tasks_root)

    RsiAgentServerHandlers(restarted)
    history_after_first_recovery = list(restarted.store.get(task_id).status_history)
    restarted.recover_workspace()

    recovered = restarted.store.get(task_id)
    assert recovered.status == TaskStatus.PAUSED.value
    assert recovered.status_history == history_after_first_recovery
    assert recovered.status_history[-1]["cause"] == "agentserver_restart.queue_lost"
    assert restarted.worker._queue.qsize() == 0  # noqa: SLF001 - explicit resume is required
