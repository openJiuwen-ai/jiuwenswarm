# -*- coding: utf-8 -*-
"""RSI AgentServer 分发层 + Gateway 注册单测（B0/B2 + P1 推送）。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.errors import RsiBadRequest, RsiPathInvalid
from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider
from jiuwenswarm.agents.harness.common.rsi.services import _ensure_provider_valid
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers

RSI_METHOD_NAMES = {
    "rsi.dataset.validate",
    "rsi.task.create",
    "rsi.task.list",
    "rsi.task.get",
    "rsi.task.delete",
    "rsi.training.start",
    "rsi.training.pause",
    "rsi.training.resume",
    "rsi.training.terminate",
    "rsi.report.get",
    "rsi.usage.get",
    "rsi.artifact.download",
    "rsi.artifact.files.list",
    "rsi.artifact.files.get",
    "rsi.tree.get",
    "rsi.harness.install",
    "rsi.harness.versions.list",
    "rsi.harness.rollback",
}


class TestReqMethod:
    def test_18_rsi_methods_registered(self):
        present = {m.value for m in ReqMethod if m.value.startswith("rsi.")}
        assert present == RSI_METHOD_NAMES


class FakeRequest:
    def __init__(self, method, params=None):
        self.req_method = method
        self.params = params or {}


@pytest.fixture
def handlers():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_rsi_service_context(Path(tmp))
        pushes = []
        h = RsiAgentServerHandlers(
            ctx,
            send_push=lambda msg: (pushes.append(msg), True)[1],
            harness_refs_provider=lambda: None,
        )
        yield h, ctx, pushes


class TestDispatch:
    def test_harness_install_dispatches_to_context_installer(self, handlers):
        h, ctx, _ = handlers
        calls = []

        class Installer:
            def install(self, task_id):
                calls.append(task_id)
                return {"task_id": task_id, "status": "ACTIVE"}

        ctx.harness_installer = Installer()
        result = h.handle(
            FakeRequest(ReqMethod.RSI_HARNESS_INSTALL, {"task_id": "rsi-ready"})
        )
        assert result == {
            "ok": True,
            "payload": {"task_id": "rsi-ready", "status": "ACTIVE"},
        }
        assert calls == ["rsi-ready"]

    def test_harness_versions_list_dispatches_to_context_installer(self, handlers):
        h, ctx, _ = handlers

        class Installer:
            def list_versions(self):
                return {"active_installation_id": "rsi-harness-a", "versions": []}

        ctx.harness_installer = Installer()
        result = h.handle(FakeRequest(ReqMethod.RSI_HARNESS_VERSIONS_LIST))

        assert result == {
            "ok": True,
            "payload": {"active_installation_id": "rsi-harness-a", "versions": []},
        }

    @pytest.mark.asyncio
    async def test_harness_rollback_dispatches_to_context_installer(self, handlers):
        h, ctx, _ = handlers

        class Installer:
            async def rollback(self, installation_id):
                assert installation_id == "rsi-harness-first"
                return {"installation_id": installation_id, "status": "ACTIVE"}

        ctx.harness_installer = Installer()
        result = await h.handle_async(
            FakeRequest(
                ReqMethod.RSI_HARNESS_ROLLBACK,
                {"installation_id": "rsi-harness-first"},
            )
        )

        assert result == {
            "ok": True,
            "payload": {"installation_id": "rsi-harness-first", "status": "ACTIVE"},
        }

    @pytest.mark.asyncio
    async def test_async_harness_install_awaits_and_maps_rsi_error(self, handlers):
        h, ctx, _ = handlers

        class Installer:
            async def install(self, task_id):
                assert task_id == "rsi-async"
                raise RsiBadRequest("install request is invalid")

        ctx.harness_installer = Installer()
        result = await h.handle_async(
            FakeRequest(ReqMethod.RSI_HARNESS_INSTALL, {"task_id": "rsi-async"})
        )
        assert result == {
            "ok": False,
            "error": "install request is invalid",
            "code": "BAD_REQUEST",
        }

    def test_unknown_method(self, handlers):
        h, _, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.MODELS_LIST))
        assert result["ok"] is False
        assert result["code"] == "BAD_REQUEST"

    def test_create_list_delete_roundtrip(self, handlers):
        h, ctx, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "t1", "input_file": "C:/d.json",
            "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        assert result["ok"]
        task_id = result["payload"]["task_id"]

        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_LIST, {}))
        assert result["ok"] and len(result["payload"]["tasks"]) == 1

        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_DELETE, {"task_id": task_id}))
        assert result["ok"]
        assert result["payload"] == {"ok": True}

    def test_dataset_validate_path_invalid(self, handlers):
        h, _, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
            "input_file": "C:/missing.json", "scenario": "HARNESS",
        }))
        assert result["ok"] is False
        assert result["code"] == "PATH_INVALID"

    def test_dataset_validate_ok(self, handlers, tmp_path: Path):
        h, _, _ = handlers
        dataset = tmp_path / "dataset.json"
        dataset.write_text('{"cases": [{"case_id": "a"}, {"case_id": "b"}]}', encoding="utf-8")
        result = h.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
            "input_file": str(dataset), "scenario": "HARNESS",
        }))
        assert result["ok"] is True
        assert result["payload"]["sample_count"] == 2
        assert result["payload"]["valid"] is True

    def test_dataset_validate_accepts_dataset_path_alias(self, handlers, tmp_path: Path):
        h, _, _ = handlers
        dataset = tmp_path / "dataset.json"
        dataset.write_text('{"cases": [{"case_id": "alias"}]}', encoding="utf-8")
        result = h.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
            "dataset_path": str(dataset), "scenario": "HARNESS",
        }))
        assert result["ok"] is True
        assert result["payload"] == {"valid": True, "sample_count": 1, "errors": []}

    def test_dataset_validate_uses_real_harness_provider_semantics(self, tmp_path: Path):
        tasks_root = tmp_path / "tasks"
        context = build_rsi_service_context(tasks_root)
        context.register_harness_provider(HarnessProvider(tasks_root))
        h = RsiAgentServerHandlers(context)
        dataset = tmp_path / "duplicate.json"
        dataset.write_text(
            '[{"case_id": "same"}, {"case_id": "same"}]',
            encoding="utf-8",
        )
        result = h.handle(FakeRequest(ReqMethod.RSI_DATASET_VALIDATE, {
            "input_file": str(dataset), "scenario": "HARNESS",
        }))
        # Validation failures are a successful method response with
        # ``payload.valid=false``; transport/protocol errors use ``ok=false``.
        assert result["ok"] is True
        assert result["payload"]["valid"] is False
        assert result["payload"]["errors"][0]["code"] == "DATASET_INVALID"

    def test_provider_path_error_uses_matching_error_reason(self):
        result = SimpleNamespace(
            valid=False,
            errors=[
                {"code": "ARTIFACT_PATH_REQUIRED", "message": "artifact path required"},
                {"code": "PATH_INVALID", "message": "path is not readable"},
            ],
        )

        with pytest.raises(RsiPathInvalid, match="path is not readable"):
            _ensure_provider_valid(result)

    def test_artifact_files_list_and_get(self, handlers):
        h, ctx, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "artifact-files",
            "input_file": "C:/d.json", "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        assert result["ok"]
        task_id = result["payload"]["task_id"]
        artifact_dir = Path(ctx.store.tasks_root) / task_id / "artifact"
        artifact_dir.mkdir(parents=True)
        artifact_file = artifact_dir / "main.tex"
        artifact_file.write_text("\\section{Title}\n", encoding="utf-8", newline="\n")

        result = h.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_FILES_LIST, {
            "task_id": task_id,
            "path": str(artifact_dir),
        }))
        assert result["ok"]
        assert any(item["name"] == "main.tex" for item in result["payload"]["files"])

        result = h.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_FILES_GET, {
            "task_id": task_id,
            "path": str(artifact_file),
        }))
        assert result["ok"]
        assert result["payload"]["encoding"] == "text"
        assert result["payload"]["content"] == "\\section{Title}\n"

        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "other-task",
            "input_file": "C:/d.json", "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        assert result["ok"]
        other_task_id = result["payload"]["task_id"]

        other_dir = Path(ctx.store.tasks_root) / other_task_id / "artifact"
        other_dir.mkdir(parents=True)
        other_file = other_dir / "secret.tex"
        other_file.write_text("secret", encoding="utf-8")
        result = h.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_FILES_LIST, {
            "task_id": task_id,
            "path": str(other_file),
        }))
        assert not result["ok"]
        assert result["code"] == "PATH_INVALID"

        result = h.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_FILES_GET, {
            "task_id": f"../{task_id}",
            "path": str(artifact_file),
        }))
        assert not result["ok"]
        assert result["code"] == "TASK_NOT_FOUND"

        private_dir = Path(ctx.store.tasks_root) / task_id / "models"
        private_dir.mkdir(parents=True)
        private_file = private_dir / "evaluation.yaml"
        private_file.write_text("api_key: should-not-leak\n", encoding="utf-8")
        result = h.handle(FakeRequest(ReqMethod.RSI_ARTIFACT_FILES_GET, {
            "task_id": task_id,
            "path": str(private_file),
        }))
        assert not result["ok"]
        assert result["code"] == "PATH_INVALID"


class TestP1Push:
    def test_status_changed_push(self, handlers):
        h, ctx, pushes = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "t", "input_file": "C:/d.json",
            "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        task_id = result["payload"]["task_id"]
        pushes.clear()
        ctx.store.update_status(task_id, ["CREATED"], "QUEUED", cause="start")
        ctx.store.update_status(task_id, ["QUEUED"], "RUNNING", cause="start")
        assert len(pushes) == 2
        first = pushes[0]
        assert first["payload"]["event_type"] == "rsi.training.status.changed"
        assert first["payload"]["task_id"] == task_id
        assert first["payload"]["old_status"] == "CREATED"
        assert first["payload"]["new_status"] == "QUEUED"


class TestTrainingControl:
    def test_pause_queued_semantics(self, handlers):
        h, ctx, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "t", "input_file": "C:/d.json",
            "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        task_id = result["payload"]["task_id"]
        # CREATED → pause 应冲突（适用 RUNNING/QUEUED）
        result = h.handle(FakeRequest(ReqMethod.RSI_TRAINING_PAUSE, {"task_id": task_id}))
        assert result["ok"] is False
        assert result["code"] == "TASK_STATE_CONFLICT"

    def test_terminate_created(self, handlers):
        h, ctx, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "t", "input_file": "C:/d.json",
            "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        task_id = result["payload"]["task_id"]
        result = h.handle(FakeRequest(ReqMethod.RSI_TRAINING_TERMINATE, {"task_id": task_id}))
        # 状态机允许 CREATED → TERMINATED
        assert result["ok"] is True
        assert result["payload"]["status"] == "TERMINATED"

    def test_start_requires_adapter(self, handlers):
        """无 adapter 注册时 start 仍入队；执行时 FAILED（引擎装配 ⚠️外部）。"""
        h, ctx, _ = handlers
        result = h.handle(FakeRequest(ReqMethod.RSI_TASK_CREATE, {
            "scenario": "HARNESS", "name": "t", "input_file": "C:/d.json",
            "model_refs": {"optimizer": "o", "tester": "e"},
        }))
        task_id = result["payload"]["task_id"]
        result = h.handle(FakeRequest(ReqMethod.RSI_TRAINING_START, {"task_id": task_id}))
        assert result["ok"] is True
        assert result["payload"]["status"] in {"QUEUED", "RUNNING"}
