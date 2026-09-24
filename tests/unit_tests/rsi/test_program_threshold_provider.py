from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.rsi.events import EventNode

from jiuwenswarm.agents.harness.common.rsi.program_threshold_provider import (
    ThresholdStoppingProgramProvider,
)
from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters


def _node(score: float, *, adopted: bool = True) -> EventNode:
    return EventNode(
        node=SimpleNamespace(
            type="adopted" if adopted else "rejected", adopted=adopted, score=score
        )
    )


def test_real_program_adapter_uses_threshold_stopping_provider(tmp_path: Path) -> None:
    adapters = build_rsi_adapters(tmp_path, artifact_adapters={"ARTIFACT:PAPER": object()})
    assert isinstance(adapters["ARTIFACT:PROGRAM"].provider, ThresholdStoppingProgramProvider)


@pytest.mark.asyncio
async def test_solved_candidate_stops_active_search_and_keeps_completed_status(tmp_path: Path) -> None:
    (tmp_path / "scorecard.json").write_text(
        json.dumps({"scorecard": {"solvedThreshold": 0.9}}), encoding="utf-8"
    )
    request = SimpleNamespace(task_id="solved", run_dir=tmp_path)
    provider = ThresholdStoppingProgramProvider()
    stop = threading.Event()
    state = SimpleNamespace(stopped_status="terminated")
    provider._stopping[request.task_id] = stop
    provider._live[request.task_id] = state
    forwarded: list[EventNode] = []

    async def collect(event: EventNode) -> None:
        forwarded.append(event)

    async def run(_request: object, *, on_event: object) -> str:
        await on_event(_node(0.8))
        assert not stop.is_set()
        await on_event(_node(0.9, adopted=False))
        assert not stop.is_set()
        await on_event(_node(0.9))
        assert stop.is_set()
        assert state.stopped_status == "completed"
        return "done"

    assert await provider._with_threshold_stop(request, collect, run) == "done"
    assert [event.node.score for event in forwarded] == [0.8, 0.9, 0.9]


@pytest.mark.asyncio
async def test_threshold_stop_preserves_user_termination_and_other_tasks(tmp_path: Path) -> None:
    (tmp_path / "scorecard.json").write_text(json.dumps({}), encoding="utf-8")
    request = SimpleNamespace(task_id="target", run_dir=tmp_path)
    provider = ThresholdStoppingProgramProvider()
    stopped = threading.Event()
    stopped.set()
    state = SimpleNamespace(stopped_status="terminated")
    provider._stopping[request.task_id] = stopped
    provider._live[request.task_id] = state
    other_stop = threading.Event()
    provider._stopping["other"] = other_stop
    provider._live["other"] = SimpleNamespace(stopped_status="terminated")

    async def run(_request: object, *, on_event: object) -> None:
        await on_event(_node(1.0))

    await provider._with_threshold_stop(request, None, run)
    assert state.stopped_status == "terminated"
    assert not other_stop.is_set()
