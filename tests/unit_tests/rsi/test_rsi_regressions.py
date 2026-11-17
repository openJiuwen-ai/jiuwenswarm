# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 6 small test modules)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.model_resolver import RsiModelConfigResolver
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers
import importlib
import os
import subprocess
import sys
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineRequest
from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import (
    _MOCK_EVAL_CASES,
    MockHarnessProvider,
)
from jiuwenswarm.agents.harness.common.rsi.projector import RsiProjector
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.orchestrator import PaperTreeOrchestrator
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.schemas import (
    NodeStageEvent,
    PaperNodeExtra,
    RsiTreeNode,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.tree_provider.storage import TaskStorage
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import ArtifactEngineAdapter
from openjiuwen.rsi.artifact_rsi.program_opt import PuctProgramArtifactProvider
from jiuwenswarm.agents.harness.common.rsi.artifact_adapter import provider_node_to_dict
from openjiuwen.rsi.artifact_rsi.program_opt import events
from openjiuwen.rsi.artifact_rsi.program_opt.state import ProgramRunState, read_tree_file


# --- from test_agentserver_generic_harness_smoke.py ---

class _FakeModelConfig:
    def __init__(self, values: dict):
        self.values = values

    def model_dump(self, **_: object) -> dict:
        return dict(self.values)

def _model_resolver() -> RsiModelConfigResolver:
    entries = [
        {
            "model_client_config": {
                "model_name": "smoke-model",
                "client_provider": "OpenAI",
                "api_base": "https://example.test/v1",
                "api_key": "test-only",
            },
            "model_config_obj": {"temperature": 0.0},
            "is_default": True,
        }
    ]

    def build_model(mcc: dict, mco: dict) -> SimpleNamespace:
        return SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        )

    return RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: entries,
        zen_loader=lambda: [],
        model_builder=build_model,
    )

def _request(params: dict) -> SimpleNamespace:
    return SimpleNamespace(
        req_method=ReqMethod.RSI_TASK_CREATE,
        params=params,
        session_id=None,
    )

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repair_options, expected_repair_rounds",
    [
        ({}, 3),
        ({"max_repair_rounds": 1}, 1),
        ({"training_options": {"max_repair_rounds": 2}}, 2),
        ({"max_repair_rounds": 5, "training_options": {"max_repair_rounds": 2}}, 5),
    ],
)
async def test_agentserver_accepts_evobench_suite_and_generic_harness_refs(
    tmp_path: Path, repair_options: dict, expected_repair_rounds: int,
) -> None:
    dataset_root = tmp_path / "datasets"
    harness_root = tmp_path / "harnesses"
    package = harness_root / "policy_harness"
    package.mkdir(parents=True)
    (package / "harness_config.yaml").write_text(
        "schema_version: harness_config.v0.1\n"
        "id: policy_harness\n"
        "name: RSI Policy Harness\n"
        "tools: []\nrails: []\nskills: []\n",
        encoding="utf-8",
    )
    refs = harness_root / "initial_harness_refs.yaml"
    refs.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "harness_refs": {"policy_harness": "policy_harness"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    suite = dataset_root / "suites" / "train_suite.json"
    suite.parent.mkdir(parents=True)
    suite.write_text(
        json.dumps(
            {
                "name": "train",
                "validation": [
                    {
                        "id": "gdpval-office-1",
                        "domain": "office",
                        "prompt": "create the requested office deliverable",
                        "metadata": {"task_type": "office"},
                    },
                    {
                        "id": "gdpval-general-1",
                        "domain": "general",
                        "prompt": "create the requested general deliverable",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    tasks_root = tmp_path / "tasks"
    context = build_rsi_service_context(
        tasks_root,
        enable_harness_materialization=True,
        harness_materializer=RsiTaskMaterializer(
            tasks_root,
            dataset_root=dataset_root,
            harness_root=harness_root,
        ),
        model_resolver=_model_resolver(),
    )
    handlers = RsiAgentServerHandlers(context)

    result = await handlers.handle_async(
        _request(
            {
                "scenario": "HARNESS",
                "name": "gdpval3",
                "input_file": str(suite),
                "harness_path": str(refs),
                "model_refs": {"optimizer": "smoke-model", "tester": "smoke-model"},
                "domain": "office",
                "improver_policy_ref": "",
                "execution_mode": "local",
                "max_epochs": 1,
                "batch_size": 1,
                "max_issue_attempts": 8,
                **repair_options,
                "sibling_candidate_count": 1,
                "rollout_concurrency": 2,
            }
        )
    )

    assert result["ok"] is True
    task_id = result["payload"]["task_id"]
    task = context.store.get(task_id)
    task_root = tasks_root / task_id
    cases = json.loads(Path(task.input_file).read_text(encoding="utf-8"))
    refs_payload = yaml.safe_load(
        Path(task.config["harness_refs_path"]).read_text(encoding="utf-8")
    )
    profile = yaml.safe_load(
        Path(task.config["orchestrator_config_path"]).read_text(encoding="utf-8")
    )

    assert [case["case_id"] for case in cases["cases"]] == ["gdpval-office-1"]
    assert cases["cases"][0]["input"] == "create the requested office deliverable"
    local_harness = Path(refs_payload["harness_refs"]["validation_harness"])
    assert local_harness.is_dir()
    assert local_harness.is_relative_to(task_root / "harness")
    assert (local_harness / "harness_config.yaml").is_file()
    assert profile["max_epochs"] == 1
    assert profile["data_loader"]["batch_size"] == 1
    assert profile["member_optimizer"]["sibling_candidate_count"] == 1
    assert profile["member_optimizer"]["max_issue_attempts_per_batch"] == 8
    assert profile["member_optimizer"]["max_repair_rounds_per_batch"] == expected_repair_rounds
    assert profile["rsi_runtime"] == {
        "domain": "office",
        "execution_mode": "local",
        "rollout_concurrency": 2,
    }
    task_manifest = (task_root / "task.json").read_text(encoding="utf-8")
    assert str(harness_root) not in task_manifest
    assert "api_key" not in (task_root / "task.json").read_text(encoding="utf-8")

# --- from test_lazy_exports.py ---

def test_importing_rsi_errors_does_not_load_optional_execution_services():
    code = """
import sys
from jiuwenswarm.agents.harness.common.rsi.errors import RsiHarnessInstallConflict
prefix = 'jiuwenswarm.agents.harness.common.rsi.'
for module in ('context', 'provider_factory', 'paper_provider', 'mock_harness_provider', 'materializer'):
    assert prefix + module not in sys.modules, module
assert not any(name.startswith('openjiuwen') for name in sys.modules)
"""
    root = Path(__file__).resolve().parents[3]
    subprocess.run([sys.executable, "-c", code], cwd=root,
                   env={**os.environ, "PYTHONPATH": str(root)}, check=True,
                   capture_output=True, text=True, timeout=30)

def test_public_rsi_exports_preserve_original_symbols_and_discoverability():
    package = importlib.import_module("jiuwenswarm.agents.harness.common.rsi")
    assert set(package.__all__) <= set(dir(package))
    for name in package.__all__:
        module, attribute = package._EXPORTS[name]
        assert getattr(package, name) is getattr(importlib.import_module(module), attribute)

# --- from test_mock_harness_stage_events.py ---

@pytest.mark.asyncio
async def test_mock_harness_emits_per_case_stage_events(tmp_path):
    provider = MockHarnessProvider(tmp_path, iteration_delay=0, case_delay=0)
    events = []

    async def on_event(event):
        events.append(event)

    request = HarnessEngineRequest(
        task_id="rsi-stage-task",
        dataset_files=("dataset.json",),
        harness_refs_path="",
        output_dir=str(tmp_path / "run"),
        dataset_id="single_harness_benchmark",
        max_iterations=1,
        search_width=1,
        model_refs={},
    )
    await provider.run(request, on_event=on_event)

    stage_events = [event for event in events if getattr(event, "event_type", None) == "node.stage"]
    assert len(stage_events) == _MOCK_EVAL_CASES * 2
    assert stage_events[0].stage["name"] == f"Case 1/{_MOCK_EVAL_CASES}"
    assert stage_events[-1].stage["name"].endswith("passed")
    assert [event.stage["name"] for event in stage_events[0::2]] == [
        f"Case {index}/{_MOCK_EVAL_CASES}" for index in range(1, _MOCK_EVAL_CASES + 1)
    ]

    node_events = [event for event in events if getattr(event, "event_type", None) == "node"]
    assert node_events[0].node.type == "provisional"
    assert node_events[-1].node.adopted is True

# --- from test_paper_stage_projection.py ---

@pytest.mark.asyncio
async def test_stage_snapshot_keeps_one_pending_node_and_zero_completed(tmp_path):
    storage = TaskStorage(str(tmp_path / "provider"))
    node = RsiTreeNode(
        node_id="artifact:t:node:1", iteration=1, parent_id=None,
        type="reporting", adopted=False,
        extra={"paper": {"outcome": "pending"}},
    )
    storage.append_node(node)
    orchestrator = object.__new__(PaperTreeOrchestrator)
    orchestrator.storage = storage
    orchestrator.on_event = None
    projector = RsiProjector(tmp_path / "web")
    for stage in ("正在调研文献", "正在执行实验", "正在撰写论文"):
        await orchestrator._emit(NodeStageEvent(
            node_ref=node.node_id, stage={"id": stage, "name": stage},
        ))
        tree = projector.sync_provider_tree("t", {
            "nodes": storage.load_tree(), "depth": 0, "iteration": 0,
        })
        assert len(tree["nodes"]) == 2
        pending = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
        assert pending["description"] == stage
        assert pending["type"] == "PROVISIONAL"
        assert pending["extra"]["stage"]["name"] == stage
        assert projector.derive_progress("t")["iteration"] == 0
    completed = storage.load_tree()[0].model_copy(update={
        "adopted": True, "extra": {"paper": {"outcome": "success"}},
    })
    storage.append_node(completed)
    tree = projector.sync_provider_tree("t", {
        "nodes": storage.load_tree(), "depth": 0, "iteration": 1,
    })
    assert len(tree["nodes"]) == 2
    pending = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
    assert pending["type"] == "ADOPTED"
    assert projector.derive_progress("t")["iteration"] == 1

def test_pruned_provider_node_replaces_pending_projection_and_keeps_reason(tmp_path):
    projector = RsiProjector(tmp_path / "web")
    projector.sync_provider_tree(
        "t",
        {
            "nodes": [
                RsiTreeNode(
                    node_id="artifact:t:node:1",
                    iteration=1,
                    parent_id=None,
                    type="reporting",
                    adopted=False,
                    extra={"paper": {"outcome": "pending"}},
                )
            ],
            "depth": 0,
            "iteration": 0,
        },
    )

    pruned = RsiTreeNode(
        node_id="artifact:t:node:1",
        iteration=1,
        parent_id=None,
        type="pruned",
        adopted=False,
        reason="资料获取质量不佳，已剪枝。",
        failure_class="pipeline_failed",
        extra={
            "paper": PaperNodeExtra(
                logical_kind="pruned",
                round_index=1,
                attempt=1,
                outcome="failed",
                node_run_id="t-r1",
            ).model_dump(mode="json")
        },
    )
    tree = projector.sync_provider_tree(
        "t", {"nodes": [pruned], "depth": 1, "iteration": 1}
    )

    node = next(item for item in tree["nodes"] if item["node_id"] != "ROOT")
    assert node["type"] == "PRUNED"
    assert node["failure_reason"] == "资料获取质量不佳，已剪枝。"

# --- from test_program_provider_persistence.py ---

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

# --- from test_program_stage_projection.py ---

def test_program_stages_and_best_branch_survive_snapshot_refresh(tmp_path):
    run = ProgramRunState(task_id="t", run_dir=tmp_path / "engine", total_iterations=2)
    projector = RsiProjector(tmp_path / "web")
    def emit(event):
        for message in run.absorb(event):
            if message.event_type == "node":
                projector.on_provider_node("t", message.node)
            elif message.event_type == "node.stage":
                projector.on_node_stage("t", {"node_ref": message.node_ref, "stage": message.stage})
    emit(events.seeded(0, 0.1))
    for iteration, score in ((1, 0.2), (2, 0.3)):
        emit({"type": "candidate_started", "iteration": iteration, "parentIndex": 0})
        emit({"type": "stage", "iteration": iteration, "id": "evaluate", "name": "正在评测程序"})
        tree = projector.sync_provider_tree("t", read_tree_file("t"))
        assert tree["nodes"][-1]["type"] == "PROVISIONAL"
        assert tree["nodes"][-1]["description"] == "正在评测程序"
        assert tree["iteration"] == iteration - 1
        emit(events.expanded(iteration, 0, 1, score, True, iteration=iteration))
        assert provider_node_to_dict(run.nodes[iteration])["type"] == "PROVISIONAL"
        emit(events.merged(iteration, True, "better"))
    tree = projector.sync_provider_tree("t", read_tree_file("t"))
    assert len(tree["nodes"]) == 3
    assert tree["iteration"] == 2
    assert tree["nodes"][1]["type"] == "REJECTED"
    assert tree["nodes"][1]["extra"]["program"]["logical_kind"] == "adopted"
    assert tree["nodes"][2]["type"] == "ADOPTED"

