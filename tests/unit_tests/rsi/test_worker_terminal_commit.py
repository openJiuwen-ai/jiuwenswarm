"""Preserve a concurrent committed state and results at worker termination."""
import threading
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.worker import logger


@pytest.fixture
def running_task(tmp_path):
    ctx = build_rsi_service_context(tmp_path / "runtime")
    task_id = ctx.task_service.create({
        "scenario": "HARNESS", "name": "terminal-commit-regression",
        "input_file": "C:/unused.json",
        "model_refs": {"optimizer": "unused", "tester": "unused"},
    })["task_id"]
    ctx.store.update_status(task_id, ["CREATED"], "QUEUED", cause="test.setup")
    ctx.store.update_status(task_id, ["QUEUED"], "RUNNING", cause="test.setup")
    return ctx, task_id


@pytest.mark.parametrize("winner_status", ["TERMINATED", "PAUSED", "COMPLETED"])
def test_concurrent_commit_preserves_winner_state_and_results(running_task, monkeypatch, winner_status):
    """A real second thread commits between the initial read and transition."""
    ctx, task_id = running_task
    ready, committed = threading.Event(), threading.Event()
    original_atomic = ctx.store.update_status_with_results
    controller_errors = []

    def delayed_atomic(*args, **kwargs):
        ready.set()
        assert committed.wait(5), "concurrent commit did not complete"
        return original_atomic(*args, **kwargs)

    def controller():
        try:
            assert ready.wait(5), "worker did not reach transition boundary"
            original_atomic(task_id, ["RUNNING"], winner_status,
                            {"best_score": 0.9, "final_node_id": "winner"},
                            cause="test.concurrent.winner")
        except BaseException as exc:
            controller_errors.append(repr(exc))
        finally:
            committed.set()

    monkeypatch.setattr(ctx.store, "update_status_with_results", delayed_atomic)
    thread = threading.Thread(target=controller)
    thread.start()
    try:
        ctx.worker._apply_result_status(
            task_id, SimpleNamespace(status="COMPLETED", best_score=0.75, final_node_id="stale")
        )
    finally:
        thread.join(5)
    assert not thread.is_alive() and not controller_errors
    task = ctx.store.get(task_id)
    assert task.status == winner_status
    assert task.config["results"]["best_score"] == 0.9
    assert task.config["results"]["final_node_id"] == "winner"


def test_after_commit_callback_error_preserves_completed_evidence(running_task, caplog):
    """The real store writes state/results before invoking the public hook."""
    ctx, task_id = running_task

    def callback(task, old, new):
        if new == "COMPLETED":
            raise RuntimeError("intentional callback error after durable commit")

    ctx.store.set_status_changed_callback(callback)
    logger.addHandler(caplog.handler)
    try:
        ctx.worker._apply_result_status(task_id, SimpleNamespace(status="COMPLETED", best_score=0.75))
    finally:
        logger.removeHandler(caplog.handler)
    task = ctx.store.get(task_id)
    assert task.status == "COMPLETED"
    assert task.config["results"]["best_score"] == 0.75
    assert "intentional callback error after durable commit" in caplog.text
    assert "终态证据持久化失败" not in caplog.text


def test_normal_completion_still_atomically_commits_results(running_task):
    ctx, task_id = running_task
    ctx.worker._apply_result_status(task_id, SimpleNamespace(status="COMPLETED", best_score=0.75))
    task = ctx.store.get(task_id)
    assert task.status == "COMPLETED"
    assert task.config["results"]["best_score"] == 0.75


def test_preexisting_nonrunning_branch_still_persists_evidence(running_task):
    """The existing initial non-running path intentionally retains evidence."""
    ctx, task_id = running_task
    ctx.store.update_status(task_id, ["RUNNING"], "TERMINATED", cause="test.preexisting.control")
    ctx.worker._apply_result_status(task_id, SimpleNamespace(status="COMPLETED", state_path="retained-state"))
    task = ctx.store.get(task_id)
    assert task.status == "TERMINATED"
    assert task.config["results"]["state_path"] == "retained-state"


def fail_completed_write(ctx, monkeypatch):
    """Inject a pre-commit I/O failure while retaining the real transaction."""
    original_write = ctx.store._write_task

    def write(task_dir, payload):
        if payload["status"] == "COMPLETED":
            raise PermissionError("intentional pre-commit write failure")
        return original_write(task_dir, payload)

    monkeypatch.setattr(ctx.store, "_write_task", write)


def test_second_race_before_failed_fallback_preserves_real_winner(running_task, monkeypatch):
    """A second real thread commits after re-read but before FAILED fallback."""
    ctx, task_id = running_task
    fail_completed_write(ctx, monkeypatch)
    ready, committed = threading.Event(), threading.Event()
    original_transition = ctx.store.update_status
    controller_errors = []

    def delayed_fallback(task, from_states, to_state, cause=""):
        if cause == "worker.terminal_evidence_persist_failed":
            ready.set()
            assert committed.wait(5), "concurrent fallback winner did not commit"
        return original_transition(task, from_states, to_state, cause)

    def controller():
        try:
            assert ready.wait(5), "worker did not reach fallback boundary"
            ctx.store.update_status_with_results(
                task_id, ["RUNNING"], "TERMINATED",
                {"best_score": 0.9, "final_node_id": "winner"},
                cause="test.concurrent.fallback.winner",
            )
        except BaseException as exc:
            controller_errors.append(repr(exc))
        finally:
            committed.set()

    monkeypatch.setattr(ctx.store, "update_status", delayed_fallback)
    thread = threading.Thread(target=controller)
    thread.start()
    try:
        ctx.worker._apply_result_status(
            task_id, SimpleNamespace(status="COMPLETED", best_score=0.75, final_node_id="stale")
        )
    finally:
        thread.join(5)
    assert not thread.is_alive() and not controller_errors
    task = ctx.store.get(task_id)
    assert task.status == "TERMINATED"
    assert task.config["results"]["best_score"] == 0.9
    assert task.config["results"]["final_node_id"] == "winner"


def test_precommit_write_failure_with_running_state_still_converges_failed(running_task, monkeypatch):
    ctx, task_id = running_task
    fail_completed_write(ctx, monkeypatch)
    ctx.worker._apply_result_status(task_id, SimpleNamespace(status="COMPLETED", best_score=0.75))
    task = ctx.store.get(task_id)
    assert task.status == "FAILED"
    assert task.status_history[-1]["cause"] == "worker.terminal_evidence_persist_failed"
    assert "best_score" not in task.config.get("results", {})
