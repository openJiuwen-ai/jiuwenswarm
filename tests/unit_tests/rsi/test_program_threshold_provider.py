from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.rsi.events import EventNode
from openjiuwen.rsi.schema import RsiTreeNode as ProviderTreeNode

from jiuwenswarm.agents.harness.common.rsi.models import RsiTreeNode
from jiuwenswarm.agents.harness.common.rsi.program_threshold_provider import (
    ThresholdStoppingProgramProvider,
)
from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from jiuwenswarm.agents.harness.common.rsi.provider_factory import build_rsi_adapters


def _node(score: float, *, adopted: bool = True) -> EventNode:
    return EventNode(
        node=SimpleNamespace(
            type="adopted" if adopted else "rejected", adopted=adopted, score=score
        )
    )


def _program_node(node_id: str, *, score: float | None, node_type: str,
                  failure_class: str | None = None) -> EventNode:
    return EventNode(node=ProviderTreeNode(
        node_id=node_id,
        iteration=int(node_id.rsplit(":", 1)[-1]),
        parent_id="root",
        type=node_type,
        adopted=node_type == "adopted",
        score=score,
        summary=None,
        snapshot_artifact_id=None,
        reason="the model returned an empty reply" if failure_class else None,
        failure_class=failure_class,
        changes=[],
        extra={"program": {"logical_kind": node_type, "error": failure_class}},
    ))


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


@pytest.mark.asyncio
async def test_empty_model_waits_after_solve_are_stopped_not_failed(tmp_path: Path) -> None:
    (tmp_path / "scorecard.json").write_text(
        json.dumps({"scorecard": {"solvedThreshold": 0.9}}), encoding="utf-8"
    )
    request = SimpleNamespace(task_id="solved", run_dir=tmp_path)
    provider = ThresholdStoppingProgramProvider()
    stop = threading.Event()
    provider._stopping[request.task_id] = stop
    provider._live[request.task_id] = SimpleNamespace(stopped_status=None)
    forwarded: list[EventNode] = []

    async def collect(event: EventNode) -> None:
        forwarded.append(event)

    async def run(_request: object, *, on_event: object) -> None:
        await on_event(_program_node("attempt:1", score=None, node_type="rejected",
                                     failure_class="empty_reply"))
        await on_event(_program_node("attempt:2", score=1.0, node_type="adopted"))
        await on_event(_program_node("attempt:3", score=None, node_type="candidate",
                                     failure_class="empty_reply"))
        await on_event(_program_node("attempt:3", score=None, node_type="rejected",
                                     failure_class="empty_reply"))
        await on_event(_program_node("attempt:4", score=0.5, node_type="rejected"))

    await provider._with_threshold_stop(request, collect, run)
    assert stop.is_set()
    assert [event.node.type for event in forwarded] == [
        "rejected", "adopted", "pruned", "pruned", "rejected",
    ]
    stopped = forwarded[3].node
    assert stopped.failure_class is None
    assert stopped.extra["threshold_stop_cancelled"] is True
    assert stopped.extra["program"]["error"] is None


def test_stopped_verdict_survives_provider_tree_refresh() -> None:
    stopped = RsiTreeNode(
        node_id="attempt:3", iteration=3, parent_id="ROOT", type="PRUNED",
        adopted=False, score=None, description="已达标，停止此候选",
        failure_reason="其他候选已达到目标分数",
        extra={"threshold_stop_cancelled": True},
    )
    provider = RsiTreeNode(
        node_id="attempt:3", iteration=3, parent_id="ROOT", type="REJECTED",
        adopted=False, score=None, description="did not run",
        failure_reason="the model returned an empty reply",
        failure_class="empty_reply",
    )
    assert RsiProjector._merge_node(stopped, provider) is stopped


def test_completed_legacy_run_reclassifies_only_empty_replies_after_solve(tmp_path: Path) -> None:
    task_id = "legacy"
    task_dir = tmp_path / task_id
    (task_dir / "run").mkdir(parents=True)
    (task_dir / "run" / "scorecard.json").write_text(
        json.dumps({"scorecard": {"solvedThreshold": 0.9}}), encoding="utf-8"
    )
    sequence = [
        {"node_id": "attempt:1", "type": "rejected", "failure_class": "empty_reply", "score": None},
        {"node_id": "attempt:2", "type": "adopted", "adopted": True, "score": 1.0},
        {"node_id": "attempt:3", "type": "rejected", "failure_class": "empty_reply", "score": None},
    ]
    (task_dir / "events.jsonl").write_text("".join(
        json.dumps({"event_type": "node", "event": {"node": node}}) + "\n"
        for node in sequence
    ), encoding="utf-8")
    projector = RsiProjector(tmp_path)
    projector.register_root(task_id)
    for node in sequence:
        projector.on_provider_node(task_id, _program_node(
            node["node_id"], score=node["score"], node_type=node["type"],
            failure_class=node.get("failure_class"),
        ).node)

    recovered = RsiProjector(tmp_path)
    assert recovered.load_from_disk(task_id)
    assert recovered.reconcile_program_threshold_stops(task_id) == 1
    tree = recovered.derive_tree(task_id)
    by_id = {node["node_id"]: node for node in tree["nodes"]}
    assert by_id["attempt:1"]["type"] == "REJECTED"
    assert by_id["attempt:3"]["type"] == "PRUNED"
    assert by_id["attempt:3"]["extra"]["threshold_stop_cancelled"] is True
    assert recovered.reconcile_program_threshold_stops(task_id) == 0
