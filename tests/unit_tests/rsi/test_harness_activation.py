# -*- coding: utf-8 -*-
"""RSI published Harness refs and activation persistence tests."""

from pathlib import Path

import pytest
import yaml
from jiuwenswarm.server.runtime import extension_package_manager as catalog

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

pytestmark = pytest.mark.usefixtures("rsi_catalog_workspace")


def _write_package(root: Path, name: str = "policy_harness") -> Path:
    package = root / "member_optimizations" / "current_harnesses" / name
    package.mkdir(parents=True)
    (package / "harness_config.yaml").write_text(
        "extension_name: validation_harness\nschema_version: expert_harness.v1\n",
        encoding="utf-8",
    )
    (package / "prompt.md").write_text("hello\n", encoding="utf-8")
    return package


def test_published_refs_resolve_relative_to_refs_parent(tmp_path):
    package = _write_package(tmp_path)
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
                        "description": "published policy harness",
                        "harness_ref_path": "current_harnesses/policy_harness",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    parsed = parse_published_harness_refs(refs, task_run_root=tmp_path)

    assert parsed.role == "policy_harness"
    assert parsed.package_path == package.resolve()
    assert parsed.metadata["member_name"] == "policy_harness"
    assert parsed.refs_payload["version"] == 1


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


def test_published_refs_require_single_role_and_manifest(tmp_path):
    first = _write_package(tmp_path, "first")
    _write_package(tmp_path, "second")
    refs = tmp_path / "member_optimizations" / "current_harness_refs.yaml"
    refs.write_text(
        yaml.safe_dump(
            {
                "harness_refs": {
                    "first": "current_harnesses/first",
                    "second": "current_harnesses/second",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RsiHarnessInvalid, match="恰好一个"):
        parse_published_harness_refs(refs, task_run_root=tmp_path)

    (first / "harness_config.yaml").unlink()
    refs.write_text(
        yaml.safe_dump({"harness_refs": {"first": "current_harnesses/first"}}),
        encoding="utf-8",
    )
    with pytest.raises(RsiHarnessInvalid, match="配置文件"):
        parse_published_harness_refs(refs, task_run_root=tmp_path)


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


@pytest.mark.asyncio
async def test_installer_publishes_and_is_idempotent(tmp_path):
    tasks_root = tmp_path / "rsi" / "tasks"
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    task_id = "rsi-install-01"
    context.store.create(_completed_harness_task(context, task_id))
    _write_published_state(context, task_id, extension_name="validation_harness")

    class FakeRsiAgentManager:
        def __init__(self):
            self.calls = []

        async def broadcast_rsi_harness_change(self, old_installation, new_installation):
            self.calls.append((old_installation, new_installation))
            return {"attempted": 1, "succeeded": 1, "failed": []}

    manager = FakeRsiAgentManager()
    installer = RsiHarnessInstaller(
        context.store,
        context.adapter_for_task,
        manager,
        activation_root=tasks_root,
    )

    first = await installer.install(task_id)
    second = await installer.install(task_id)

    assert first["status"] == "ACTIVE"
    assert first["already_active"] is False
    assert second["already_active"] is True
    assert len(manager.calls) == 1
    assert Path(context.harness_activation_store.get_active()["runtime_path"]).name == "validation_harness"
    assert Path(context.harness_activation_store.get_active()["runtime_path"]).is_relative_to(
        tasks_root / task_id / "harness" / "versions"
    )
    assert context.store.get(task_id).config["rsi_installation"]["installation_id"] == first["installation_id"]
    cards = catalog.list_plugin_packages({"filter": "local"})
    assert [card["id"] for card in cards] == [first["installation_id"]]
    assert cards[0]["installed"] is True
    assert catalog.resolve_plugin_dir(first["installation_id"]).is_dir()

    # An already-active installation repairs an unregistered/deleted catalog copy.
    catalog.uninstall_plugin_package({"id": first["installation_id"]})
    await installer.install(task_id)
    assert catalog.is_plugin_allowed(first["installation_id"])
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_installer_rolls_back_live_and_pointer_when_provenance_write_fails(tmp_path):
    tasks_root = tmp_path / "rsi" / "tasks"
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    task_id = "rsi-install-provenance-failure"
    context.store.create(_completed_harness_task(context, task_id))
    _write_published_state(context, task_id, extension_name="validation_harness")

    class FakeRsiAgentManager:
        def __init__(self):
            self.calls = []

        async def broadcast_rsi_harness_change(self, old_installation, new_installation):
            self.calls.append((old_installation, new_installation))
            return {"attempted": 1, "succeeded": 1, "failed": []}

    def fail_provenance(*_args, **_kwargs):
        raise OSError("task.json is temporarily read-only")

    context.store.merge_config = fail_provenance
    manager = FakeRsiAgentManager()
    activation_root = tasks_root
    installer = RsiHarnessInstaller(
        context.store,
        context.adapter_for_task,
        manager,
        activation_root=activation_root,
    )

    with pytest.raises(RsiHarnessInstallFailed, match="provenance"):
        await installer.install(task_id)

    assert context.harness_activation_store.get_active() is None
    assert len(manager.calls) == 2
    assert manager.calls[1][0]["installation_id"] == manager.calls[0][1]["installation_id"]
    assert manager.calls[1][1] is None
    assert not list((activation_root / task_id / "harness" / "versions").glob("**/validation_harness"))
    assert catalog.list_plugin_packages({"filter": "local"}) == []


@pytest.mark.asyncio
async def test_hot_load_failure_compensates_catalog_registration(tmp_path):
    context = build_rsi_service_context(tmp_path / "rsi/tasks", enable_harness_materialization=False)
    context.store.create(_completed_harness_task(context, "failed-install"))
    _write_published_state(context, "failed-install", extension_name="validation_harness")

    class FailingManager:
        async def broadcast_rsi_harness_change(self, **kwargs):
            assert catalog.list_plugin_packages({"filter": "local"})
            raise RuntimeError("native load failed")

    installer = RsiHarnessInstaller(
        context.store, context.adapter_for_task, FailingManager(), activation_root=context.tasks_root,
    )
    with pytest.raises(RsiHarnessInstallFailed, match="native load failed"):
        await installer.install("failed-install")
    assert context.harness_activation_store.get_active() is None
    assert catalog.list_plugin_packages({"filter": "local"}) == []


@pytest.mark.asyncio
async def test_installer_rolls_back_to_any_retained_version_and_marks_initial(tmp_path):
    tasks_root = tmp_path / "rsi" / "tasks"
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    first_task_id = "rsi-install-first"
    second_task_id = "rsi-install-second"
    context.store.create(_completed_harness_task(context, first_task_id))
    context.store.create(_completed_harness_task(context, second_task_id))
    _write_published_state(context, first_task_id, extension_name="validation_first")
    _write_published_state(context, second_task_id, extension_name="validation_second")

    class FakeRsiAgentManager:
        def __init__(self):
            self.calls = []

        async def broadcast_rsi_harness_change(self, old_installation, new_installation):
            self.calls.append((old_installation, new_installation))
            return {"attempted": 1, "succeeded": 1, "failed": []}

    manager = FakeRsiAgentManager()
    installer = RsiHarnessInstaller(
        context.store,
        context.adapter_for_task,
        manager,
        activation_root=tasks_root,
    )

    first = await installer.install(first_task_id)
    second = await installer.install(second_task_id)
    rollback = await installer.rollback(first["installation_id"])
    versions = installer.list_versions()

    assert rollback["status"] == "ACTIVE"
    assert rollback["installation_id"] == first["installation_id"]
    assert rollback["from_installation_id"] == second["installation_id"]
    assert context.harness_activation_store.get_active()["installation_id"] == first["installation_id"]
    assert [item["installation_id"] for item in versions["versions"]] == [
        first["installation_id"],
        second["installation_id"],
    ]
    assert versions["versions"][0]["is_active"] is True
    assert versions["versions"][0]["is_initial"] is True
    assert manager.calls[-1][0]["installation_id"] == second["installation_id"]
    assert manager.calls[-1][1]["installation_id"] == first["installation_id"]
    with pytest.raises(RsiTaskStateConflict, match="保留的 Harness 版本"):
        context.task_service.delete({"task_id": second_task_id})


@pytest.mark.asyncio
async def test_installer_rejects_rollback_when_any_rsi_task_is_queued_running_or_paused(tmp_path):
    tasks_root = tmp_path / "rsi" / "tasks"
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    first_task_id = "rsi-install-first"
    second_task_id = "rsi-install-second"
    blocking_task_id = "rsi-rollback-blocked"
    context.store.create(_completed_harness_task(context, first_task_id))
    context.store.create(_completed_harness_task(context, second_task_id))
    blocking = _completed_harness_task(context, blocking_task_id)
    blocking.status = TaskStatus.QUEUED.value
    context.store.create(blocking)
    _write_published_state(context, first_task_id, extension_name="validation_first")
    _write_published_state(context, second_task_id, extension_name="validation_second")

    class FakeRsiAgentManager:
        async def broadcast_rsi_harness_change(self, old_installation, new_installation):
            return {"attempted": 1, "succeeded": 1, "failed": []}

    installer = RsiHarnessInstaller(
        context.store,
        context.adapter_for_task,
        FakeRsiAgentManager(),
        activation_root=tasks_root,
    )
    first = await installer.install(first_task_id)
    second = await installer.install(second_task_id)

    with pytest.raises(RsiTaskStateConflict, match=blocking_task_id):
        await installer.rollback(first["installation_id"])

    assert context.harness_activation_store.get_active()["installation_id"] == second["installation_id"]


@pytest.mark.asyncio
async def test_installer_restores_old_active_when_rollback_pointer_write_fails(tmp_path, monkeypatch):
    tasks_root = tmp_path / "rsi" / "tasks"
    context = build_rsi_service_context(tasks_root, enable_harness_materialization=False)
    first_task_id = "rsi-install-first"
    second_task_id = "rsi-install-second"
    context.store.create(_completed_harness_task(context, first_task_id))
    context.store.create(_completed_harness_task(context, second_task_id))
    _write_published_state(context, first_task_id, extension_name="validation_first")
    _write_published_state(context, second_task_id, extension_name="validation_second")

    class FakeRsiAgentManager:
        def __init__(self):
            self.calls = []

        async def broadcast_rsi_harness_change(self, old_installation, new_installation):
            self.calls.append((old_installation, new_installation))
            return {"attempted": 1, "succeeded": 1, "failed": []}

    manager = FakeRsiAgentManager()
    installer = RsiHarnessInstaller(
        context.store,
        context.adapter_for_task,
        manager,
        activation_root=tasks_root,
    )
    first = await installer.install(first_task_id)
    second = await installer.install(second_task_id)

    def fail_commit(_record):
        raise OSError("activation pointer is read-only")

    monkeypatch.setattr(installer.activation_store, "commit", fail_commit)
    with pytest.raises(RsiHarnessInstallFailed, match="回退"):
        await installer.rollback(first["installation_id"])

    assert context.harness_activation_store.get_active()["installation_id"] == second["installation_id"]
    assert manager.calls[-2][0]["installation_id"] == second["installation_id"]
    assert manager.calls[-2][1]["installation_id"] == first["installation_id"]
    assert manager.calls[-1][0]["installation_id"] == first["installation_id"]
    assert manager.calls[-1][1]["installation_id"] == second["installation_id"]
