"""Report metrics must describe the same durable candidates as the tree."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask


@pytest.mark.parametrize("scenario,artifact_type", [("HARNESS", None), ("ARTIFACT", "PAPER")])
def test_report_counts_pruned_nodes_when_provider_has_no_metrics(tmp_path, scenario, artifact_type):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", scenario, "FAILED", "2026-11-14", artifact_type))
    adapter = SimpleNamespace(
        read_state=lambda _: {"status": "failed", "iteration": 1},
        read_report=lambda _: {"status": "failed"},
        get_tree=lambda _: {"iteration": 1, "nodes": [
            {"node_id": "root", "type": "root", "iteration": 0},
            {"node_id": "n1", "type": "pruned", "iteration": 1},
            {"node_id": "n2", "type": "rejected", "iteration": 1},
            {"node_id": "n3", "type": "provisional", "iteration": 2},
        ]},
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["pruned_count"] == 1
    assert report["metrics"]["iterations"] == 1


def test_report_counts_threshold_stop_skips_as_pruned(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "ARTIFACT", "COMPLETED", "2026-11-14", "PROGRAM"))
    adapter = SimpleNamespace(
        read_state=lambda _: {"iteration": 1},
        read_report=lambda _: {"status": "completed"},
        get_tree=lambda _: {"iteration": 1, "nodes": [
            {
                "node_id": "n1",
                "type": "rejected",
                "iteration": 1,
                "failure_class": "SKIPPED_TARGET_REACHED",
                "reason": "其他候选已达到目标分数",
            },
            {"node_id": "n2", "type": "adopted", "iteration": 1, "adopted": True},
        ]},
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["pruned_count"] == 1


def test_report_preserves_explicit_provider_metrics(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "HARNESS", "COMPLETED", "2026-11-14"))
    adapter = SimpleNamespace(
        read_state=lambda _: {"iteration": 3},
        read_report=lambda _: {"metrics": {"pruned_count": 0, "iterations": 2}},
        get_tree=lambda _: pytest.fail("Explicit report statistics must not require reading the tree"),
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["pruned_count"] == 0
    assert report["metrics"]["iterations"] == 2


def test_missing_tree_does_not_claim_zero_pruned_nodes(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "ARTIFACT", "FAILED", "2026-11-14", "PAPER"))
    adapter = SimpleNamespace(
        read_state=lambda _: {"iteration": 1},
        read_report=lambda _: {"status": "failed"},
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["pruned_count"] is None


def test_metric_only_projection_has_a_report_before_root_registration(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "HARNESS", "RUNNING", "2026-11-14"))
    context.projector.on_progress_metric("task", {"iteration": 0, "total_iterations": 2})
    report = context.report_service.get({"task_id": "task"})
    assert report["metrics"]["iterations"] == 0
    assert report["metrics"]["pruned_count"] is None


def test_fallback_report_restores_disk_tree_for_pruned_count(tmp_path):
    tasks_root = tmp_path / "tasks"
    original = build_rsi_service_context(tasks_root)
    original.store.create(RsiTask("task", "test", "HARNESS", "FAILED", "2026-11-14"))
    original.projector.register_root("task")
    original.projector.on_provider_node("task", {
        "node_id": "n1",
        "type": "PRUNED",
        "iteration": 1,
        "parent_id": "ROOT",
    })
    restarted = build_rsi_service_context(tasks_root)
    report = restarted.report_service.get({"task_id": "task"})
    assert report["metrics"]["pruned_count"] == 1


def test_program_report_uses_completed_iterations_not_largest_attempt_number(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "ARTIFACT", "COMPLETED", "2026-11-14", "PROGRAM"))
    adapter = SimpleNamespace(
        read_state=lambda _: {"iteration": 1},
        read_report=lambda _: {"status": "completed"},
        get_tree=lambda _: {"iteration": 1, "nodes": [
            {"node_id": "n1", "type": "adopted", "iteration": 1},
            {"node_id": "n2", "type": "pruned", "iteration": 2},
        ]},
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["iterations"] == 1
    assert report["metrics"]["pruned_count"] == 1


def test_iterations_fall_back_to_tree_when_state_iteration_is_none(tmp_path):
    context = build_rsi_service_context(tmp_path)
    context.store.create(RsiTask("task", "test", "HARNESS", "FAILED", "2026-11-14"))
    adapter = SimpleNamespace(
        read_state=lambda _: {"iteration": None, "status": "failed"},
        read_report=lambda _: {"status": "failed"},
        get_tree=lambda _: {"iteration": 2, "nodes": []},
    )
    report = context.report_service.get({"task_id": "task"}, adapter=adapter)
    assert report["metrics"]["iterations"] == 2
