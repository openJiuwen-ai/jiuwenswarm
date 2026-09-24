"""Paper Provider snapshots must stay readable after an AgentServer restart.

agent-core's ``PaperArtifactProviderImpl`` keeps ``task_id -> run_dir`` in an
instance-local dict that a restart empties; the adapter re-seeds the index so
``read_state``/``read_report`` keep answering from the durable snapshots.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from openjiuwen.rsi.artifact_rsi.request import ArtifactEngineRequest
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.provider import (
    PaperArtifactProviderImpl,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.schemas import (
    PaperTaskState,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.storage import (
    TaskStorage,
)
from openjiuwen.rsi.usage import record_model_usage


def test_paper_provider_reads_snapshots_after_restart(tmp_path: Path) -> None:
    tasks_root = tmp_path / "workspace" / "rsi" / "tasks"
    task_id = "rsi-restarted-paper"
    run_dir = tasks_root / task_id / "run"
    storage = TaskStorage(run_dir)
    storage.save_task_state(
        PaperTaskState(
            task_id=task_id,
            run_dir=str(run_dir),
            status="completed",
            max_iterations=5,
            score=0.87,
            baseline=0.52,
            node_count=2,
            usage={
                "tokens": {"input": 100, "output": 200, "cache_hit": 0},
                "cost_estimate": 0.0,
                "call_count": 3,
            },
        )
    )

    # A fresh Provider instance models the post-restart process: its
    # in-memory run-dir index is empty until the adapter re-seeds it.
    adapter = ArtifactEngineAdapter(
        "PAPER",
        PaperArtifactProviderImpl(),
        tasks_root=tasks_root,
    )

    state = adapter.read_state(task_id)
    report = adapter.read_report(task_id)

    assert state.status == "completed"
    assert state.baseline == 0.52
    assert state.score == 0.87
    assert state.usage is not None
    assert state.usage.tokens.input == 100
    assert state.usage.call_count == 3
    assert report.baseline == 0.52
    assert report.usage is not None


def test_paper_adapter_persists_model_usage_from_completed_run(tmp_path: Path) -> None:
    tasks_root = tmp_path / "workspace" / "rsi" / "tasks"
    task_id = "rsi-paper-with-usage"
    run_dir = tasks_root / task_id / "run"
    run_dir.mkdir(parents=True)
    storage = TaskStorage(run_dir)
    storage.save_task_state(
        PaperTaskState(
            task_id=task_id,
            run_dir=str(run_dir),
            status="completed",
            max_iterations=1,
            score=0.9,
            baseline=0.6,
            node_count=1,
        )
    )

    provider = PaperArtifactProviderImpl()

    async def fake_run(request: ArtifactEngineRequest, on_event=None):
        assert request.task_id == task_id
        await record_model_usage(
            model="paper-model",
            call_id="paper-call-1",
            usage={"input_tokens": 123, "output_tokens": 45, "cache_read_tokens": 7},
        )
        return SimpleNamespace(task_id=task_id, status="completed")

    provider.run = fake_run  # type: ignore[method-assign]
    adapter = ArtifactEngineAdapter("PAPER", provider, tasks_root=tasks_root)
    request = ArtifactEngineRequest(
        task_id=task_id,
        run_dir=str(run_dir),
        artifact_path=None,
        model=object(),
        max_iterations=1,
        optimization_instruction="improve the paper",
    )

    asyncio.run(adapter.run(request))

    state = adapter.read_state(task_id)
    report = adapter.read_report(task_id)
    assert state.status == "completed"
    assert state.usage is not None
    assert state.usage.tokens.input == 123
    assert state.usage.tokens.output == 45
    assert state.usage.tokens.cache_hit == 7
    assert state.usage.call_count == 1
    assert report.usage is not None
    assert report.usage.tokens.input == 123
