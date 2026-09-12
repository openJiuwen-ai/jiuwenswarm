# -*- coding: utf-8 -*-
"""RSI 事件链路/投影/用量单测（内部 v3 §4.3/§4.4/§4.6）。"""

import json
from pathlib import Path

import pytest
from openjiuwen.rsi.events import EventStatus, EventUsage
from openjiuwen.rsi.schema import RsiModelCall as EngineRsiModelCall
from openjiuwen.rsi.schema import RsiUsageTokens

from jiuwenswarm.agents.harness.common.rsi.artifact_service import RsiArtifactService
from jiuwenswarm.agents.harness.common.rsi.event_consumer import RsiEventConsumer
from jiuwenswarm.agents.harness.common.rsi.events import EngineEvent
from jiuwenswarm.agents.harness.common.rsi.models import RsiArtifactPath, RsiModelCall, Tokens
from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from jiuwenswarm.agents.harness.common.rsi.usage_recorder import RsiUsageRecorder


@pytest.fixture
def projector(tmp_path: Path):
    return RsiProjector(tmp_path)


@pytest.fixture
def usage():
    return RsiUsageRecorder()


@pytest.fixture
def artifacts(tmp_path: Path):
    return RsiArtifactService(tmp_path)


def _metric_event(iteration: int, score: float, baseline: float) -> EngineEvent:
    return EngineEvent(
        family="progress", kind="metric", task_id="rsi-t1",
        payload={"iteration": iteration, "total_iterations": 3, "score": score, "baseline": baseline},
    )


class TestProjectorProgress:
    def test_derive_progress(self, projector):
        projector.register_root("rsi-t1", baseline=0.5)
        projector.on_progress_metric("rsi-t1", {"iteration": 2, "total_iterations": 3, "score": 0.9, "baseline": 0.5})
        progress = projector.derive_progress("rsi-t1")
        assert progress["iteration"] == 2
        assert progress["total_iterations"] == 3
        assert progress["score"] == 0.9

    def test_node_created(self, projector):
        projector.register_root("rsi-t1")
        node = projector.on_node_created("rsi-t1", {
            "node": {"ref": "cand_1", "parent_ref": "root", "outcome": "ADOPTED", "accepted": True,
                     "score": 0.8, "summary": "优化 prompt"},
        })
        assert node is not None
        assert node.node_id == "N1"
        assert node.parent_id == "ROOT"
        assert node.type == "ADOPTED"
        assert node.adopted is True
        tree = projector.derive_tree("rsi-t1")
        assert len(tree["nodes"]) == 2
        assert tree["iteration"] == 0  # metric 未更新

    def test_node_stage_updates_description(self, projector):
        projector.register_root("rsi-t1")
        node = projector.on_node_created("rsi-t1", {
            "node": {"ref": "c1", "parent_ref": "root", "outcome": "ADOPTED", "accepted": True,
                     "score": 0.8, "summary": "优化"},
        })
        projector.on_node_stage("rsi-t1", {"node_ref": "c1", "stage": {"id": "verify", "name": "验证中"}})
        assert node is not None
        # 统一动态阶段语义：description 覆盖为当前阶段文案，而不是追加历史。
        assert node.description == "验证中"
        assert node.extra["stage"]["name"] == "验证中"

    def test_harness_h0_node_normalizes_to_root(self, projector):
        projector.register_root("rsi-t1")
        node = projector.on_provider_node("rsi-t1", {
            "node_id": "h0",
            "iteration": 0,
            "parent_id": None,
            "type": "ROOT",
            "adopted": True,
            "summary": "Initial Harness",
            "extra": {},
        })

        assert node is not None
        assert node.node_id == "ROOT"
        tree = projector.derive_tree("rsi-t1")
        assert len(tree["nodes"]) == 1

    def test_tree_persist_reload(self, projector, tmp_path):
        projector.register_root("rsi-t1")
        projector.on_node_created("rsi-t1", {
            "node": {"ref": "c1", "outcome": "REJECTED", "accepted": False, "score": 0.3},
        })
        reloaded = RsiProjector(tmp_path)
        reloaded.load_from_disk("rsi-t1")
        tree = reloaded.derive_tree("rsi-t1")
        assert len(tree["nodes"]) == 2


class TestUsageRecorder:
    def test_record_and_get(self, usage):
        usage.record("rsi-t1", "N1", RsiModelCall(model="m1", call_count=1, tokens=Tokens(input=10, output=5)))
        usage.record("rsi-t1", "N1", RsiModelCall(model="m1", call_count=1, tokens=Tokens(input=5, output=2)))
        data = usage.get("rsi-t1")
        assert data["usage"]["tokens"]["input"] == 15
        assert data["usage"]["tokens"]["output"] == 7
        assert data["usage"]["call_count"] == 2
        assert data["per_iteration"][0]["iteration"] == 1
        assert "N1" in data["usage_by_node"]

    def test_from_event(self, usage):
        usage.record_engine_event("rsi-t1", {
            "node_ref": "N1",
            "model_call": {"model": "m2", "call_count": 1,
                           "tokens": {"input": 100, "output": 20, "cache_hit": 3}},
        })
        data = usage.get("rsi-t1")
        assert data["usage"]["tokens"]["input"] == 100
        assert data["usage"]["tokens"]["cache_hit"] == 3

    def test_task_not_found(self, usage):
        with pytest.raises(Exception):
            usage.get("rsi-ghost")


class TestArtifactService:
    def test_make_snapshot_and_locate(self, tmp_path):
        service = RsiArtifactService(tmp_path)
        task_dir = tmp_path / "rsi-t1"
        task_dir.mkdir()
        asset = task_dir / "optimized.txt"
        asset.write_text("content", encoding="utf-8")
        artifact_id = service.make_snapshot("rsi-t1", "cand_1", "N1", [
            RsiArtifactPath(role="PRIMARY", path=str(asset), format="txt"),
        ])
        assert artifact_id == "AN1"
        located = service.locate("rsi-t1", "AN1")
        assert Path(located.path).parts[-2:] == ("snapshots", "AN1.zip")
        assert located.kind == "harness_plugin"
        best = service.locate("rsi-t1", None)
        assert best.is_best is True

    def test_locate_missing(self, tmp_path):
        service = RsiArtifactService(tmp_path)
        (tmp_path / "rsi-t1").mkdir()
        with pytest.raises(Exception):
            service.locate("rsi-t1", None)


class TestEventConsumer:
    def test_engine_events_are_appended_to_task_jsonl(
        self, projector, usage, artifacts, tmp_path
    ):
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        metric = EngineEvent(
            family="progress",
            kind="metric",
            task_id="engine-task-id",
            event_id=7,
            ts="2026-09-06T01:00:00+00:00",
            payload={
                "iteration": 1,
                "total_iterations": 3,
                "score": 0.9,
                "baseline": 0.5,
            },
        )

        import asyncio

        asyncio.run(consumer.on_engine_event(metric))
        asyncio.run(consumer.on_engine_event(EventStatus(status="running")))

        records = [
            json.loads(line)
            for line in (tmp_path / "rsi-t1" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [record["event_type"] for record in records] == [
            "progress.metric",
            "status",
        ]
        assert records[0]["task_id"] == "rsi-t1"
        assert records[0]["event"]["task_id"] == "engine-task-id"
        assert records[0]["event"]["event_id"] == 7
        assert records[1]["event"]["status"] == "running"
        assert all(record["schema_version"] == 1 for record in records)
        assert all(record["recorded_at"] for record in records)

    def test_metric_event_no_push(self, projector, usage, artifacts):
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        pushed = []
        async def on_progress(task_id, payload):
            pushed.append(payload)
        consumer.bind_push(on_progress=on_progress)
        import asyncio
        asyncio.run(consumer.on_engine_event(_metric_event(1, 0.9, 0.5)))
        assert len(pushed) == 1
        assert pushed[0]["iteration"] == 1

    def test_usage_event(self, projector, usage, artifacts):
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        event = EngineEvent(family="progress", kind="usage", task_id="rsi-t1", payload={
            "node_ref": "N1",
            "model_call": {"model": "m", "call_count": 1, "tokens": {"input": 3, "output": 1, "cache_hit": 0}},
        })
        import asyncio
        asyncio.run(consumer.on_engine_event(event))
        data = usage.get("rsi-t1")
        assert data["usage"]["tokens"]["input"] == 3

    def test_agent_core_usage_dataclass_event(self, projector, usage, artifacts):
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        event = EventUsage(
            event_id=1,
            task_id="rsi-t1",
            ts="2026-09-07T01:00:00+00:00",
            call_id="call-1",
            model_call=EngineRsiModelCall(
                model="m",
                call_count=1,
                tokens=RsiUsageTokens(input=7, output=3, cache_hit=2),
            ),
            node_ref="epoch-001",
        )
        import asyncio

        asyncio.run(consumer.on_engine_event(event))
        data = usage.get("rsi-t1")
        assert data["usage"]["tokens"]["input"] == 7
        assert data["usage"]["tokens"]["output"] == 3
        assert data["usage"]["tokens"]["cache_hit"] == 2

    def test_agent_core_usage_dataclass_event_pushes_live_usage(self, projector, usage, artifacts):
        projector.register_root("rsi-t1", baseline=0.5)
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        pushed = []

        async def on_progress(task_id, payload):
            pushed.append((task_id, payload))

        consumer.bind_push(on_progress=on_progress)
        event = EventUsage(
            event_id=1,
            task_id="rsi-t1",
            ts="2026-09-07T01:00:00+00:00",
            call_id="call-live-1",
            model_call=EngineRsiModelCall(
                model="m",
                call_count=1,
                tokens=RsiUsageTokens(input=7, output=3, cache_hit=2),
            ),
            node_ref="epoch-001",
        )

        import asyncio

        asyncio.run(consumer.on_engine_event(event))
        assert pushed
        assert pushed[-1][0] == "rsi-t1"
        assert pushed[-1][1]["usage"]["tokens"] == {"input": 7, "output": 3, "cache_hit": 2}

    def test_node_created_with_artifacts(self, projector, usage, artifacts, tmp_path):
        task_dir = tmp_path / "rsi-t1"
        task_dir.mkdir()
        asset = task_dir / "out.txt"
        asset.write_text("x", encoding="utf-8")
        consumer = RsiEventConsumer("rsi-t1", usage, projector, artifacts)
        pushed = []
        async def on_tree(task_id, payload):
            pushed.append(payload)
        consumer.bind_push(on_tree_delta=on_tree)
        event = EngineEvent(family="node", kind="created", task_id="rsi-t1", payload={
            "node": {"ref": "c1", "outcome": "ADOPTED", "accepted": True, "score": 0.9},
            "artifacts": [{"role": "PRIMARY", "path": str(asset), "format": "txt"}],
        })
        import asyncio
        asyncio.run(consumer.on_engine_event(event))
        assert len(pushed) == 1
        node = pushed[0]["nodes"][0]
        assert node["snapshot_artifact_id"] == "AN1"
