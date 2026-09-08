"""生产 HarnessProvider 单元测试（设计 §2.1/§2.2 归一化与校验闭环）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiNotReady,
    RsiPathNotAllowed,
    RsiResumeInputChanged,
    RsiResumeMismatch,
)
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineRequest
from jiuwenswarm.agents.harness.common.rsi.harness_provider import HarnessProvider
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer


def _request(tmp_path: Path, *, resume: bool = False) -> HarnessEngineRequest:
    return HarnessEngineRequest(
        task_id="rsi-test0001",
        dataset_files=(str(tmp_path / "cases.json"),),
        harness_refs_path=str(tmp_path / "refs" / "harness_refs.yaml"),
        output_dir=str(tmp_path / "rsi-test0001" / "run"),
        dataset_id="single_harness_benchmark",
        max_iterations=3,
        search_width=1,
        model_refs={},
        resume=resume,
    )


class _StubOrchestrator:
    """记录 engine request 并按状态文件收敛的最小编排器替身。"""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.requests: list[Any] = []
        self.on_event_seen: list[Any] = []

    async def run(self, request: Any, *, on_event: Any = None) -> None:
        self.requests.append(request)
        self.on_event_seen.append(on_event)
        self.state["status"] = "completed"
        state_path = Path(request.output_dir) / "single_harness_state.yaml"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.state, fh, allow_unicode=True)


def _write_state(tasks_root: Path, task_id: str, state: dict[str, Any]) -> None:
    task_dir = tasks_root / task_id / "run"
    task_dir.mkdir(parents=True, exist_ok=True)
    with open(task_dir / "single_harness_state.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump(state, fh, allow_unicode=True)


def _write_baseline_summary(
    tasks_root: Path,
    task_id: str,
    summary: dict[str, Any],
) -> None:
    summary_path = (
        tasks_root
        / task_id
        / "run"
        / "evaluations"
        / "e001"
        / "b001"
        / "source"
        / "summary.json"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")


def _state_dict() -> dict[str, Any]:
    return {
        "status": "completed",
        "best_score": 0.8,
        "baseline_score": 0.5,
        "best_harness_refs_path": "/tmp/refs.yaml",
        "published_harness_refs_path": "/tmp/published.yaml",
        "candidate_gates": [
            {
                "candidate_id": "C1",
                "status": "accepted",
                "accepted": True,
                "reason": "candidate_passed_batch_gate",
                "candidate_score": 0.8,
                "before_harness_refs_path": "/tmp/source.yaml",
                "candidate_harness_refs_path": "/tmp/refs.yaml",
            },
            {
                "candidate_id": "C2",
                "status": "rejected",
                "accepted": False,
                "reason": "score_regression",
                "candidate_score": 0.3,
                "before_harness_refs_path": "/tmp/refs.yaml",
                "candidate_harness_refs_path": "/tmp/c2.yaml",
            },
        ],
    }


def test_validate_input_real_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "cases.json"
    dataset.write_text(
        json.dumps(
            [
                {"case_id": "a", "question": "q1"},
                {"case_id": "b", "question": "q2"},
            ]
        ),
        encoding="utf-8",
    )
    provider = HarnessProvider(tmp_path)
    result = provider.validate_input(str(dataset))
    assert result == {"valid": True, "sample_count": 2, "errors": []}


def test_validate_input_missing_path(tmp_path: Path) -> None:
    provider = HarnessProvider(tmp_path)
    result = provider.validate_input(str(tmp_path / "missing.json"))
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "PATH_INVALID"


def test_validate_input_bad_case_shape(tmp_path: Path) -> None:
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    provider = HarnessProvider(tmp_path)
    result = provider.validate_input(str(dataset))
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "DATASET_INVALID"
    assert result["sample_count"] is None


def test_validate_input_duplicate_case_ids(tmp_path: Path) -> None:
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps([{"case_id": "a"}, {"case_id": "a"}]), encoding="utf-8")
    result = HarnessProvider(tmp_path).validate_input(str(dataset))
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "DATASET_INVALID"


def test_validate_input_requires_path() -> None:
    result = HarnessProvider(Path(".")).validate_input(None)
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "DATASET_REQUIRED"


def test_run_translates_request_and_result(tmp_path: Path) -> None:
    state = _state_dict()
    stub = _StubOrchestrator(state)
    provider = HarnessProvider(tmp_path, orchestrator=stub)
    events: list[Any] = []

    async def _sink(event: Any) -> None:
        events.append(event)

    import asyncio

    result = asyncio.run(provider.run(_request(tmp_path), on_event=_sink))
    assert result.status == "completed"
    assert result.final_node_id == "C1"
    assert len(stub.requests) == 1
    engine_request = stub.requests[0]
    assert engine_request.dataset_files == [str(tmp_path / "cases.json")]
    assert engine_request.resume is False
    assert engine_request.auto_full_baseline is True
    assert len(stub.on_event_seen) == 1 and callable(stub.on_event_seen[0])


@pytest.mark.parametrize("fingerprint", [{}, {"auto_full_baseline": False}, {"auto_full_baseline": True}])
def test_resume_preserves_baseline_protocol(tmp_path: Path, fingerprint: dict) -> None:
    import asyncio

    state = {**_state_dict(), "fingerprint": fingerprint}
    _write_state(tmp_path, "rsi-test0001", state)
    stub = _StubOrchestrator(state)
    asyncio.run(HarnessProvider(tmp_path, orchestrator=stub).resume(_request(tmp_path, resume=True)))
    assert stub.requests[0].auto_full_baseline is fingerprint.get("auto_full_baseline", False)


def test_baseline_progress_does_not_consume_an_epoch(tmp_path: Path) -> None:
    _write_state(tmp_path, "rsi-test0001", {
        **_state_dict(), "max_iteration": 5, "epoch_checkpoints": [], "status": "running",
    })
    state = HarnessProvider(tmp_path).read_state("rsi-test0001")
    assert state.iteration == 0
    assert state.total_iterations == 5
    assert state.baseline == 0.5


def test_rejected_epoch_parent_is_consistent_in_push_query_and_recovery(tmp_path: Path) -> None:
    from dataclasses import replace

    from openjiuwen.rsi.harness_rsi.single_harness.events_translate import (
        active_epoch_node_event,
        epoch_node_event,
        root_node_event,
    )

    from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector

    task_id = "epoch-parent-task"
    checkpoint = {"epoch": 1, "status": "rejected", "promotion_applied": False, "score": 0.6,
                  "before_harness_refs_path": "h0.yaml", "harness_refs_path": "h0.yaml",
                  "selected_harness_refs_path": "h0.yaml"}
    state = {**_state_dict(), "source_harness_refs_path": "h0.yaml", "best_harness_refs_path": "h0.yaml",
             "baseline_score": 0.6, "best_score": 0.6, "epoch_checkpoints": [checkpoint],
             "active_epoch": 2, "active_epoch_before_harness_refs_path": "h0.yaml"}
    _write_state(tmp_path, task_id, state)
    active = active_epoch_node_event(state).node
    assert active.parent_id == "h0"
    projector = RsiProjector(tmp_path)
    for node in (root_node_event(state).node, epoch_node_event(state, checkpoint).node, active):
        projector.on_provider_node(task_id, node)
    assert projector.derive_tree(task_id)["nodes"][-1]["parent_id"] == "ROOT"

    # An older persisted tree must not override the corrected engine lineage.
    projector.on_provider_node(task_id, replace(active, parent_id="epoch-001"))
    recovered = RsiProjector(tmp_path)
    recovered.load_from_disk(task_id)
    provider = HarnessProvider(tmp_path)
    tree = recovered.merge_provider_tree(task_id, provider.get_tree(task_id))
    nodes = {node["node_id"]: node for node in tree["nodes"]}
    assert nodes["epoch-001"]["parent_id"] == "ROOT"
    assert nodes["epoch-002"]["parent_id"] == "ROOT"
    assert nodes["epoch-001"]["score"] == 0.6
    assert not nodes["epoch-001"]["adopted"]
    assert nodes["epoch-002"]["score"] is None
    assert nodes["epoch-002"]["failure_reason"] is None
    assert provider.read_state(task_id).best_node_id == "h0"
    assert "score_comparison" not in nodes["epoch-002"]["extra"]


@pytest.mark.asyncio
async def test_epoch_push_query_recovery_and_plugin_artifacts_are_consistent(tmp_path: Path):
    import zipfile

    from openjiuwen.rsi.events import NodeStageEvent
    from openjiuwen.rsi.harness_rsi.single_harness.events_translate import (
        active_epoch_node_event,
        epoch_node_event,
        root_node_event,
        source_reuse_stage_payload,
    )

    from jiuwenswarm.agents.harness.common.rsi.artifact_service import (
        RsiArtifactService,
    )
    from jiuwenswarm.agents.harness.common.rsi.event_consumer import RsiEventConsumer
    from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
    from jiuwenswarm.agents.harness.common.rsi.usage_recorder import RsiUsageRecorder

    task_id = "epoch-task"
    package = tmp_path / task_id / "candidate"
    skill = package / "skills" / "verification" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Verify before delivery.", encoding="utf-8")
    (package / "harness_config.yaml").write_text("id: candidate\n", encoding="utf-8")
    refs = tmp_path / task_id / "refs.yaml"
    refs.write_text(yaml.safe_dump({"harness_refs": {"solver": str(package)}}), encoding="utf-8")
    state = {**_state_dict(), "max_iteration": 3, "epoch_checkpoints": [], "active_epoch": 1,
             "active_epoch_before_harness_refs_path": str(refs), "source_harness_refs_path": str(refs),
             "best_harness_refs_path": str(refs)}
    projector = RsiProjector(tmp_path)
    projector.register_root(task_id)
    artifacts = RsiArtifactService(tmp_path)
    consumer = RsiEventConsumer(task_id, RsiUsageRecorder(), projector, artifacts)
    await consumer.on_engine_event(root_node_event(state))
    await consumer.on_engine_event(active_epoch_node_event(state))
    await consumer.on_engine_event(NodeStageEvent("epoch-001", {"name": "Analyzing failed cases"}))
    nodes = projector.derive_tree(task_id)["nodes"]
    assert len(nodes) == 2
    assert nodes[0]["description"] == "Initial Harness"
    assert nodes[1]["description"] == "Analyzing failed cases"

    provenance = {
        "reused_case_ids": ["a", "b"], "evaluated_case_ids": [],
        "evaluations": [{"eval_ref_path": "/h0/eval_ref.yaml", "case_ids": ["a", "b"]}],
    }
    stage = source_reuse_stage_payload(batch_index=1, total_cases=2, score=0.25,
                                      eval_ref_path="/source/eval_ref.yaml", provenance=provenance)
    await consumer.on_engine_event(NodeStageEvent("epoch-001", stage))
    reused_tree = projector.derive_tree(task_id)
    assert len(reused_tree["nodes"]) == 2
    assert reused_tree["nodes"][1]["extra"]["stage"] == stage
    assert reused_tree["nodes"][1]["score"] is None
    state["completed_batches"] = {
        "epoch_001:batch_001": {"epoch": 1, "batch_index": 1, "source_eval_ref_path": "/source/eval_ref.yaml",
                                 "source_evidence": provenance},
    }

    checkpoint = {"epoch": 1, "before_harness_refs_path": str(refs), "harness_refs_path": str(refs),
                  "selected_harness_refs_path": str(refs), "score": 1.0, "promotion_applied": True}
    state.update(epoch_checkpoints=[checkpoint], active_epoch=2, best_score=1.0)
    event = epoch_node_event(state, checkpoint)
    assert any(item["role"] == "PRIMARY" and item["path"] == str(package) for item in event.artifacts)
    await consumer.on_engine_event(event)
    await consumer.on_engine_event(active_epoch_node_event(state))
    # Simulate a persisted tree from the older candidate-based query adapter.
    projector.on_node_created(task_id, {"node": {"ref": "legacy-candidate", "score": 0.0}})
    projector.on_progress_metric(task_id, {"iteration": 99})
    _write_state(tmp_path, task_id, state)
    provider = HarnessProvider(tmp_path)
    for view in (projector, RsiProjector(tmp_path)):
        view.load_from_disk(task_id)
        tree = view.merge_provider_tree(task_id, provider.get_tree(task_id))
        assert [node["node_id"] for node in tree["nodes"]] == ["ROOT", "epoch-001", "epoch-002"]
        assert tree["iteration"] == 1
        assert tree["nodes"][1]["snapshot_artifact_id"] == "Aepoch-001"
        assert tree["nodes"][1]["extra"]["source_evidence"][0]["reused_case_ids"] == ["a", "b"]
    assert provider.read_state(task_id).best_node_id == "epoch-001"
    assert provider.read_report(task_id).best_node_id == "epoch-001"
    assert provider._result_from_state(task_id).final_node_id == "epoch-001"
    assert {item.node_id for item in provider.read_report(task_id).artifact_index} == {"h0", "epoch-001"}
    snapshot = artifacts.locate(task_id, "Aepoch-001")
    with zipfile.ZipFile(snapshot.path) as archive:
        assert "PRIMARY_candidate/skills/verification/SKILL.md" in archive.namelist()
        assert "PRIMARY_candidate/harness_config.yaml" in archive.namelist()
    # Replaying H0 must not make it newer than the adopted epoch artifact.
    await consumer.on_engine_event(root_node_event(state))
    assert artifacts.best_artifact(task_id)["artifact_id"] == "Aepoch-001"


@pytest.mark.parametrize("baseline_score", [0.0, 0.5])
def test_real_engine_baseline_reaches_push_report_tree_and_resume(tmp_path: Path, baseline_score: float) -> None:
    import asyncio
    from types import SimpleNamespace

    from openjiuwen.rsi import (
        AutoCoordinatingHarnessConfig,
        SingleHarnessIterativeOptimizationOrchestrator,
    )
    from openjiuwen.rsi.harness_rsi.config import DataLoaderConfig, EvaluatorConfig

    from jiuwenswarm.agents.harness.common.rsi.artifact_service import (
        RsiArtifactService,
    )
    from jiuwenswarm.agents.harness.common.rsi.event_consumer import RsiEventConsumer
    from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
    from jiuwenswarm.agents.harness.common.rsi.services import RsiReportService
    from jiuwenswarm.agents.harness.common.rsi.usage_recorder import RsiUsageRecorder

    request = _request(tmp_path)
    Path(request.dataset_files[0]).write_text(json.dumps({"cases": [
        {"case_id": "one", "input": "first"}, {"case_id": "two", "input": "second"},
    ]}), encoding="utf-8")
    refs_path = Path(request.harness_refs_path)
    refs_path.parent.mkdir()
    refs_path.write_text("harness_refs:\n  solver: baseline\n", encoding="utf-8")
    projector = RsiProjector(tmp_path)
    projector.register_root(request.task_id)
    usage = RsiUsageRecorder()
    artifacts = RsiArtifactService(tmp_path)
    consumer = RsiEventConsumer(request.task_id, usage, projector, artifacts)
    pushes = []

    async def on_progress(task_id, payload):
        pushes.append(payload)

    consumer.bind_push(on_progress=on_progress)
    baseline_calls = []

    class Evaluator:
        async def evaluate_batch(self, **kwargs):
            output = Path(kwargs["output_dir"])
            if output.name != "frozen_baseline":
                assert provider.read_state(request.task_id).baseline == baseline_score
                raise RuntimeError("optimization-batch-reached")
            baseline_calls.append(kwargs)
            assert [case["case_id"] for case in kwargs["cases"]] == ["one", "two"]
            assert provider.read_state(request.task_id).baseline is None
            await kwargs["on_case_stage"]({"case_index": 1, "total_cases": 2, "status": "running"})
            nodes = projector.derive_tree(request.task_id)["nodes"]
            assert len(nodes) == 1 and nodes[0]["node_id"] == "ROOT"
            assert nodes[0]["extra"]["stage"]["total_cases"] == 2
            output.mkdir(parents=True, exist_ok=True)
            result = output / "result.json"
            result.write_text("{}", encoding="utf-8")
            cases = [{"case_id": name, "score": score, "status": "passed" if score == 1 else "failed",
                      "result_path": str(result), "trace_path": str(result)}
                     for name, score in (("one", baseline_score * 2), ("two", 0.0))]
            eval_ref = output / "eval_ref.yaml"
            eval_ref.write_text(yaml.safe_dump({"harness_refs_path": str(refs_path), "cases": cases}), encoding="utf-8")
            return str(eval_ref)

    orchestrator = SingleHarnessIterativeOptimizationOrchestrator(
        AutoCoordinatingHarnessConfig(max_epochs=3, evaluator=EvaluatorConfig(backend="single_harness"),
                                      data_loader=DataLoaderConfig(batch_size=1)),
        evaluator=Evaluator(), analyzer=object(), member_optimizer=object(),
    )
    provider = HarnessProvider(tmp_path, orchestrator=orchestrator)
    with pytest.raises(RuntimeError, match="optimization-batch-reached"):
        asyncio.run(provider.run(request, on_event=consumer.on_engine_event))
    assert len(baseline_calls) == 1
    assert pushes[-1]["progress"]["baseline"] == baseline_score
    assert pushes[-1]["progress"]["iteration"] == 0
    assert pushes[-1]["progress"]["total_iterations"] == 3
    assert provider.get_tree(request.task_id).nodes[0].score == baseline_score

    report_service = RsiReportService(SimpleNamespace(get=lambda _: None), projector, usage, artifacts)
    report = report_service.get({"task_id": request.task_id}, adapter=provider)
    assert report["baseline"] == baseline_score
    assert report["best_score"] == baseline_score
    reloaded = RsiProjector(tmp_path)
    reloaded.load_from_disk(request.task_id)
    assert reloaded.derive_progress(request.task_id)["baseline"] == baseline_score
    assert reloaded.derive_tree(request.task_id)["nodes"][0]["score"] == baseline_score

    with pytest.raises(RuntimeError, match="optimization-batch-reached"):
        asyncio.run(provider.resume(request, on_event=consumer.on_engine_event))
    assert len(baseline_calls) == 1
    assert provider.read_state(request.task_id).baseline == baseline_score


def test_resume_fingerprint_conflict_maps_to_resume_mismatch(tmp_path: Path) -> None:
    class _Conflicting:
        async def run(self, request: Any, *, on_event: Any = None) -> None:
            raise ValueError("resume inputs do not match single-harness state")

    provider = HarnessProvider(tmp_path, orchestrator=_Conflicting())
    import asyncio

    with pytest.raises(RsiResumeMismatch):
        asyncio.run(provider.resume(_request(tmp_path, resume=True)))


def test_pause_and_terminate_report_capability_boundary(tmp_path: Path) -> None:
    provider = HarnessProvider(tmp_path)
    assert provider.supports_pause is False
    import asyncio

    with pytest.raises(RsiNotReady):
        asyncio.run(provider.pause("rsi-test0001"))
    with pytest.raises(RsiNotReady):
        asyncio.run(provider.terminate("rsi-test0001"))


def test_read_state_and_report(tmp_path: Path) -> None:
    _write_state(tmp_path, "rsi-test0001", _state_dict())
    (tmp_path / "rsi-test0001" / "run" / "single_harness_report.yaml").write_text(
        yaml.safe_dump({"status": "completed", "best_score": 0.8, "candidate_gates": []}),
        encoding="utf-8",
    )
    provider = HarnessProvider(tmp_path)
    state = provider.read_state("rsi-test0001")
    assert state.status == "completed"
    assert state.best_node_id == "C1"
    assert state.score == pytest.approx(0.8)
    assert state.baseline == pytest.approx(0.5)
    assert state.iteration == 2
    report = provider.read_report("rsi-test0001")
    assert report.status == "completed"
    assert report.best_node_id == "C1"


def test_get_tree_derives_parent_by_refs_reversal(tmp_path: Path) -> None:
    state = _state_dict()
    state["candidate_gates"][1]["before_harness_refs_path"] = "/tmp/refs.yaml"
    _write_state(tmp_path, "rsi-test0001", state)
    tree = HarnessProvider(tmp_path).get_tree("rsi-test0001")
    node_ids = [node.node_id for node in tree.nodes]
    assert node_ids == ["ROOT", "C1", "C2"]
    assert tree.nodes[0].parent_id is None
    assert tree.nodes[0].score == pytest.approx(0.5)
    assert tree.nodes[1].parent_id == "ROOT"
    assert tree.nodes[2].parent_id == "C1"
    assert tree.nodes[1].adopted is True
    assert tree.nodes[2].adopted is False


def test_get_tree_includes_root_before_first_gate(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        "rsi-test0001",
        {"status": "running", "baseline_score": None, "candidate_gates": []},
    )
    tree = HarnessProvider(tmp_path).get_tree("rsi-test0001")
    assert [node.node_id for node in tree.nodes] == ["ROOT"]
    assert tree.nodes[0].type == "ROOT"
    assert tree.nodes[0].summary == "基线"
    assert tree.depth == 0
    assert tree.iteration == 0


def test_baseline_score_falls_back_to_full_source_summary(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        "rsi-test0001",
        {
            "status": "running",
            "baseline_score": None,
            "dataset": {"cases": 3},
            "candidate_gates": [],
        },
    )
    _write_baseline_summary(
        tmp_path,
        "rsi-test0001",
        {"total_cases": 3, "average_score": 0.6666666666666666},
    )

    provider = HarnessProvider(tmp_path)
    assert (
        provider.read_state("rsi-test0001").baseline
        == pytest.approx(0.6666666666666666)
    )
    assert (
        provider.get_tree("rsi-test0001").nodes[0].score
        == pytest.approx(0.6666666666666666)
    )


def test_baseline_score_prefers_existing_state_value(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        "rsi-test0001",
        {
            "status": "running",
            "baseline_score": 0.5,
            "dataset": {"cases": 3},
            "candidate_gates": [],
        },
    )
    _write_baseline_summary(
        tmp_path,
        "rsi-test0001",
        {"total_cases": 3, "average_score": 0.9},
    )

    assert HarnessProvider(tmp_path).read_state("rsi-test0001").baseline == pytest.approx(0.5)


def test_baseline_score_ignores_partial_batch_summary(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        "rsi-test0001",
        {
            "status": "running",
            "baseline_score": None,
            "dataset": {"cases": 3},
            "candidate_gates": [],
        },
    )
    _write_baseline_summary(
        tmp_path,
        "rsi-test0001",
        {"total_cases": 2, "average_score": 0.9},
    )

    assert HarnessProvider(tmp_path).read_state("rsi-test0001").baseline is None


def test_locate_artifact_by_gate_id(tmp_path: Path) -> None:
    _write_state(tmp_path, "rsi-test0001", _state_dict())
    ref = HarnessProvider(tmp_path).locate_artifact("rsi-test0001", "C1")
    assert ref.artifact_id == "C1"
    assert ref.path == "/tmp/refs.yaml"


def test_materialized_request_is_validated_against_task_snapshots(tmp_path: Path) -> None:
    task_id = "rsi-materialized"
    task_root = tmp_path / task_id
    input_dir = task_root / "input"
    harness_dir = task_root / "harness"
    config_dir = task_root / "config"
    run_dir = task_root / "run"
    for directory in (input_dir, harness_dir, config_dir, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    dataset = input_dir / "cases.json"
    dataset.write_text('[{"case_id": "a"}]', encoding="utf-8")
    source_harness = tmp_path / "source_harness.yaml"
    source_harness.write_text("name: demo\n", encoding="utf-8")
    harness_material = RsiTaskMaterializer(tmp_path).materialize_harness_refs(
        task_id,
        source_harness,
    )
    refs = Path(harness_material["path"])
    profile = config_dir / "harness_orchestrator.yaml"
    profile.write_text("workspace_dir: %s\n" % run_dir, encoding="utf-8")
    model_dir = task_root / "models"
    model_dir.mkdir()
    model = model_dir / "evaluation.yaml"
    model.write_text("model_client_config: {}\n", encoding="utf-8")
    import hashlib

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    materials = {
        "input_snapshot": {"sha256": digest(dataset)},
        "harness_snapshot": {
            "sha256": digest(refs),
            "source_path": str(source_harness.resolve()),
            "source_sha256": digest(source_harness),
            "package_path": harness_material["package_path"],
            "target_sha256": harness_material["target_sha256"],
        },
        "profile": {"sha256": digest(profile)},
        "models": {"evaluation": {"path": str(model), "config_sha256": digest(model)}},
    }
    attachment = input_dir / "input.txt"
    attachment.write_text("original attachment", encoding="utf-8")
    materials["input_snapshot"]["files"] = {"input.txt": digest(attachment)}
    (task_root / "task.json").write_text(
        json.dumps({"config": {"rsi_materials": materials}}),
        encoding="utf-8",
    )

    request = HarnessEngineRequest(
        task_id=task_id,
        dataset_files=(str(dataset),),
        harness_refs_path=str(refs),
        output_dir=str(run_dir),
        dataset_id="single_harness_benchmark",
        max_iterations=1,
        search_width=1,
        model_refs={},
        orchestrator_config_path=str(profile),
    )
    provider = HarnessProvider(tmp_path, orchestrator=_StubOrchestrator(_state_dict()))
    import asyncio

    result = asyncio.run(provider.run(request))
    assert result.status == "completed"

    attachment.write_text("changed attachment", encoding="utf-8")
    with pytest.raises(RsiPathNotAllowed, match="dataset file changed"):
        asyncio.run(provider.run(request))
    with pytest.raises(RsiResumeInputChanged, match="dataset file changed"):
        asyncio.run(provider.resume(request))
    attachment.write_text("original attachment", encoding="utf-8")

    source_harness.write_text("name: changed\n", encoding="utf-8")
    with pytest.raises(RsiPathNotAllowed):
        asyncio.run(provider.run(request))
    with pytest.raises(RsiResumeInputChanged):
        asyncio.run(provider.resume(request))


def test_analysis_reuses_optimizer_model() -> None:
    from openjiuwen.rsi import AutoCoordinatingHarnessConfig

    config = HarnessProvider._apply_model_refs(
        AutoCoordinatingHarnessConfig(),
        {"optimizer": "optimizer.yaml", "tester": "tester.yaml"},
    )

    assert config.model_configs.evaluation == "tester.yaml"
    assert config.evaluator.model_config_ref == "tester.yaml"
    assert config.model_configs.analysis == "optimizer.yaml"
    assert config.evaluation_result_analyzer.model_config_ref == "optimizer.yaml"
    assert config.model_configs.member_optimization == "optimizer.yaml"
    assert config.member_optimizer.model_config_ref == "optimizer.yaml"
