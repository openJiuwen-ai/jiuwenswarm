"""Regression coverage for the direct AgentServer Generic Harness path."""

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
