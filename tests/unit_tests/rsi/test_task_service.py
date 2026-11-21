# -*- coding: utf-8 -*-
"""RSI 服务域单测：任务 CRUD + 状态机 + 场景校验（web 契约 v0.3 对齐）。"""
import json
import tempfile
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiBadRequest,
    RsiScenarioNotSupported,
    RsiUnsupportedParameter,
    RsiTaskNotFound,
    RsiTaskStateConflict,
)
from jiuwenswarm.agents.harness.common.rsi.models import TaskStatus


@pytest.fixture
def ctx():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_rsi_service_context(Path(tmp))
        ctx.bind_task_service(harness_refs_provider=lambda: "C:/fake/harness_config.yaml")
        yield ctx


def _harness_create_params(**overrides):
    params = {
        "scenario": "HARNESS",
        "name": "harness-task",
        "input_file": "C:/data/dataset.json",
        "model_refs": {"optimizer": "opt-model", "tester": "tst-model"},
        "max_iterations": 3,
        "search_width": 2,
    }
    params.update(overrides)
    return params


class TestTaskCreate:
    def test_harness_ok(self, ctx):
        result = ctx.task_service.create(_harness_create_params())
        assert result["status"] == TaskStatus.CREATED.value
        task_id = result["task_id"]
        task = ctx.store.get(task_id)
        assert task.task_id == task_id
        assert task.scenario == "HARNESS"
        assert task.config["harness_refs_path"] == "C:/fake/harness_config.yaml"
        assert Path(task.run_dir).parts[-2:] == (task_id, "run")

    def test_missing_model_refs(self, ctx):
        params = _harness_create_params(model_refs=None)
        with pytest.raises(RsiBadRequest):
            ctx.task_service.create(params)

    def test_harness_requires_tester(self, ctx):
        params = _harness_create_params(model_refs={"optimizer": "opt"})
        with pytest.raises(RsiBadRequest):
            ctx.task_service.create(params)

    def test_harness_rejects_artifact_path(self, ctx):
        params = _harness_create_params(artifact_path="C:/x.zip")
        with pytest.raises(RsiBadRequest):
            ctx.task_service.create(params)

    def test_harness_rejects_unsupported_execution_mode(self, ctx):
        params = _harness_create_params(execution_mode="e2b")
        with pytest.raises(RsiUnsupportedParameter):
            ctx.task_service.create(params)

    def test_invalid_scenario(self, ctx):
        params = _harness_create_params(scenario="UNKNOWN")
        with pytest.raises(RsiScenarioNotSupported):
            ctx.task_service.create(params)








class TestTaskList:
    def test_empty(self, ctx):
        assert ctx.task_service.list({}) == []

    def test_filter_by_scenario(self, ctx):
        ctx.task_service.create(_harness_create_params(name="h1"))
        result = ctx.task_service.list({"scenario": "HARNESS"})
        assert len(result) == 1
        projection = result[0]
        assert set(projection.keys()) == {
            "task_id", "name", "scenario", "artifact_type", "status", "created_at",
        }
        assert projection["status"] == "CREATED"

    def test_filter_mismatch(self, ctx):
        ctx.task_service.create(_harness_create_params(name="h1"))
        assert ctx.task_service.list({"scenario": "ARTIFACT"}) == []


class TestTaskGet:
    def test_not_found(self, ctx):
        with pytest.raises(RsiTaskNotFound):
            ctx.task_service.get({"task_id": "rsi-missing"}, projector=ctx.projector,
                                 usage_recorder=ctx.usage_recorder, artifact_service=ctx.artifact_service)

    def test_ok(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.projector.register_root(task_id)
        data = ctx.task_service.get({"task_id": task_id}, projector=ctx.projector,
                                    usage_recorder=ctx.usage_recorder, artifact_service=ctx.artifact_service)
        assert data["status"] == "CREATED"
        assert data["config"]["model"]["optimizer"] == "opt-model"
        assert data["config"]["model"]["tester"] == "tst-model"
        assert data["config"]["input_file"] == "C:/data/dataset.json"
        assert data["config"]["max_iterations"] == 3
        assert "search_width" not in data["config"]
        assert data["progress"]["iteration"] == 0

    def test_failed_projection_prefers_detailed_result_over_generic_history(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())[
            "task_id"
        ]
        ctx.store.update_status(task_id, ["CREATED"], "QUEUED", cause="start")
        ctx.store.update_status(task_id, ["QUEUED"], "RUNNING", cause="start")
        ctx.store.update_status(task_id, ["RUNNING"], "FAILED", cause="provider.failed")
        ctx.store.merge_results(
            task_id,
            {"error_code": "ENGINE_FAILED", "error_message": "评测脚本缺少入口函数"},
        )

        data = ctx.task_service.get(
            {"task_id": task_id},
            projector=ctx.projector,
            usage_recorder=ctx.usage_recorder,
            artifact_service=ctx.artifact_service,
        )

        assert data["failure_reason"] == "评测脚本缺少入口函数"





class TestTaskDelete:
    def test_delete_created_without_active_refs(self, ctx):
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        result = ctx.task_service.delete({"task_id": task_id})
        assert result == {"ok": True}

    def test_delete_blocked_by_active_refs(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        with pytest.raises(RsiTaskStateConflict):
            ctx.task_service.delete({"task_id": task_id})
        ctx.store.mark_active_ref_released(task_id)
        assert ctx.task_service.delete({"task_id": task_id}) == {"ok": True}

    def test_delete_queued_updates_state_before_removing_task(self, ctx):
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.store.update_status(task_id, [TaskStatus.CREATED.value], TaskStatus.QUEUED.value, cause="start")
        status_changes: list[tuple[str, str, str]] = []
        ctx.store.set_status_changed_callback(lambda *change: status_changes.append(change))

        assert ctx.task_service.delete({"task_id": task_id}) == {"ok": True}

        assert status_changes == [(task_id, TaskStatus.QUEUED.value, TaskStatus.TERMINATED.value)]
        with pytest.raises(RsiTaskNotFound):
            ctx.store.get(task_id)

    def test_delete_paused_updates_state_before_removing_task(self, ctx):
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.store.update_status(task_id, [TaskStatus.CREATED.value], TaskStatus.QUEUED.value, cause="start")
        ctx.store.update_status(task_id, [TaskStatus.QUEUED.value], TaskStatus.PAUSED.value, cause="pause")
        status_changes: list[tuple[str, str, str]] = []
        ctx.store.set_status_changed_callback(lambda *change: status_changes.append(change))

        assert ctx.task_service.delete({"task_id": task_id}, worker=ctx.worker) == {"ok": True}

        assert status_changes == [(task_id, TaskStatus.PAUSED.value, TaskStatus.TERMINATED.value)]
        with pytest.raises(RsiTaskNotFound):
            ctx.store.get(task_id)

    def test_delete_queued_with_worker_removes_queue_entry(self, ctx):
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.worker.enqueue(task_id)
        assert ctx.store.get(task_id).status == TaskStatus.QUEUED.value

        assert ctx.task_service.delete({"task_id": task_id}, worker=ctx.worker) == {"ok": True}

        assert ctx.worker._queue.empty()  # noqa: SLF001 - verify queued deletion cleanup
        with pytest.raises(RsiTaskNotFound):
            ctx.store.get(task_id)

    def test_store_delete_allows_queued_but_not_running_or_paused(self, ctx):
        ctx.bind_task_service(harness_refs_provider=lambda: None)
        queued_id = ctx.task_service.create(_harness_create_params(name="queued"))["task_id"]
        ctx.store.update_status(
            queued_id,
            [TaskStatus.CREATED.value],
            TaskStatus.QUEUED.value,
            cause="start",
        )
        ctx.store.delete(queued_id)
        with pytest.raises(RsiTaskNotFound):
            ctx.store.get(queued_id)

        running_id = ctx.task_service.create(_harness_create_params(name="running"))["task_id"]
        ctx.store.update_status(running_id, [TaskStatus.CREATED.value], TaskStatus.QUEUED.value, cause="start")
        ctx.store.update_status(running_id, [TaskStatus.QUEUED.value], TaskStatus.RUNNING.value, cause="start")
        with pytest.raises(RsiTaskStateConflict):
            ctx.store.delete(running_id)

        paused_id = ctx.task_service.create(_harness_create_params(name="paused"))["task_id"]
        ctx.store.update_status(paused_id, [TaskStatus.CREATED.value], TaskStatus.QUEUED.value, cause="start")
        ctx.store.update_status(paused_id, [TaskStatus.QUEUED.value], TaskStatus.PAUSED.value, cause="pause")
        with pytest.raises(RsiTaskStateConflict):
            ctx.store.delete(paused_id)

    def test_delete_missing(self, ctx):
        with pytest.raises(RsiTaskNotFound):
            ctx.task_service.delete({"task_id": "rsi-ghost"})


class TestStatusMachine:
    def test_happy_path(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.store.mark_active_ref_released(task_id)
        final = ctx.store.update_status(task_id, ["CREATED"], "QUEUED", cause="start")
        assert final.status == "QUEUED"
        final = ctx.store.update_status(task_id, ["QUEUED"], "RUNNING", cause="start")
        assert final.status == "RUNNING"
        final = ctx.store.update_status(task_id, ["RUNNING"], "COMPLETED", cause="done")
        assert final.status == "COMPLETED"

    def test_illegal_transition(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        with pytest.raises(RsiTaskStateConflict):
            ctx.store.update_status(task_id, ["CREATED"], "RUNNING", cause="bad")

    def test_terminal_no_transition(self, ctx):
        task_id = ctx.task_service.create(_harness_create_params())["task_id"]
        ctx.store.update_status(task_id, ["CREATED"], "TERMINATED", cause="cancel")
        with pytest.raises(RsiTaskStateConflict):
            ctx.store.update_status(task_id, ["TERMINATED"], "QUEUED", cause="bad")
