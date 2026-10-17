from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from openjiuwen.rsi.artifact_rsi.program_opt import PuctProgramArtifactProvider


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_program_provider_reads_snapshots_after_task_directory_move(tmp_path: Path) -> None:
    tasks_root = tmp_path / "workspace" / "rsi" / "tasks"
    task_id = "rsi-moved-program"
    run_dir = tasks_root / task_id / "run"
    usage = {
        "tokens": {"input": 44014, "output": 131376, "cache_hit": 0},
        "cost_estimate": 0.0,
        "call_count": 22,
    }
    _write_json(
        run_dir / "state.json",
        {
            "task_id": task_id,
            "status": "completed",
            "iteration": 20,
            "total_iterations": 20,
            "score": 1.0,
            "baseline": 0.391667,
            "best_node_id": f"artifact:{task_id}:attempt:1",
            "usage": usage,
        },
    )
    _write_json(
        run_dir / "report.json",
        {
            "task_id": task_id,
            "status": "completed",
            "best_node_id": f"artifact:{task_id}:attempt:1",
            "usage": usage,
            "artifact_index": [],
        },
    )

    adapter = ArtifactEngineAdapter(
        "PROGRAM",
        PuctProgramArtifactProvider(),
        tasks_root=tasks_root,
    )

    state = adapter.read_state(task_id)
    report = adapter.read_report(task_id)

    assert state.score == 1.0
    assert state.baseline == 0.391667
    assert state.usage is not None
    assert state.usage.tokens.input == 44014
    assert state.usage.call_count == 22
    assert report.usage is not None
    assert report.usage.tokens.output == 131376
