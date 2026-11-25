# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

import json
from pathlib import Path
import pytest
from jiuwenswarm.agents.harness.common.rsi.errors import RsiDatasetInvalid
from jiuwenswarm.agents.harness.common.rsi.harness_provider import engine_validate_input
from jiuwenswarm.agents.harness.common.rsi.materializer import (
    RsiTaskMaterialization,
    RsiTaskMaterializer,
)
from jiuwenswarm.agents.harness.common.rsi.context import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, TaskStatus, utcnow_iso


# --- from test_dataset_files.py ---

@pytest.mark.parametrize("suite", [False, True])
def test_snapshot_preserves_public_and_private_files(tmp_path, suite):
    from openjiuwen.rsi import load_cases
    from openjiuwen.rsi.harness_rsi.evaluator.case_backend import (
        _case_inputs,
        _stage_case_assets,
    )

    source_root = tmp_path / "dataset"
    source_root.mkdir()
    (source_root / "assets").mkdir()
    (source_root / "references").mkdir()
    (source_root / "assets/input.txt").write_text("public")
    (source_root / "references/private.txt").write_text("private")
    case = {"case_id": "gdpval-one", "input": "Read assets/input.txt",
            "assets": ["assets/input.txt"], "reference": {"files": ["references/private.txt"]}}
    if suite:
        case.update(id=case.pop("case_id"), prompt=case.pop("input"), domain="office")
    source = source_root / "train.json"
    source.write_text(json.dumps({"validation" if suite else "cases": [case]}))
    assert engine_validate_input(str(source))["valid"]
    result = RsiTaskMaterializer(tmp_path / "tasks").materialize_dataset("test", source)
    target = Path(result["path"])
    snapshot = RsiTaskMaterialization(result, {}, {}, {}).to_manifest()["input_snapshot"]
    assert snapshot["files"] == result["files"]
    assert set(result["files"]) == {"assets/input.txt", "references/private.txt"}
    assert (target.parent / "assets/input.txt").read_text() == "public"
    assert (target.parent / "references/private.txt").read_text() == "private"
    (source_root / "assets/input.txt").write_text("source edited later")
    loaded = load_cases([str(target)])[0]
    workspace = tmp_path / "solver"
    _stage_case_assets(loaded, workspace)
    assert (workspace / "assets/input.txt").read_text() == "public"
    assert not (workspace / "references").exists()
    assert _case_inputs(loaded) == "Read assets/input.txt"

@pytest.mark.parametrize("case", [
    {"case_id": "a", "reference": {"answer": "secret"}},
    {"case_id": "a", "input": ""},
    {"case_id": "a", "input": "task", "assets": ["missing.txt"]},
    {"case_id": "a", "input": "task", "reference": {"files": ["../private.txt"]}},
])
def test_invalid_packets_rejected_without_starting_task(tmp_path, case):
    source = tmp_path / "cases.json"
    source.write_text(json.dumps({"cases": [case]}))
    assert engine_validate_input(str(source))["valid"] is False
    with pytest.raises(RsiDatasetInvalid):
        RsiTaskMaterializer(tmp_path / "tasks").materialize_dataset("test", source)

# --- from test_workspace_layout.py ---

def test_default_rsi_context_uses_legacy_rsi_tasks_root(monkeypatch, tmp_path: Path):
    user_workspace = tmp_path / ".jiuwenswarm"
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: user_workspace,
    )

    context = build_rsi_service_context(
        None,
        enable_harness_materialization=False,
    )

    expected_root = (user_workspace / "rsi" / "tasks").resolve()
    assert context.tasks_root == expected_root
    assert context.harness_activation_store.root == expected_root
    assert context.harness_activation_store.activation_path == expected_root / "activation.json"

    task_id = "rsi-layout-test"
    context.store.create(
        RsiTask(
            task_id=task_id,
            name="layout",
            scenario="HARNESS",
            status=TaskStatus.CREATED.value,
            created_at=utcnow_iso(),
            input_file=str(context.tasks_root / task_id / "input" / "dataset.json"),
            run_dir=str(context.tasks_root / task_id / "run"),
        )
    )
    context.ensure_root(task_id)

    task_root = context.tasks_root / task_id
    assert (task_root / "task.json").is_file()
    assert (task_root / "tree.json").is_file()

