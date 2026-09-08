"""Tests for the production Harness task materialization boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiDatasetInvalid,
    RsiModelNotFound,
    RsiPathInvalid,
    RsiUnsupportedParameter,
)
from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
    resolve_native_harness_baseline,
)
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.harness_provider import (
    HarnessProvider,
    engine_validate_input,
)
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.model_resolver import RsiModelConfigResolver


def _entry(
    name: str,
    *,
    alias: str = "",
    is_default: bool = False,
    api_base: str = "https://example.test/v1",
):
    return {
        "model_client_config": {
            "model_name": name,
            "client_provider": "OpenAI",
            "api_base": api_base,
            "api_key": f"secret-{name}",
        },
        "model_config_obj": {"temperature": 0.2},
        "alias": alias,
        "is_default": is_default,
    }


class _FakeModelConfig:
    def __init__(self, values: dict):
        self.values = values

    def model_dump(self, **_: object) -> dict:
        return dict(self.values)


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(hashlib.sha256(item.read_bytes()).hexdigest()))
    return digest.hexdigest()


def test_model_resolver_uses_models_list_global_origin_index(tmp_path: Path) -> None:
    entries = [
        _entry("same", alias="first", is_default=True, api_base="https://one.test/v1"),
        _entry("same", alias="second", api_base="https://two.test/v1"),
    ]

    def build_model(mcc: dict, mco: dict) -> SimpleNamespace:
        return SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        )

    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: entries,
        zen_loader=lambda: [],
        model_builder=build_model,
    )

    manifest = resolver.resolve_to_file("same#1", "tester", tmp_path)
    payload = yaml.safe_load((tmp_path / "tester.yaml").read_text(encoding="utf-8"))

    assert manifest["origin_index"] == 1
    assert manifest["model_name"] == "same"
    assert payload["model_client_config"]["api_base"] == "https://two.test/v1"
    assert payload["model_client_config"]["max_retries"] == 0
    assert payload["model_request_config"]["model"] == "same"
    assert payload["model_client_config"]["api_key"] == "secret-same"


def test_model_resolver_rejects_unknown_reference_without_default_fallback(
    tmp_path: Path,
) -> None:
    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: [_entry("configured", is_default=True)],
        zen_loader=lambda: [],
        model_builder=lambda mcc, mco: SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        ),
    )

    with pytest.raises(RsiModelNotFound):
        resolver.resolve_to_file("missing", "tester", tmp_path)


def test_native_harness_baseline_is_capability_free_and_materializable(
    tmp_path: Path,
) -> None:
    baseline = resolve_native_harness_baseline()

    assert baseline is not None
    payload = yaml.safe_load(baseline.read_text(encoding="utf-8"))
    assert payload["id"] == "rsi-native-agent-baseline"
    for capability in ("tools", "rails", "skills", "prompt_sections"):
        assert payload.get(capability, []) == []

    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs(
        "rsi-native-baseline",
        baseline,
    )
    refs = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    package = Path(refs["harness_refs"]["validation_harness"])

    assert package.is_dir()
    assert package.is_relative_to(
        tmp_path / "tasks" / "rsi-native-baseline" / "harness" / "versions"
    )
    assert (package / "harness_config.yaml").read_text(
        encoding="utf-8"
    ) == baseline.read_text(encoding="utf-8")
    assert result["package_path"] == str(package.resolve())
    assert result["source_config_path"] == str(
        (package / "harness_config.yaml").resolve()
    )


def test_gdpval_validation_suite_is_normalized(tmp_path: Path) -> None:
    source = tmp_path / "train_suite.json"
    source.write_text(
        json.dumps(
            {
                "validation": [
                    {
                        "id": "gdpval-case-1",
                        "domain": "office",
                        "prompt": "Prepare the report.",
                        "metadata": {"task_type": "spreadsheet"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    validation = engine_validate_input(str(source))
    materialized = RsiTaskMaterializer(tmp_path / "tasks").materialize_dataset(
        "rsi-gdpval-suite",
        source,
    )
    payload = json.loads(Path(materialized["path"]).read_text(encoding="utf-8"))

    assert validation == {"valid": True, "sample_count": 1, "errors": []}
    assert payload["dataset_id"] == "evobench_local_no_key_validation"
    assert payload["cases"] == [
        {
            "id": "gdpval-case-1",
            "prompt": "Prepare the report.",
            "metadata": {"task_type": "spreadsheet"},
            "case_id": "gdpval-case-1",
            "task_id": "gdpval-case-1",
            "input": "Prepare the report.",
            "domain": "office",
            "source": "gdpval",
            "task_type": "spreadsheet",
        }
    ]


def test_materializer_copies_dataset_wraps_single_harness_and_writes_validation_profile(
    tmp_path: Path,
) -> None:
    source_dataset = tmp_path / "source" / "validation.json"
    source_dataset.parent.mkdir()
    source_dataset.write_text('{"cases": [{"case_id": "a", "input": "Task a"}]}', encoding="utf-8")
    source_harness = tmp_path / "harness" / "harness_config.yaml"
    source_harness.parent.mkdir()
    source_harness.write_text("name: demo\n", encoding="utf-8")

    materializer = RsiTaskMaterializer(tmp_path / "tasks")
    task_id = "rsi-materialized"
    dataset = materializer.materialize_dataset(task_id, source_dataset)
    refs = materializer.materialize_harness_refs(task_id, source_harness)
    models_dir = tmp_path / "tasks" / task_id / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_paths = {
        "evaluation": str(models_dir / "evaluation.yaml"),
        "analysis": str(models_dir / "analysis.yaml"),
        "member_optimization": str(models_dir / "member_optimization.yaml"),
    }
    for model_path in model_paths.values():
        Path(model_path).write_text("model_client_config: {}\n", encoding="utf-8")
    profile = materializer.materialize_validation_profile(
        task_id,
        model_paths,
        max_iterations=4,
    )

    dataset_path = Path(dataset["path"])
    refs_path = Path(refs["path"])
    profile_path = Path(profile["path"])
    refs_payload = yaml.safe_load(refs_path.read_text(encoding="utf-8"))
    profile_payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    materialized_package = Path(refs_payload["harness_refs"]["validation_harness"])

    assert dataset_path.is_file()
    assert dataset_path.read_text(encoding="utf-8") == source_dataset.read_text(
        encoding="utf-8"
    )
    assert dataset["sha256"] == hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    assert refs_payload["harness_refs"] == {
        "validation_harness": str(materialized_package.resolve())
    }
    assert materialized_package.is_dir()
    assert materialized_package.is_relative_to(
        tmp_path / "tasks" / task_id / "harness" / "versions"
    )
    assert (materialized_package / "harness_config.yaml").read_text(
        encoding="utf-8"
    ) == source_harness.read_text(encoding="utf-8")
    assert refs["source_path"] == str(materialized_package.resolve())
    assert refs["source_sha256"] == _tree_digest(materialized_package)
    assert profile_payload["max_epochs"] == 4
    assert profile_payload["data_loader"]["batch_size"] == 1
    assert profile_payload["member_optimizer"]["sibling_candidate_count"] == 1
    assert profile_payload["member_optimizer"]["max_issue_attempts_per_batch"] == 8
    assert profile_payload["member_optimizer"]["max_repair_rounds_per_batch"] == 3
    assert (
        profile_payload["evaluation_result_analyzer"]["diagnosis_agent_max_concurrency"]
        == 5
    )
    assert profile["sha256"] == hashlib.sha256(profile_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "profile_args",
    [
        {"search_width": 2},
        {"options": {"sibling_candidate_count": 2}},
        {"options": {"improver_policy_ref": "policy.yaml"}},
    ],
)
def test_materializer_rejects_unsupported_single_harness_policy(
    tmp_path: Path, profile_args: dict,
) -> None:
    tasks_root = tmp_path / "tasks"
    task_id = "rsi-invalid-policy"
    models_dir = tasks_root / task_id / "models"
    models_dir.mkdir(parents=True)
    model_paths = {}
    for role in ("evaluation", "analysis", "member_optimization"):
        path = models_dir / f"{role}.yaml"
        path.write_text("model_client_config: {}\n", encoding="utf-8")
        model_paths[role] = str(path)

    with pytest.raises(RsiPathInvalid, match="requires one candidate"):
        RsiTaskMaterializer(tasks_root).materialize_validation_profile(
            task_id, model_paths, **profile_args,
        )
    assert not (tasks_root / task_id / "config" / "harness_orchestrator.yaml").exists()


@pytest.mark.parametrize(
    "options",
    [
        {"sibling_candidate_count": 2},
        {"improver_policy_ref": "policy.yaml"},
        {"training_options": {"sibling_candidate_count": 2}},
        {"training_options": {"improver_policy_ref": "policy.yaml"}},
    ],
)
def test_task_service_rejects_unsupported_policy_before_creating_task(
    tmp_path: Path, options: dict,
) -> None:
    context = build_rsi_service_context(tmp_path / "tasks")
    with pytest.raises(RsiUnsupportedParameter, match="requires one candidate"):
        context.task_service.create({
            "scenario": "HARNESS",
            "name": "invalid-policy",
            "input_file": str(tmp_path / "cases.json"),
            "model_refs": {"optimizer": "optimizer", "tester": "tester"},
            **options,
        })
    assert not context.store.list()


def test_materializer_preserves_harness_package_directory_for_engine_checkpoints(
    tmp_path: Path,
) -> None:
    package = tmp_path / "harness" / "demo"
    package.mkdir(parents=True)
    config = package / "harness_config.yaml"
    config.write_text("name: demo\n", encoding="utf-8")
    (package / "prompt_sections").mkdir()
    (package / "prompt_sections" / "sections.yaml").write_text(
        "sections: []\n", encoding="utf-8"
    )

    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs(
        "rsi-package",
        package,
    )

    refs = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    local_source = Path(refs["harness_refs"]["validation_harness"])
    assert local_source.is_dir()
    assert local_source.is_relative_to(tmp_path / "tasks" / "rsi-package" / "harness")
    assert result["source_path"] == str(local_source.resolve())
    assert result["source_config_path"] == str((local_source / "harness_config.yaml").resolve())
    assert result["source_sha256"] == _tree_digest(local_source)


def test_materializer_accepts_manifest_json_harness_directory(tmp_path: Path) -> None:
    package = tmp_path / "harness" / "modern"
    package.mkdir(parents=True)
    manifest = package / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "package_type": "plugin",
                "id": "modern-harness",
                "name": "Modern Harness",
                "description": "Modern package",
            }
        ),
        encoding="utf-8",
    )

    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs(
        "rsi-modern-package",
        package,
    )

    refs = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    local_package = Path(refs["harness_refs"]["validation_harness"])
    assert local_package.is_dir()
    assert local_package.is_relative_to(
        tmp_path / "tasks" / "rsi-modern-package" / "harness" / "versions"
    )
    assert result["source_config_path"] == str(
        (local_package / "manifest.json").resolve()
    )
    assert result["package_path"] == str(local_package.resolve())


def test_materializer_copies_single_file_harness_dependencies(tmp_path: Path) -> None:
    source_root = tmp_path / "bundle"
    source = source_root / "harness_config.yaml"
    source_root.mkdir()
    source.write_text(
        yaml.safe_dump(
            {
                "schema_version": "expert_harness.v1",
                "id": "single-file-bundle",
                "name": "single-file-bundle",
                "skills": [{"dir": "skills/demo"}],
            }
        ),
        encoding="utf-8",
    )
    tool_list = source_root / "tools" / "tools.yaml"
    tool_list.parent.mkdir()
    tool_list.write_text(
        yaml.safe_dump(
            [
                {
                    "type": "harness.tool.file",
                    "params": {
                        "file_path": "shared/demo_tool.py",
                        "class_name": "DemoTool",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    tool = source_root / "shared" / "demo_tool.py"
    tool.parent.mkdir()
    tool.write_text("class DemoTool:\n    pass\n", encoding="utf-8")
    skill = source_root / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Demo\n", encoding="utf-8")

    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs(
        "rsi-single-file",
        source,
    )
    refs = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    package = Path(refs["harness_refs"]["validation_harness"])

    assert package.is_dir()
    assert package.name == "bundle"
    assert package != source_root
    assert (package / "tools" / "tools.yaml").read_text(
        encoding="utf-8"
    ) == tool_list.read_text(encoding="utf-8")
    assert (package / "shared" / "demo_tool.py").read_text(
        encoding="utf-8"
    ) == tool.read_text(encoding="utf-8")
    assert (package / "skills" / "demo" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == skill.read_text(encoding="utf-8")
    assert result["source_path"] == str(package.resolve())
    assert result["package_path"] == str(package.resolve())
    assert result["target_sha256"] == _tree_digest(package)


def test_materializer_copies_manifest_json_file_dependencies(tmp_path: Path) -> None:
    source_root = tmp_path / "modern-file"
    manifest = source_root / "manifest.json"
    source_root.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "package_type": "plugin",
                "id": "modern-file",
                "name": "Modern File",
                "description": "Modern file package",
                "tools": [{"file": "shared/demo_tool.py", "class": "DemoTool"}],
                "skills": [{"dir": "skills/demo"}],
            }
        ),
        encoding="utf-8",
    )
    tool = source_root / "shared" / "demo_tool.py"
    tool.parent.mkdir()
    tool.write_text("class DemoTool:\n    pass\n", encoding="utf-8")
    skill = source_root / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Demo\n", encoding="utf-8")

    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_harness_refs(
        "rsi-modern-file",
        manifest,
    )
    refs = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    package = Path(refs["harness_refs"]["validation_harness"])

    assert package.is_dir()
    assert package.name == "modern-file"
    assert (package / "manifest.json").read_text(
        encoding="utf-8"
    ) == manifest.read_text(encoding="utf-8")
    assert (package / "shared" / "demo_tool.py").is_file()
    assert (package / "skills" / "demo" / "SKILL.md").is_file()


@pytest.mark.parametrize("legacy_options", [{}, {"search_width": 4}])
def test_task_service_materializes_private_validation_inputs_and_keeps_manifest_non_secret(
    tmp_path: Path,
    legacy_options: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dataset = tmp_path / "source" / "validation.json"
    source_dataset.parent.mkdir()
    source_dataset.write_text('{"cases": [{"case_id": "a", "input": "Task a"}]}', encoding="utf-8")
    source_harness = tmp_path / "harness" / "harness_config.yaml"
    source_harness.parent.mkdir()
    source_harness.write_text("name: demo\n", encoding="utf-8")

    def build_model(mcc: dict, mco: dict) -> SimpleNamespace:
        return SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        )

    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: [
            _entry("optimizer", is_default=True),
            _entry("tester"),
        ],
        zen_loader=lambda: [],
        model_builder=build_model,
    )
    tasks_root = tmp_path / "tasks"
    context = build_rsi_service_context(
        tasks_root,
        enable_harness_materialization=True,
        harness_materializer=RsiTaskMaterializer(tasks_root),
        model_resolver=resolver,
    )
    provider = HarnessProvider(tasks_root)
    context.register_harness_provider(provider)
    context.bind_task_service(harness_refs_provider=lambda: str(source_harness))

    result = context.task_service.create(
        {
            "scenario": "HARNESS",
            "name": "validation",
            "dataset_path": str(source_dataset),
            "model_refs": {"optimizer": "optimizer", "tester": "tester"},
            "max_iterations": 5,
            **legacy_options,
        }
    )
    task = context.store.get(result["task_id"])
    task_root = tasks_root / task.task_id
    assert Path(task.input_file).parent == task_root / "input"
    assert Path(task.config["harness_refs_path"]).parent == task_root / "harness"
    assert Path(task.config["orchestrator_config_path"]).parent == task_root / "config"
    profile = yaml.safe_load(
        Path(task.config["orchestrator_config_path"]).read_text(encoding="utf-8")
    )
    assert profile["max_epochs"] == 5
    assert profile["member_optimizer"]["sibling_candidate_count"] == 1
    # Exercise the real constructor, not just the YAML parser or a stub engine.
    request = HarnessEngineAdapter(provider).build_request(task.to_taskview())
    orchestrator = provider._resolve_orchestrator(request)
    assert orchestrator.config.max_epochs == 5
    assert orchestrator.config.member_optimizer.sibling_candidate_count == 1
    assert not orchestrator.config.member_optimizer.improver_policy_ref
    evaluated = []

    async def evaluation_boundary(self, **kwargs):
        evaluated.append(kwargs)
        raise RuntimeError("evaluation-startup-canary")

    monkeypatch.setattr(type(orchestrator), "_evaluate", evaluation_boundary)
    with pytest.raises(RuntimeError, match="evaluation-startup-canary"):
        asyncio.run(provider.run(request))
    assert len(evaluated) == 1
    assert Path(evaluated[0]["output_dir"]).name == "frozen_baseline"
    assert evaluated[0]["node_ref"] == "h0"
    assert task.config["dataset_id"] == "single_harness_benchmark"
    assert "api_key" not in (task_root / "task.json").read_text(encoding="utf-8")
    assert (task_root / "models" / "evaluation.yaml").is_file()
    assert (task_root / "models" / "analysis.yaml").is_file()
    assert (task_root / "models" / "member_optimization.yaml").is_file()
    for role, expected_model in {
        "evaluation": "tester",
        "analysis": "optimizer",
        "member_optimization": "optimizer",
    }.items():
        payload = yaml.safe_load(
            (task_root / "models" / f"{role}.yaml").read_text(encoding="utf-8")
        )
        assert payload["model_request_config"]["model"] == expected_model
    assert task.config["active_ref_released"] is True

    context.store.update_status(
        task.task_id,
        ["CREATED"],
        "TERMINATED",
        cause="test.cleanup",
    )
    context.task_service.delete({"task_id": task.task_id})
    assert not task_root.exists()


def test_task_service_rejects_invalid_dataset_before_writing_task_materials(
    tmp_path: Path,
) -> None:
    source_dataset = tmp_path / "source" / "duplicate.json"
    source_dataset.parent.mkdir()
    source_dataset.write_text(
        '[{"case_id": "same"}, {"case_id": "same"}]',
        encoding="utf-8",
    )
    source_harness = tmp_path / "harness" / "harness_config.yaml"
    source_harness.parent.mkdir()
    source_harness.write_text("name: demo\n", encoding="utf-8")

    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: [_entry("optimizer", is_default=True)],
        zen_loader=lambda: [],
        model_builder=lambda mcc, mco: SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        ),
    )
    tasks_root = tmp_path / "tasks"
    context = build_rsi_service_context(
        tasks_root,
        enable_harness_materialization=True,
        harness_materializer=RsiTaskMaterializer(tasks_root),
        model_resolver=resolver,
    )
    context.register_harness_provider(HarnessProvider(tasks_root))
    context.bind_task_service(harness_refs_provider=lambda: str(source_harness))

    with pytest.raises(RsiDatasetInvalid):
        context.task_service.create(
            {
                "scenario": "HARNESS",
                "name": "invalid-dataset",
                "input_file": str(source_dataset),
                "model_refs": {"optimizer": "optimizer", "tester": "optimizer"},
            }
        )

    assert not list(tasks_root.glob("rsi-*/task.json"))
