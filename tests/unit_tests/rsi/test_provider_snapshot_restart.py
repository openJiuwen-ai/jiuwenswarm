"""RSI query regressions against persisted snapshots and production Providers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, TaskStatus, utcnow_iso
from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters
from jiuwenswarm.agents.harness.common.rsi.task_store import RsiTaskStore
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers

_PAPER_TASK_ID = "rsi-restart-paper"
_PAPER_ROOT_NODE_ID = f"artifact:{_PAPER_TASK_ID}:node:0"
_PAPER_ADOPTED_NODE_ID = f"artifact:{_PAPER_TASK_ID}:node:1"
_PAPER_ARTIFACT_ID = f"artifact:{_PAPER_TASK_ID}:snapshot:1"
_PAPER_USAGE = {
    "tokens": {"input": 1234, "output": 67, "cache_hit": 100},
    "cost_estimate": 0.0,
    "call_count": 4,
}


def _persist_task(tasks_root: Path, task_id: str, artifact_type: str | None) -> Path:
    run_dir = tasks_root / task_id / "run"
    RsiTaskStore(tasks_root).create(
        RsiTask(
            task_id=task_id,
            name=task_id,
            scenario="ARTIFACT" if artifact_type else "HARNESS",
            artifact_type=artifact_type,
            status=TaskStatus.COMPLETED.value,
            created_at=utcnow_iso(),
            max_iterations=3,
            run_dir=str(run_dir),
        )
    )
    run_dir.mkdir()
    return run_dir


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _persist_paper_state(run_dir: Path, usage: dict[str, Any] | None) -> None:
    state = {
        "task_id": _PAPER_TASK_ID,
        "run_dir": str(run_dir),
        "status": "completed",
        "max_iterations": 3,
        "node_count": 2,
        "score": 6.75,
        "baseline": 4.5,
        "best_node_id": _PAPER_ADOPTED_NODE_ID,
    }
    if usage is not None:
        state["usage"] = usage
    _write_json(run_dir / "state.json", state)


@pytest.fixture
def paper_snapshot(tmp_path: Path) -> Path:
    run_dir = _persist_task(tmp_path, _PAPER_TASK_ID, "PAPER")
    _persist_paper_state(run_dir, _PAPER_USAGE)
    _write_json(
        run_dir / "tree.json",
        [
            {"node_id": _PAPER_ROOT_NODE_ID, "iteration": 0, "parent_id": None, "type": "root", "adopted": True},
            {
                "node_id": _PAPER_ADOPTED_NODE_ID,
                "iteration": 1,
                "parent_id": _PAPER_ROOT_NODE_ID,
                "type": "reporting",
                "adopted": True,
                "score": 6.75,
                "snapshot_artifact_id": _PAPER_ARTIFACT_ID,
            },
            {
                "node_id": f"artifact:{_PAPER_TASK_ID}:node:2",
                "iteration": 2,
                "parent_id": _PAPER_ADOPTED_NODE_ID,
                "type": "pruned",
                "adopted": False,
            },
        ],
    )
    paper = run_dir / "best-paper"
    paper.mkdir()
    (paper / "main.tex").write_text("Paper fixture", encoding="utf-8")
    _write_json(
        run_dir / "artifacts.json",
        {
            _PAPER_ARTIFACT_ID: {
                "artifact_id": _PAPER_ARTIFACT_ID,
                "node_id": _PAPER_ADOPTED_NODE_ID,
                "name": "Best paper",
                "path": str(paper),
            },
        },
    )
    return tmp_path


def _query(tasks_root: Path, task_id: str, method: ReqMethod) -> dict[str, Any]:
    # A fresh process has disk snapshots only. Each endpoint must work as the
    # first request, with no earlier query or training call to register the task.
    adapters = build_rsi_adapters(tasks_root, harness_provider=HarnessProvider(tasks_root))
    context = build_rsi_service_context(tasks_root, adapters=adapters)
    handlers = RsiAgentServerHandlers(context)
    return handlers.handle(SimpleNamespace(req_method=method, params={"task_id": task_id}))


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    assert response["ok"] is True, response
    return response["payload"]


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(_PAPER_USAGE, id="recorded-tokens"),
        pytest.param(
            {"tokens": {"input": 0, "output": 0, "cache_hit": 0}, "cost_estimate": 0.0, "call_count": 2},
            id="recorded-zero-tokens",
        ),
    ],
)
def test_paper_usage_get_reads_saved_totals_after_restart(tmp_path: Path, usage: dict[str, Any]) -> None:
    run_dir = _persist_task(tmp_path, _PAPER_TASK_ID, "PAPER")
    _persist_paper_state(run_dir, usage)

    result = _payload(_query(tmp_path, _PAPER_TASK_ID, ReqMethod.RSI_USAGE_GET))

    assert result["usage"] == usage


def test_paper_usage_get_stays_unavailable_when_legacy_snapshot_has_no_usage(tmp_path: Path) -> None:
    run_dir = _persist_task(tmp_path, _PAPER_TASK_ID, "PAPER")
    _persist_paper_state(run_dir, usage=None)

    response = _query(tmp_path, _PAPER_TASK_ID, ReqMethod.RSI_USAGE_GET)

    assert response["ok"] is False
    assert response["code"] == "TASK_NOT_FOUND"


def test_paper_task_get_reads_saved_progress_and_usage_after_restart(paper_snapshot: Path) -> None:
    detail = _payload(_query(paper_snapshot, _PAPER_TASK_ID, ReqMethod.RSI_TASK_GET))

    assert detail["status"] == "COMPLETED"
    assert detail.get("progress") == {"iteration": 2, "total_iterations": 3, "score": 6.75, "baseline": 4.5}
    assert detail["usage"] == _PAPER_USAGE
    assert detail["best_artifact"]["artifact_id"] == _PAPER_ARTIFACT_ID


def test_paper_report_get_reads_saved_summary_after_restart(paper_snapshot: Path) -> None:
    report = _payload(_query(paper_snapshot, _PAPER_TASK_ID, ReqMethod.RSI_REPORT_GET))

    assert report["status"] == "COMPLETED"
    assert report["best_score"] == 6.75
    assert report["baseline"] == 4.5
    assert report["metrics"]["iterations"] == 2
    assert report["metrics"]["pruned_count"] == 1
    assert report["usage"] == _PAPER_USAGE
    assert report["best_artifact"]["artifact_id"] == _PAPER_ARTIFACT_ID


def test_paper_tree_get_recovers_provider_nodes_after_restart(paper_snapshot: Path) -> None:
    tree = _payload(_query(paper_snapshot, _PAPER_TASK_ID, ReqMethod.RSI_TREE_GET))

    assert tree["iteration"] == 2
    nodes = {node["node_id"]: node for node in tree["nodes"]}
    assert {node["type"] for node in nodes.values()} == {"ROOT", "ADOPTED", "PRUNED"}
    assert nodes[_PAPER_ADOPTED_NODE_ID]["score"] == 6.75
    assert nodes[_PAPER_ADOPTED_NODE_ID]["snapshot_artifact_id"] == _PAPER_ARTIFACT_ID


def test_paper_artifact_download_locates_saved_directory_after_restart(paper_snapshot: Path) -> None:
    artifact = _payload(_query(paper_snapshot, _PAPER_TASK_ID, ReqMethod.RSI_ARTIFACT_DOWNLOAD))

    expected = paper_snapshot / _PAPER_TASK_ID / "run" / "best-paper"
    assert Path(artifact["path"]) == expected.resolve()
    assert artifact["is_directory"] is True
    assert artifact["is_best"] is True


def test_program_usage_get_reads_saved_totals_after_restart(tmp_path: Path) -> None:
    task_id = f"rsi-restart-program-{tmp_path.name}"
    run_dir = _persist_task(tmp_path, task_id, "PROGRAM")
    usage = {"tokens": {"input": 310, "output": 29, "cache_hit": 0}, "cost_estimate": 0.0, "call_count": 3}
    _write_json(run_dir / "state.json", {"task_id": task_id, "status": "completed", "usage": usage})

    result = _payload(_query(tmp_path, task_id, ReqMethod.RSI_USAGE_GET))

    assert result["usage"] == usage


def test_harness_usage_get_reads_saved_totals_after_restart(tmp_path: Path) -> None:
    task_id = "rsi-restart-harness"
    run_dir = _persist_task(tmp_path, task_id, None)
    usage = {"tokens": {"input": 2350, "output": 100, "cache_hit": 800}, "cost_estimate": 0.0, "call_count": 7}
    (run_dir / "single_harness_state.yaml").write_text(
        yaml.safe_dump({"status": "completed", "usage": usage}),
        encoding="utf-8",
    )

    result = _payload(_query(tmp_path, task_id, ReqMethod.RSI_USAGE_GET))

    assert result["usage"] == usage
