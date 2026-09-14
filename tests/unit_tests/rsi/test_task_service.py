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

    def test_paper_file_or_directory_is_accepted_without_extension_check(self, ctx, tmp_path: Path):
        ctx.install_mock_artifact_adapters()
        paper = tmp_path / "paper"
        paper.mkdir()
        (paper / "main.tex").write_text("\\section{Paper}\n", encoding="utf-8")
        params = {
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": "paper-task",
            "model_refs": {"optimizer": "opt"},
            "artifact_path": str(paper),
        }
        # The service only delegates path semantics. The selected Provider
        # owns artifact-specific validation and accepts a source directory.
        result = ctx.task_service.create(params)
        assert result["status"] == TaskStatus.CREATED.value

    def test_paper_instruction_is_preserved_without_truncation(self, ctx, tmp_path: Path):
        ctx.install_mock_artifact_adapters()
        paper = tmp_path / "paper"
        paper.mkdir()
        instruction = "联网检索并保留完整实验约束。" * 800
        task_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": "paper-long-instruction",
            "model_refs": {"optimizer": "opt"},
            "artifact_path": str(paper),
            "optimization_instruction": instruction,
        })["task_id"]

        task = ctx.store.get(task_id)
        assert task.optimization_instruction == instruction
        assert task.config["optimization_instruction"] == instruction

    def test_paper_artifact_type_required(self, ctx):
        params = {
            "scenario": "ARTIFACT",
            "name": "paper-task",
            "model_refs": {"optimizer": "opt"},
            "artifact_path": "C:/missing/paper.zip",
        }
        with pytest.raises(RsiBadRequest):
            ctx.task_service.create(params)

    def test_paper_web_proxy_is_task_scoped_and_not_projected(self, ctx, tmp_path: Path):
        paper_path = tmp_path / "paper"
        paper_path.mkdir()
        task_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": "paper-proxy",
            "model_refs": {"optimizer": "opt-model"},
            "artifact_path": str(paper_path),
            "web_proxy": "http://proxy.example.test:7890/",
        })["task_id"]

        task = ctx.store.get(task_id)
        assert task.config["web_proxy"] == "http://proxy.example.test:7890"
        projection = ctx.task_service.get(
            {"task_id": task_id},
            projector=ctx.projector,
            usage_recorder=ctx.usage_recorder,
            artifact_service=ctx.artifact_service,
        )
        assert projection["config"]["web_proxy_configured"] is True
        assert "web_proxy" not in projection["config"]

    def test_web_proxy_only_allowed_for_paper(self, ctx):
        with pytest.raises(RsiBadRequest):
            ctx.task_service.create(_harness_create_params(web_proxy="http://proxy.test:7890"))

    def test_web_proxy_requires_http_url_and_port(self, ctx):
        params = {
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": "paper-proxy",
            "model_refs": {"optimizer": "opt-model"},
            "optimization_instruction": "improve",
            "web_proxy": "socks5://127.0.0.1:1080",
        }
        with pytest.raises(RsiBadRequest):
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

    def test_artifact_config_projection(self, ctx, tmp_path: Path):
        program_path = tmp_path / "program"
        program_path.mkdir()
        paper_path = tmp_path / "paper.zip"
        paper_path.write_bytes(b"paper")

        program_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PROGRAM",
            "name": "program-task",
            "model_refs": {"optimizer": "opt-model"},
            "artifact_path": str(program_path),
        })["task_id"]
        paper_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": "paper-task",
            "model_refs": {"optimizer": "opt-model"},
            "artifact_path": str(paper_path),
            "optimization_instruction": "improve abstract",
            "max_iterations": 2,
        })["task_id"]

        program = ctx.task_service.get(
            {"task_id": program_id},
            projector=ctx.projector,
            usage_recorder=ctx.usage_recorder,
            artifact_service=ctx.artifact_service,
        )
        paper = ctx.task_service.get(
            {"task_id": paper_id},
            projector=ctx.projector,
            usage_recorder=ctx.usage_recorder,
            artifact_service=ctx.artifact_service,
        )
        assert program["config"]["input_file"] is None
        assert program["config"]["artifact_path"] == str(program_path)
        assert program["config"]["max_iterations"] == 1
        assert paper["config"]["input_file"] is None
        assert paper["config"]["artifact_path"] == str(paper_path)
        assert paper["config"]["optimization_instruction"] == "improve abstract"
        assert paper["config"]["max_iterations"] == 2
        assert "search_width" not in paper["config"]

    def test_program_bundle_uses_manifest_max_iterations(self, ctx, tmp_path: Path):
        ctx.install_mock_artifact_adapters()
        program = tmp_path / "program-task"
        (program / "seed").mkdir(parents=True)
        (program / "seed" / "candidate.py").write_text("print('seed')\n", encoding="utf-8")
        (program / "run").mkdir()
        (program / "run" / "scorecard.json").write_text("{}", encoding="utf-8")
        (program / "task.json").write_text(
            json.dumps({
                "artifact_path": "seed",
                "run_dir": "run",
                "max_iterations": 6,
            }),
            encoding="utf-8",
        )

        task_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PROGRAM",
            "name": "program-bundle",
            "model_refs": {"optimizer": "mock-optimizer"},
            "artifact_path": str(program),
        })["task_id"]

        task = ctx.store.get(task_id)
        assert task.max_iterations == 6
        adapter = ctx.adapter_for("ARTIFACT", "PROGRAM")
        request = adapter.build_request(task.to_taskview())
        assert request.max_iterations == 6

    def test_program_bundle_explicit_max_iterations_overrides_manifest(
        self, ctx, tmp_path: Path
    ):
        ctx.install_mock_artifact_adapters()
        program = tmp_path / "program-task"
        program.mkdir()
        (program / "task.json").write_text(
            json.dumps({"max_iterations": 6}),
            encoding="utf-8",
        )

        task_id = ctx.task_service.create({
            "scenario": "ARTIFACT",
            "artifact_type": "PROGRAM",
            "name": "program-bundle-override",
            "model_refs": {"optimizer": "mock-optimizer"},
            "artifact_path": str(program),
            "max_iterations": 2,
        })["task_id"]

        assert ctx.store.get(task_id).max_iterations == 2


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
