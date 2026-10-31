from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import (
    ArtifactEngineAdapter,
)
from jiuwenswarm.agents.harness.common.rsi.models import TaskStatus


def _seed_completed_paper(run_dir: Path, task_id: str, *, valid: bool = True) -> Path:
    manager_run_id = f"{task_id}-iteration-001"
    paper_dir = run_dir / "experiments" / manager_run_id / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    final_paper = paper_dir / "main.pdf"
    final_paper.write_bytes(b"%PDF-1.7\nsynthetic paper\n")
    artifact_path = final_paper.relative_to(run_dir).as_posix()
    manager_state = {
        "reports": [
            {
                "module": "reporting",
                "mode": "run",
                "outcome": "succeeded",
                "artifact_paths": [artifact_path],
            }
        ]
    }
    state_path = run_dir / "experiments" / manager_run_id / "manager" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(manager_state), encoding="utf-8")
    if valid:
        (run_dir / "model_calls.jsonl").write_text(
            json.dumps(
                {
                    "call_id": "call-001",
                    "model_call": {"tokens": {"input": 11, "output": 7}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return final_paper


@dataclass
class _Artifact:
    artifact_id: str
    node_id: str
    name: str
    kind: str
    path: str
    sha256: str
    download_url: str | None = None


@dataclass
class _State:
    task_id: str
    status: str
    iteration: int
    best_node_id: str
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class _Report:
    task_id: str
    status: str
    best_node_id: str
    artifact_index: list[_Artifact]


class _DurablePaperProvider:
    supports_pause = True
    supports_resume = False

    def __init__(self, final_paper: Path, *, initial_status: str = "running") -> None:
        self.final_paper = final_paper
        self.initial_status = initial_status
        self.manager_run_id = final_paper.parents[1].name
        run_dir = final_paper.parents[3]
        self.final_package = run_dir / "artifacts" / "paper-optimization-001"
        (self.final_package / "paper").mkdir(parents=True)
        (self.final_package / "paper" / "main.pdf").write_bytes(final_paper.read_bytes())
        self.calls: list[str] = []

    def validate_input(self, path: str | None):
        del path
        return {"valid": True, "errors": []}

    async def run(self, request, *, on_event=None):
        del request, on_event
        self.calls.append("run")
        return SimpleNamespace(status=self.initial_status)

    def read_state(self, task_id: str):
        self.calls.append("read_state")
        return _State(
            task_id=task_id,
            status="completed",
            iteration=1,
            best_node_id="paper-node-1",
            error_code=None,
            error_message=None,
        )

    def read_report(self, task_id: str):
        self.calls.append("read_report")
        return _Report(
            task_id=task_id,
            status="completed",
            best_node_id="paper-node-1",
            artifact_index=[
                _Artifact(
                    artifact_id="A-paper:paper-node-1",
                    node_id="paper-node-1",
                    name="paper-optimization-001",
                    kind="paper_snapshot",
                    path=str(self.final_package),
                    sha256="fixture",
                    download_url=None,
                )
            ],
        )

    def locate_artifact(self, task_id: str, artifact_id: str | None = None):
        del task_id, artifact_id
        self.calls.append("locate_artifact")
        return _Artifact(
            artifact_id="A-paper:paper-node-1",
            node_id="paper-node-1",
            name="paper-optimization-001",
            kind="paper_snapshot",
            path=str(self.final_package),
            sha256="fixture",
            download_url=None,
        )

    def get_tree(self, task_id: str):
        del task_id
        self.calls.append("get_tree")
        return {
            "nodes": [
                {
                    "node_id": "paper-node-1",
                    "extra": {"manager_run_id": self.manager_run_id},
                }
            ]
        }


class _TestPaperAdapter(ArtifactEngineAdapter):
    def build_request(self, task, *, resume: bool = False):
        del resume
        return task


def _adapter(
    tasks_root: Path,
    task_id: str,
    *,
    valid: bool = True,
    initial_status: str = "running",
):
    run_dir = tasks_root / task_id / "run"
    final_paper = _seed_completed_paper(run_dir, task_id, valid=valid)
    provider = _DurablePaperProvider(final_paper, initial_status=initial_status)
    return _TestPaperAdapter(
        "PAPER",
        provider,
        requires_model=False,
        tasks_root=tasks_root,
    ), provider, run_dir


def _paper_task(ctx, name: str) -> str:
    return ctx.task_service.create(
        {
            "scenario": "ARTIFACT",
            "artifact_type": "PAPER",
            "name": name,
            "model_refs": {"optimizer": "fixture-model"},
            "optimization_instruction": "produce a paper",
        }
    )["task_id"]


async def _run_queued_task(ctx, task_id: str) -> None:
    ctx.worker.enqueue(task_id)
    await asyncio.wait_for(ctx.worker._queue.join(), timeout=1)  # noqa: SLF001
    runner = ctx.worker._run_task  # noqa: SLF001
    assert runner is not None
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner


def test_paper_adapter_pass_writes_audit_and_preserves_artifact(tmp_path: Path) -> None:
    task_id = "rsi-paper-pass"
    adapter, provider, run_dir = _adapter(tmp_path, task_id)

    result = adapter.finalize_terminal(
        task_id,
        SimpleNamespace(status="completed", final_node_id="paper-node-1"),
    )

    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["decision"] == "PASS"
    assert audit["run"] == {
        "task_id": task_id,
        "manager_run_id": f"{task_id}-iteration-001",
        "iteration": 1,
    }
    assert result.status == "completed"
    assert result.final_node_id == "paper-node-1"
    assert result.best_artifact_path == str(provider.final_package.resolve())
    assert (provider.final_package / "audit.json").is_file()
    assert not (run_dir / "audit.json.tmp").exists()


def test_paper_adapter_blocked_writes_audit_and_preserves_artifact(tmp_path: Path) -> None:
    task_id = "rsi-paper-blocked"
    adapter, provider, run_dir = _adapter(tmp_path, task_id, valid=False)

    result = adapter.finalize_terminal(
        task_id,
        SimpleNamespace(status="completed", final_node_id="paper-node-1"),
    )

    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["decision"] == "BLOCKED"
    assert result.status == "failed"
    assert result.error_code == "PAPER_PROVENANCE_BLOCKED"
    assert len(result.error_message) <= 500
    assert result.final_node_id == "paper-node-1"
    assert result.best_artifact_path == str(provider.final_package.resolve())
    assert (provider.final_package / "audit.json").is_file()


@pytest.mark.parametrize("valid", [False, True])
def test_restart_recovery_finalizes_paper_before_publishing(
    tmp_path: Path, valid: bool
) -> None:
    context = build_rsi_service_context(tmp_path)
    task_id = _paper_task(context, "restart gate")
    context.store.update_status(task_id, ["CREATED"], "QUEUED")
    context.store.update_status(task_id, ["QUEUED"], "RUNNING")
    adapter, provider, run_dir = _adapter(tmp_path, task_id, valid=valid)
    observed = []
    restarted = build_rsi_service_context(tmp_path, adapters={"ARTIFACT:PAPER": adapter})

    def on_status_changed(task_id, old, new):
        del old
        audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
        assert restarted.store.get(task_id).config["results"]["best_artifact_path"]
        observed.append((new, audit["decision"]))

    restarted.store.set_status_changed_callback(on_status_changed)
    summary = restarted.recover_workspace()
    task = restarted.store.get(task_id)
    expected = "COMPLETED" if valid else "FAILED"
    assert task.status == expected
    assert observed == [(expected, "PASS" if valid else "BLOCKED")]
    assert summary[expected.lower()] == 1
    assert (provider.final_package / "paper" / "main.pdf").is_file()
    assert (provider.final_package / "audit.json").is_file()
    assert task.config["results"]["best_artifact_path"] == str(provider.final_package.resolve())
    if not valid:
        assert task.config["results"]["error_code"] == "PAPER_PROVENANCE_BLOCKED"
        assert "APG004" in task.config["results"]["error_message"]
    assert "run" not in provider.calls


def test_restart_recovery_finalization_exception_fails_closed(tmp_path: Path) -> None:
    context = build_rsi_service_context(tmp_path)
    task_id = _paper_task(context, "corrupt provider report")
    context.store.update_status(task_id, ["CREATED"], "QUEUED")
    context.store.update_status(task_id, ["QUEUED"], "RUNNING")
    adapter, provider, _ = _adapter(tmp_path, task_id)

    def read_report(task_id):
        del task_id
        raise RuntimeError("durable report unavailable")

    provider.read_report = read_report
    restarted = build_rsi_service_context(tmp_path, adapters={"ARTIFACT:PAPER": adapter})
    restarted.recover_workspace()
    task = restarted.store.get(task_id)
    assert task.status == "FAILED"
    assert task.config["results"]["error_message"] == "durable report unavailable"
    assert "run" not in provider.calls


def test_restart_recovery_terminal_result_persist_failure_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    context = build_rsi_service_context(tmp_path)
    task_id = _paper_task(context, "terminal result disk failure")
    context.store.update_status(task_id, ["CREATED"], "QUEUED")
    context.store.update_status(task_id, ["QUEUED"], "RUNNING")
    adapter, provider, run_dir = _adapter(tmp_path, task_id)
    restarted = build_rsi_service_context(tmp_path, adapters={"ARTIFACT:PAPER": adapter})

    def fail_persist(*args, **kwargs):
        del args, kwargs
        raise OSError("terminal result write failed")

    monkeypatch.setattr(restarted.store, "update_status_with_results", fail_persist)
    summary = restarted.recover_workspace()
    task = restarted.store.get(task_id)
    assert summary["failed"] == 1
    assert task.status == "FAILED"
    assert task.status_history[-1]["cause"] == "worker.terminal_evidence_persist_failed"
    assert json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))["decision"] == "PASS"
    assert "run" not in provider.calls


def test_restart_recovery_does_not_reopen_a_failed_paper(tmp_path: Path) -> None:
    context = build_rsi_service_context(tmp_path)
    task_id = _paper_task(context, "already blocked")
    context.store.update_status(task_id, ["CREATED"], "QUEUED")
    context.store.update_status(task_id, ["QUEUED"], "RUNNING")
    context.store.update_status(task_id, ["RUNNING"], "FAILED", cause="APG004")
    adapter, provider, run_dir = _adapter(tmp_path, task_id)
    original_task = (tmp_path / task_id / "task.json").read_bytes()
    restarted = build_rsi_service_context(tmp_path, adapters={"ARTIFACT:PAPER": adapter})
    summary = restarted.recover_workspace()
    assert summary["scanned"] == 0
    assert restarted.store.get(task_id).status == "FAILED"
    assert (tmp_path / task_id / "task.json").read_bytes() == original_task
    assert not (run_dir / "audit.json").exists()
    assert not provider.calls


@pytest.mark.parametrize("status", ["failed", "paused", "terminated"])
def test_paper_adapter_does_not_audit_other_terminal_states(
    tmp_path: Path, status: str
) -> None:
    task_id = f"rsi-paper-{status}"
    adapter, provider, run_dir = _adapter(tmp_path, task_id)
    terminal = SimpleNamespace(status=status, error_code="PROVIDER_RESULT")

    result = adapter.finalize_terminal(task_id, terminal)

    assert result is terminal
    assert provider.calls == []
    assert not (run_dir / "audit.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["running", "completed"])
async def test_worker_publishes_completed_only_after_durable_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_status: str
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, "paper-pass")
    adapter, provider, run_dir = _adapter(
        ctx.store.tasks_root, task_id, initial_status=initial_status
    )
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})
    real_update_status = ctx.store.update_status_with_results

    def assert_audit_before_completed(task, from_states, to_state, results, cause=""):
        if to_state == TaskStatus.COMPLETED.value:
            assert json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))[
                "decision"
            ] == "PASS"
        return real_update_status(task, from_states, to_state, results, cause=cause)

    monkeypatch.setattr(ctx.store, "update_status_with_results", assert_audit_before_completed)

    await _run_queued_task(ctx, task_id)

    task = ctx.store.get(task_id)
    assert task.status == TaskStatus.COMPLETED.value
    assert task.config["results"]["final_node_id"] == "paper-node-1"
    assert task.config["results"]["best_artifact_path"] == str(
        provider.final_package.resolve()
    )
    assert provider.calls.index("read_state") < provider.calls.index("read_report")
    assert provider.calls.index("read_state") < provider.calls.index("locate_artifact")


def test_public_paper_report_does_not_publish_provider_completion_before_gate(
    tmp_path: Path,
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, "paper-awaiting-gate")
    adapter, _, _ = _adapter(ctx.store.tasks_root, task_id)
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})

    report = ctx.report_service.get({"task_id": task_id})

    assert report["status"] == TaskStatus.CREATED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["running", "completed"])
async def test_worker_maps_blocked_audit_to_dedicated_failure(
    tmp_path: Path, initial_status: str
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, "paper-blocked")
    adapter, provider, run_dir = _adapter(
        ctx.store.tasks_root, task_id, valid=False, initial_status=initial_status
    )
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})

    await _run_queued_task(ctx, task_id)

    task = ctx.store.get(task_id)
    assert task.status == TaskStatus.FAILED.value
    assert json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))[
        "decision"
    ] == "BLOCKED"
    assert task.config["results"]["error_code"] == "PAPER_PROVENANCE_BLOCKED"
    assert len(task.config["results"]["error_message"]) <= 500
    assert task.config["results"]["final_node_id"] == "paper-node-1"
    assert task.config["results"]["best_artifact_path"] == str(
        provider.final_package.resolve()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid", "terminal_status"),
    [
        (True, TaskStatus.COMPLETED.value),
        (False, TaskStatus.FAILED.value),
    ],
)
async def test_terminal_callback_reads_persisted_finalization_evidence(
    tmp_path: Path,
    valid: bool,
    terminal_status: str,
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, f"paper-terminal-{terminal_status.lower()}")
    adapter, provider, _ = _adapter(ctx.store.tasks_root, task_id, valid=valid)
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})
    callback_results: list[dict[str, str]] = []

    def read_terminal_record(changed_task_id: str, old: str, new: str) -> None:
        del old
        if changed_task_id == task_id and new == terminal_status:
            task = ctx.store.get(changed_task_id)
            callback_results.append(dict(task.config.get("results") or {}))

    ctx.store.set_status_changed_callback(read_terminal_record)

    await _run_queued_task(ctx, task_id)

    assert len(callback_results) == 1
    results = callback_results[0]
    assert results["final_node_id"] == "paper-node-1"
    assert results["best_artifact_path"] == str(provider.final_package.resolve())
    if terminal_status == TaskStatus.FAILED.value:
        assert results["error_code"] == "PAPER_PROVENANCE_BLOCKED"
        assert results["error_message"]
        assert len(results["error_message"]) <= 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid", "terminal_status"),
    [
        (True, TaskStatus.COMPLETED.value),
        (False, TaskStatus.FAILED.value),
    ],
)
async def test_terminal_evidence_write_failure_does_not_publish_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
    terminal_status: str,
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, f"paper-write-failure-{terminal_status.lower()}")
    adapter, _, _ = _adapter(ctx.store.tasks_root, task_id, valid=valid)
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})
    terminal_changes: list[tuple[str, str, str]] = []
    real_write_task = ctx.store._write_task  # noqa: SLF001
    failed = False

    def fail_first_terminal_evidence_write(task_dir: Path, payload: dict) -> None:
        nonlocal failed
        results = (payload.get("config") or {}).get("results") or {}
        if not failed and results.get("final_node_id") == "paper-node-1":
            failed = True
            raise OSError("terminal evidence write failed")
        real_write_task(task_dir, payload)

    def record_terminal_change(changed_task_id: str, old: str, new: str) -> None:
        if changed_task_id == task_id and new == terminal_status:
            terminal_changes.append((changed_task_id, old, new))

    monkeypatch.setattr(ctx.store, "_write_task", fail_first_terminal_evidence_write)
    ctx.store.set_status_changed_callback(record_terminal_change)

    await _run_queued_task(ctx, task_id)

    task = ctx.store.get(task_id)
    assert failed
    assert task.status == TaskStatus.FAILED.value
    assert task.config.get("results", {}).get("final_node_id") is None
    assert all(new != TaskStatus.COMPLETED.value for _, _, new in terminal_changes)
    assert task.status_history[-1]["cause"] == "worker.terminal_evidence_persist_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["evaluate", "serialize", "write"])
@pytest.mark.parametrize("initial_status", ["running", "completed"])
async def test_worker_keeps_unexpected_audit_errors_ordinary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    initial_status: str,
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, f"paper-{failure_point}-error")
    adapter, _, run_dir = _adapter(
        ctx.store.tasks_root, task_id, initial_status=initial_status
    )
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})
    if failure_point == "evaluate":
        from jiuwenswarm.agents.harness.common.rsi import artifact_adapter

        monkeypatch.setattr(
            artifact_adapter.ArtifactProvenanceGate,
            "evaluate",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("unexpected audit evaluation failure")
            ),
        )
        expected = "unexpected audit evaluation failure"
    elif failure_point == "serialize":
        from jiuwenswarm.agents.harness.common.rsi import artifact_adapter

        monkeypatch.setattr(
            artifact_adapter,
            "_atomic_write_json",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                TypeError("unexpected audit serialization failure")
            ),
        )
        expected = "unexpected audit serialization failure"
    else:
        real_replace = Path.replace

        def fail_audit_replace(path: Path, target: Path):
            if path.name == "audit.json.tmp":
                raise OSError("unexpected audit write failure")
            return real_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_audit_replace)
        expected = "unexpected audit write failure"

    await _run_queued_task(ctx, task_id)

    task = ctx.store.get(task_id)
    assert task.status == TaskStatus.FAILED.value
    assert expected in task.status_history[-1]["cause"]
    assert task.config.get("results", {}).get("error_code") != (
        "PAPER_PROVENANCE_BLOCKED"
    )
    assert not (run_dir / "audit.json").exists()


@pytest.mark.asyncio
async def test_worker_publication_evidence_read_failure_does_not_leave_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = build_rsi_service_context(tmp_path)
    task_id = _paper_task(ctx, "paper-publication-read-failure")
    adapter, _, _ = _adapter(ctx.store.tasks_root, task_id)
    ctx.register_adapters({"ARTIFACT:PAPER": adapter})

    def fail_read(task: str):
        del task
        raise OSError("publication evidence read failed")

    monkeypatch.setattr(adapter, "read_publication_state", fail_read, raising=False)

    await _run_queued_task(ctx, task_id)

    task = ctx.store.get(task_id)
    assert task.status == TaskStatus.FAILED.value
    assert task.config.get("results", {}).get("final_node_id") is None
    assert task.status_history[-1]["cause"] == "worker.terminal_evidence_persist_failed"
