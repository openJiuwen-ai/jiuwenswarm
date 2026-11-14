# -*- coding: utf-8 -*-
"""RSI published Harness refs and activation persistence tests."""

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
    RsiHarnessInstaller,
    RsiHarnessActivationStore,
    hash_harness_package,
    parse_published_harness_refs,
)
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiHarnessInstallFailed,
    RsiHarnessInvalid,
    RsiTaskStateConflict,
)
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask, TaskStatus, utcnow_iso
from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context




def _write_package(root: Path, name: str = "policy_harness") -> Path:
    package = root / "member_optimizations" / "current_harnesses" / name
    package.mkdir(parents=True)
    (package / "harness_config.yaml").write_text(
        "extension_name: validation_harness\nschema_version: expert_harness.v1\n",
        encoding="utf-8",
    )
    (package / "prompt.md").write_text("hello\n", encoding="utf-8")
    return package




def test_published_refs_reject_path_outside_task_run(tmp_path):
    outside = tmp_path.parent / "outside-harness"
    outside.mkdir()
    (outside / "harness_config.yaml").write_text("extension_name: outside\n", encoding="utf-8")
    refs = tmp_path / "run" / "current_harness_refs.yaml"
    refs.parent.mkdir()
    refs.write_text(
        yaml.safe_dump({"harness_refs": {"policy": "../../outside-harness"}}),
        encoding="utf-8",
    )

    with pytest.raises(RsiHarnessInvalid, match="任务 run"):
        parse_published_harness_refs(refs, task_run_root=tmp_path / "run")




def test_package_hash_is_deterministic_and_content_sensitive(tmp_path):
    package = _write_package(tmp_path)
    first = hash_harness_package(package)
    (package / "prompt.md").write_text("changed\n", encoding="utf-8")
    second = hash_harness_package(package)

    assert len(first) == 64
    assert first != second


def test_activation_store_writes_active_pointer_and_survives_new_instance(tmp_path):
    root = tmp_path / "rsi" / "harness"
    store = RsiHarnessActivationStore(root)
    runtime_path = root / "versions" / "install-a" / "validation_harness"
    runtime_path.mkdir(parents=True)
    record = {
        "installation_id": "install-a",
        "runtime_path": str(runtime_path),
        "sha256": "a" * 64,
    }

    store.commit(record)

    assert store.get_active()["installation_id"] == "install-a"
    assert RsiHarnessActivationStore(root).get_active()["installation_id"] == "install-a"
    assert RsiHarnessActivationStore(root).list_history() == []
    assert not list(root.glob(".activation.*.tmp"))


def test_activation_store_rejects_runtime_path_outside_root(tmp_path):
    store = RsiHarnessActivationStore(tmp_path / "rsi" / "harness")

    with pytest.raises(ValueError, match="root"):
        store.commit(
            {
                "installation_id": "bad",
                "runtime_path": str(tmp_path / "outside"),
                "sha256": "b" * 64,
            }
        )


def _completed_harness_task(context, task_id: str) -> RsiTask:
    run_dir = context.tasks_root / task_id / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return RsiTask(
        task_id=task_id,
        name="rsi install test",
        scenario="HARNESS",
        status=TaskStatus.COMPLETED.value,
        created_at=utcnow_iso(),
        input_file=str(run_dir / "input.json"),
        model_refs={"optimizer": "optimizer", "tester": "tester"},
        config={},
        run_dir=str(run_dir),
    )


def _write_published_state(context, task_id: str, *, extension_name: str) -> Path:
    run_dir = Path(context.store.get(task_id).run_dir)
    package = run_dir / "member_optimizations" / "current_harnesses" / "policy_harness"
    package.mkdir(parents=True, exist_ok=True)
    (package / "harness_config.yaml").write_text(
        f"extension_name: {extension_name}\nschema_version: expert_harness.v1\n",
        encoding="utf-8",
    )
    (package / "prompt.md").write_text("optimized\n", encoding="utf-8")
    refs = package.parent.parent / "current_harness_refs.yaml"
    refs.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "harness_refs": {"policy_harness": "current_harnesses/policy_harness"},
                "roles": [
                    {
                        "role": "policy_harness",
                        "member_name": "policy_harness",
                        "description": "published",
                        "harness_ref_path": "current_harnesses/policy_harness",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "single_harness_state.yaml").write_text(
        yaml.safe_dump(
            {
                "publication_status": "published",
                "published_harness_refs_path": str(refs),
                "final_node_id": "candidate-1",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return refs












