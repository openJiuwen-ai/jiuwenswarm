# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Container materialization, local level loading and record projection."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.bundle import bundle_version_dir

from jiuwenswarm.common.schema.agent import AgentRef, AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.management import adapter as adapter_module
from jiuwenswarm.extensions_v2.management import catalog, containers
from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ActiveInstance,
    ExtensionInstallRecord,
    ExtensionInstallState,
)

from .support import METADATA_EXTENSION, archive_package, make_bus


def _request(method: ReqMethod, params: Any) -> AgentRequest:
    return AgentRequest(
        request_id="container-1",
        channel_id="web",
        req_method=method,
        params=params,
        metadata={},
        agent_ref=AgentRef.default(),
    )


def _write_member(
    root: Path, package_id: str, domain: str, requires=None, version: str = "1.0.0",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": package_id, "version": version, "domain": domain},
        "display": {"name": package_id},
        "extensions": [{"id": "main", "runtime": "backend", "module": "extension.py"}],
    }
    if requires:
        document["requires"] = {
            "packages": [{"id": dep, "version": spec} for dep, spec in requires],
        }
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    (root / "extension.py").write_text(METADATA_EXTENSION, encoding="utf-8")
    return root


def _build_bundle(root: Path, *, cross_domain_dependency: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "is_bundle": True,
        "package": {"id": "demo.bundle", "version": "2.0.0"},
        "display": {"name": "Demo bundle"},
        "members": [{"path": "plugins/demo.base"}, {"path": "plugins/demo.top"}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_member(
        root / "plugins/demo.base", "demo.base", "agentserver",
        requires=[("demo.top", ">=1.0.0")] if cross_domain_dependency else None,
    )
    _write_member(root / "plugins/demo.top", "demo.top", "gateway")
    return root


def _build_bundle_version(root: Path, version: str, member_version: str = "1.0.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "is_bundle": True,
        "package": {"id": "demo.bundle", "version": version},
        "display": {"name": "Demo bundle"},
        "members": [{"path": "plugins/demo.base"}, {"path": "plugins/demo.top"}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_member(root / "plugins/demo.base", "demo.base", "agentserver", version=member_version)
    _write_member(root / "plugins/demo.top", "demo.top", "gateway", version=member_version)
    return root


def _build_bundle_with_members(
    root: Path, version: str, members: list[tuple[str, list[tuple[str, str]] | None]],
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "is_bundle": True,
        "package": {"id": "demo.bundle", "version": version},
        "display": {"name": "Demo bundle"},
        "members": [{"path": f"plugins/{package_id}"} for package_id, _ in members],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for package_id, requires in members:
        _write_member(
            root / "plugins" / package_id, package_id, "agentserver", requires=requires,
        )
    return root


async def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExtensionManager:
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    monkeypatch.setattr(adapter_module, "resolve_install_dir", lambda: manager.install_dir)
    examples = tmp_path / "examples"
    examples.mkdir()
    monkeypatch.setattr(adapter_module, "resolve_examples_dir", lambda: examples)
    return manager


def _member(store: ExtensionInstallState, bundle_id: str, bundle_version: str, package_id: str):
    return store.get_bundle_member(bundle_id, bundle_version, package_id)


def _simulate_gateway(store: ExtensionInstallState, package_id: str, version: str = "1.0.0") -> None:
    """Stand in for the Gateway's independent member load."""

    for bundle in store.bundles():
        for member in bundle.member_records:
            if member.package_id == package_id and member.version == version:
                store.mark_bundle_member(
                    bundle.bundle_id, bundle.version, package_id,
                    enabled=True, active=True, status="active",
                )
                store.switch_active_instance(
                    ActiveInstance(
                        package_id=package_id,
                        version=version,
                        kind="bundle",
                        bundle_id=bundle.bundle_id,
                        bundle_version=bundle.version,
                        root=member.root,
                    )
                )
                return
    store.activate(
        ExtensionInstallRecord(
            package_id=package_id,
            version=version,
            origin="user",
            active=True,
            enabled=True,
            domain="gateway",
            status="active",
        )
    )
    store.switch_active_instance(
        ActiveInstance(package_id=package_id, version=version, kind="standalone"),
    )


@pytest.mark.asyncio
async def test_bundle_materializes_both_domains_and_loads_local_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)

    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )

    assert outcomes[0].overall_status == "partial"
    assert [record.package_id for record in manager.active_packages()] == ["demo.base"]
    record = store.get_bundle("demo.bundle")
    assert record is not None and record.status == "partial"
    assert _member(store, "demo.bundle", "2.0.0", "demo.base").status == "active"
    assert _member(store, "demo.bundle", "2.0.0", "demo.top").status == "pending"
    bundle_root = bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0")
    assert (bundle_root / "plugins" / "demo.base").is_dir()
    assert (bundle_root / "plugins" / "demo.top").is_dir()
    assert not (manager.install_dir / "demo.base").exists()
    assert not (manager.install_dir / "demo.top").exists()
    assert store.path == (
        manager.install_dir.parent / "extension" / "system_plugin_install_state.json"
    )


@pytest.mark.asyncio
async def test_bundle_member_stands_by_when_standalone_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    store.activate(
        ExtensionInstallRecord(
            package_id="demo.top", version="1.0.0", origin="user",
            active=True, enabled=True, domain="gateway", status="active",
        )
    )
    store.switch_active_instance(
        ActiveInstance(package_id="demo.top", version="1.0.0", kind="standalone"),
    )

    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )

    assert outcomes[0].overall_status == "partial"
    member = _member(store, "demo.bundle", "2.0.0", "demo.top")
    assert member is not None and member.status == "standby"
    assert store.get("demo.top").active is True
    assert store.active_instance("demo.top").kind == "standalone"
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0").is_dir()


@pytest.mark.asyncio
async def test_single_import_stands_by_when_a_bundle_member_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )
    single = _write_member(tmp_path / "single", "demo.base", "agentserver")

    outcomes = await containers.install_sources(manager, store, [single], origin="user")

    assert outcomes[0].overall_status == "partial"
    record = store.get("demo.base", "1.0.0")
    assert record is not None and record.status == "standby"
    member = _member(store, "demo.bundle", "2.0.0", "demo.base")
    assert member is not None and member.active is True
    assert [item.package_id for item in manager.active_packages()] == ["demo.base"]


@pytest.mark.asyncio
async def test_bundle_rejects_its_root_id_installed_as_a_single_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    store.activate(
        ExtensionInstallRecord(
            package_id="demo.bundle", version="1.0.0", origin="user",
            active=True, enabled=True, domain="agentserver", status="active",
        )
    )

    with pytest.raises(ExtensionFailure) as error:
        await containers.install_sources(
            manager, store, [_build_bundle(tmp_path / "source")], origin="user",
        )

    assert error.value.status is StatusCode.EXTENSION_PACKAGE_EXISTS


@pytest.mark.asyncio
async def test_single_import_rejects_an_installed_bundle_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )
    single = _write_member(tmp_path / "single", "demo.bundle", "agentserver")

    with pytest.raises(ExtensionFailure) as error:
        await containers.install_sources(manager, store, [single], origin="user")

    assert error.value.status is StatusCode.EXTENSION_PACKAGE_EXISTS


@pytest.mark.asyncio
async def test_two_bundles_may_share_a_member_id_with_one_active_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "is_bundle": True,
                "package": {"id": "demo.other", "version": "1.0.0"},
                "members": [{"path": "plugins/demo.base"}],
            }
        ),
        encoding="utf-8",
    )
    _write_member(other / "plugins/demo.base", "demo.base", "agentserver")

    outcomes = await containers.install_sources(manager, store, [other], origin="user")

    assert outcomes[0].overall_status == "partial"
    first = _member(store, "demo.bundle", "2.0.0", "demo.base")
    second = _member(store, "demo.other", "1.0.0", "demo.base")
    assert first is not None and first.active is True
    assert second is not None and second.status == "standby"
    assert store.active_instance("demo.base").bundle_id == "demo.bundle"


@pytest.mark.asyncio
async def test_bundle_rejects_a_member_id_equal_to_another_bundle_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = tmp_path / "first"
    first.mkdir()
    (first / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "is_bundle": True,
                "package": {"id": "demo.other", "version": "1.0.0"},
                "members": [{"path": "plugins/member"}],
            }
        ),
        encoding="utf-8",
    )
    _write_member(first / "plugins/member", "demo.other-member", "agentserver")
    await containers.install_sources(manager, store, [first], origin="user")
    second = tmp_path / "second"
    second.mkdir()
    (second / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "is_bundle": True,
                "package": {"id": "demo.other-suite", "version": "1.0.0"},
                "members": [{"path": "plugins/root"}],
            }
        ),
        encoding="utf-8",
    )
    _write_member(second / "plugins/root", "demo.other", "agentserver")

    with pytest.raises(ExtensionFailure) as error:
        await containers.install_sources(manager, store, [second], origin="user")

    assert error.value.status is StatusCode.EXTENSION_PACKAGE_EXISTS


@pytest.mark.asyncio
async def test_dependency_state_reads_persisted_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    requires = (("demo.top", ">=1.0.0"),)

    assert containers._dependency_state(requires, store) == "waiting"  # noqa: SLF001

    store.upsert(
        ExtensionInstallRecord(
            package_id="demo.top", version="1.0.0", origin="user",
            active=False, enabled=True, domain="gateway", status="failed",
        )
    )
    assert containers._dependency_state(requires, store) == "failed"  # noqa: SLF001
    store.remove("demo.top", "1.0.0")

    _simulate_gateway(store, "demo.top")
    assert containers._dependency_state(requires, store) == "ready"  # noqa: SLF001

@pytest.mark.asyncio
async def test_cross_domain_dependency_keeps_the_member_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    source = _build_bundle(tmp_path / "source", cross_domain_dependency=True)

    try:
        outcomes = await containers.install_sources(manager, store, [source], origin="user")

        assert outcomes[0].overall_status == "partial"
        assert manager.active_packages() == ()
        assert _member(store, "demo.bundle", "2.0.0", "demo.base").status == "pending"
        record = store.get_bundle("demo.bundle")
        assert record is not None and record.status == "partial"
    finally:
        containers._cancel_retry("demo.bundle@2.0.0")  # noqa: SLF001 - stop the background retry


@pytest.mark.asyncio
async def test_local_failure_does_not_mark_the_other_domain_member_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)

    async def fail(*args, **kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(manager, "enable_version", fail)
    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
    )

    assert outcomes[0].overall_status == "failed"
    assert {member["packageId"]: member["status"] for member in outcomes[0].members} == {
        "demo.base": "failed",
        "demo.top": "pending",
    }
    record = store.get_bundle("demo.bundle")
    assert record is not None and record.status == "uninstalling" and record.active is False
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0").is_dir()


@pytest.mark.asyncio
async def test_single_package_batch_rejects_a_cross_domain_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _write_member(
        tmp_path / "source-a", "demo.a", "agentserver", requires=[("demo.b", ">=1.0.0")],
    )
    second = _write_member(
        tmp_path / "source-b", "demo.b", "gateway", requires=[("demo.a", ">=1.0.0")],
    )

    with pytest.raises(ExtensionFailure):
        await containers.install_sources(manager, store, [first, second], origin="user")

    assert store.get("demo.a") is None
    assert store.get("demo.b") is None
    assert not (manager.install_dir / "demo.a").exists()
    assert not (manager.install_dir / "demo.b").exists()


@pytest.mark.asyncio
async def test_notify_ui_reaches_the_webui_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.extensions_v2 import assembly

    calls: list[list[str]] = []

    async def notify(package_ids):
        calls.append(list(package_ids))

    monkeypatch.setattr(
        assembly, "get_webui_runtime", lambda: SimpleNamespace(notify_packages_changed=notify),
    )
    await containers._notify_ui(["demo.base"])  # noqa: SLF001 - helper under test

    assert calls == [["demo.base"]]


@pytest.mark.asyncio
async def test_bundle_rejects_a_dependency_cycle_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    root = tmp_path / "source"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "is_bundle": True,
                "package": {"id": "demo.bundle", "version": "2.0.0"},
                "members": [{"path": "plugins/a"}, {"path": "plugins/b"}],
            }
        ),
        encoding="utf-8",
    )
    _write_member(root / "plugins/a", "demo.a", "agentserver", requires=[("demo.b", ">=1.0.0")])
    _write_member(root / "plugins/b", "demo.b", "agentserver", requires=[("demo.a", ">=1.0.0")])

    with pytest.raises(ExtensionFailure):
        await containers.install_sources(manager, store, [root], origin="user")

    assert store.get_bundle("demo.bundle") is None
    assert not bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0").exists()
    assert (await manager.query()).active == ()


@pytest.mark.asyncio
async def test_startup_recovery_marks_loaded_members_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle(tmp_path / "source")], origin="user")
    store.mark_bundle_member(
        "demo.bundle", "2.0.0", "demo.base", active=False, enabled=True, status="pending",
    )

    await containers.recover_containers(manager)

    record = _member(store, "demo.bundle", "2.0.0", "demo.base")
    assert record is not None and record.status == "active" and record.active is True


@pytest.mark.asyncio
async def test_bundle_detail_and_mine_project_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRef, AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle(tmp_path / "source")], origin="user")
    _simulate_gateway(store, "demo.top")
    adapter = PackageManagementAdapter()

    detail = await adapter.handle(
        AgentRequest(
            request_id="bundle-show",
            channel_id="web",
            req_method=ReqMethod.EXTENSIONS_V2_SHOW,
            params={"package_id": "demo.bundle"},
            metadata={},
            agent_ref=AgentRef.default(),
        )
    )
    mine = await adapter.handle(
        AgentRequest(
            request_id="bundle-mine",
            channel_id="web",
            req_method=ReqMethod.EXTENSIONS_V2_CATALOG,
            params={"scope": "mine"},
            metadata={},
            agent_ref=AgentRef.default(),
        )
    )

    assert detail.ok
    package = detail.payload["package"]
    assert package["kind"] == "bundle"
    assert package["membersCount"] == 2
    assert package["overallStatus"] == "enabled"
    assert package["events"] == []
    assert [member["packageId"] for member in package["systemPlugins"]] == ["demo.base", "demo.top"]
    assert mine.ok
    assert [item["id"] for item in mine.payload["packages"]] == ["demo.bundle"]


@pytest.mark.asyncio
async def test_gateway_single_package_is_recorded_and_projectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRef, AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    source = tmp_path / "source-gw"
    _write_member(source, "demo.gw", "gateway")
    await containers.install_sources(manager, store, [source], origin="user")
    assert store.get("demo.gw").status == "pending"
    _simulate_gateway(store, "demo.gw")
    adapter = PackageManagementAdapter()

    mine = await adapter.handle(
        AgentRequest(
            request_id="gw-mine",
            channel_id="web",
            req_method=ReqMethod.EXTENSIONS_V2_CATALOG,
            params={"scope": "mine"},
            metadata={},
            agent_ref=AgentRef.default(),
        )
    )
    detail = await adapter.handle(
        AgentRequest(
            request_id="gw-show",
            channel_id="web",
            req_method=ReqMethod.EXTENSIONS_V2_SHOW,
            params={"package_id": "demo.gw"},
            metadata={},
            agent_ref=AgentRef.default(),
        )
    )

    assert mine.ok
    card = next(item for item in mine.payload["packages"] if item["id"] == "demo.gw")
    assert (card["installed"], card["active"], card["kind"]) == (True, True, "plugin")
    assert detail.ok
    package = detail.payload["package"]
    assert (package["id"], package["active"], package["overallStatus"]) == ("demo.gw", True, "enabled")


@pytest.mark.asyncio
async def test_two_bundle_versions_stay_installed_with_one_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")

    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")

    inactive = store.get_bundle("demo.bundle", "1.0.0")
    active = store.get_bundle("demo.bundle", "1.0.1")
    assert inactive is not None and inactive.active is False and inactive.status == "disabled"
    assert active is not None and active.active is True
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.1").is_dir()
    assert store.active_bundle("demo.bundle").version == "1.0.1"


@pytest.mark.asyncio
async def test_switching_back_to_an_installed_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")
    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")

    await containers.install_sources(manager, store, [first], origin="user")

    assert store.active_bundle("demo.bundle").version == "1.0.0"
    assert store.get_bundle("demo.bundle", "1.0.1").active is False
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True
    assert _member(store, "demo.bundle", "1.0.1", "demo.base").active is False
    assert [record.version for record in manager.active_packages()] == ["1.0.0"]


@pytest.mark.asyncio
async def test_uninstalling_one_bundle_version_keeps_the_other_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")
    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")

    await containers.uninstall_container(manager, store, store.get_bundle("demo.bundle", "1.0.1"))

    removed = store.get_bundle("demo.bundle", "1.0.1")
    assert removed is not None and removed.status == "uninstalling" and removed.active is False
    assert store.get_bundle("demo.bundle", "1.0.0") is not None
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.1").is_dir()
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()
    assert _member(store, "demo.bundle", "1.0.1", "demo.base") is not None
    assert _member(store, "demo.bundle", "1.0.0", "demo.base") is not None

    await containers.install_sources(manager, store, [first], origin="user")
    assert store.active_bundle("demo.bundle").version == "1.0.0"
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True


@pytest.mark.asyncio
async def test_catalog_lists_every_installed_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v1", "1.0.0")], origin="user")
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v2", "1.0.1")], origin="user")

    cards = catalog.list_mine(
        tmp_path / "examples", await manager.query(), install_dir=manager.install_dir,
    )

    card = next(item for item in cards if item["id"] == "demo.bundle")
    assert card["versions"] == ["1.0.0", "1.0.1"]
    assert card["installedVersion"] == "1.0.1"
    assert card["overallStatus"] == "partial"


@pytest.mark.asyncio
async def test_show_returns_the_requested_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v1", "1.0.0")], origin="user")
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v2", "1.0.1")], origin="user")
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SHOW, {"package_id": "demo.bundle", "version": "1.0.0"})
    )

    assert response.ok
    package = response.payload["package"]
    assert package["version"] == "1.0.0"
    assert package["installedVersion"] == "1.0.1"
    assert package["overallStatus"] == "disabled"
    assert all(member["status"] == "disabled" for member in package["systemPlugins"])


@pytest.mark.asyncio
async def test_uninstall_request_tombstones_only_the_selected_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v1", "1.0.0")], origin="user")
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v2", "1.0.1")], origin="user")
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.bundle", "version": "1.0.0"})
    )

    assert response.ok
    removed = store.get_bundle("demo.bundle", "1.0.0")
    assert removed is not None and removed.status == "uninstalling"
    assert store.get_bundle("demo.bundle", "1.0.1") is not None
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()
    assert _member(store, "demo.bundle", "1.0.1", "demo.base") is not None


@pytest.mark.asyncio
async def test_new_bundle_version_replaces_member_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0", member_version="1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1", member_version="1.1.0")

    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")

    assert store.active_bundle("demo.bundle").version == "1.0.1"
    assert [record.version for record in manager.active_packages()] == ["1.1.0"]
    assert _member(store, "demo.bundle", "1.0.1", "demo.base").active is True
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is False
    assert manager.active_packages()[0].root == (
        bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.1") / "plugins" / "demo.base"
    )
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").enabled is False
    assert store.get_bundle("demo.bundle", "1.0.0").active is False


@pytest.mark.asyncio
async def test_uninstall_without_version_tombstones_every_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v1", "1.0.0")], origin="user")
    await containers.install_sources(manager, store, [_build_bundle_version(tmp_path / "v2", "1.0.1")], origin="user")
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.bundle"})
    )

    assert response.ok
    records = [record for record in store.bundles() if record.bundle_id == "demo.bundle"]
    assert len(records) == 2 and all(record.status == "uninstalling" for record in records)
    assert [member for member in store.bundle_members("demo.bundle")] != []
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.1").is_dir()


@pytest.mark.asyncio
async def test_failed_switch_restores_the_previous_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0", member_version="1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1", member_version="1.1.0")
    await containers.install_sources(manager, store, [first], origin="user")

    original_enable = manager.enable_version

    async def fail(package_id: str, version: str, **kwargs):
        if version == "1.1.0":
            raise RuntimeError("load failed")
        return await original_enable(package_id, version, **kwargs)

    monkeypatch.setattr(manager, "enable_version", fail)
    outcomes = await containers.install_sources(manager, store, [second], origin="user")

    assert outcomes[0].overall_status == "failed"
    assert store.active_bundle("demo.bundle").version == "1.0.0"
    tombstone = store.get_bundle("demo.bundle", "1.0.1")
    assert tombstone is not None and tombstone.status == "uninstalling"
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.1").is_dir()
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True


@pytest.mark.asyncio
async def test_standalone_and_bundle_member_coexist_with_one_active_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    bundle = _build_bundle_version(tmp_path / "bundle", "1.0.0")
    await containers.install_sources(manager, store, [bundle], origin="user")
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")

    first = await containers.install_sources(manager, store, [standalone], origin="user")

    assert first[0].overall_status == "partial"
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True
    assert store.get("demo.base", "1.0.0").status == "standby"
    assert store.active_instance("demo.base").kind == "bundle"

    # Installing the identical version again keeps the soft standby record
    # instead of preempting the instance that already owns the id.
    repeated = await containers.install_sources(manager, store, [standalone], origin="user")

    assert repeated[0].overall_status == "partial"
    assert store.get("demo.base", "1.0.0").active is False
    assert store.get("demo.base", "1.0.0").status == "standby"
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True
    assert store.active_instance("demo.base").kind == "bundle"

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    disabled = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": False})
    )

    assert disabled.ok
    record = store.get("demo.base", "1.0.0")
    assert record is not None and record.active is True and record.status == "active"
    assert store.active_instance("demo.base").kind == "standalone"
    assert manager.active_packages()[0].root == manager.install_dir / "demo.base" / "1.0.0"

    reenabled = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": True})
    )

    assert reenabled.ok
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True
    assert store.get("demo.base", "1.0.0").status == "standby"
    assert store.active_instance("demo.base").kind == "bundle"
    assert manager.active_packages()[0].root == (
        bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0") / "plugins" / "demo.base"
    )


@pytest.mark.asyncio
async def test_bundle_install_ignores_member_content_of_other_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    (standalone / "extension.py").write_text(
        (standalone / "extension.py").read_text(encoding="utf-8") + "\n# different build\n",
        encoding="utf-8",
    )
    await containers.install_sources(manager, store, [standalone], origin="user")

    outcome = await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )

    assert outcome[0].container_id == "demo.bundle"
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.status == "standby" and member.active is False
    assert store.get("demo.base", "1.0.0").active is True
    assert store.active_instance("demo.base").kind == "standalone"


@pytest.mark.asyncio
async def test_bundle_upgrade_accepts_unchanged_member_version_with_new_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    old_root = _member(store, "demo.bundle", "1.0.0", "demo.base").root

    newer = _build_bundle_version(tmp_path / "bundle-next", "1.0.1")
    (newer / "plugins/demo.base/extension.py").write_text(
        METADATA_EXTENSION + "\n# converted build\n", encoding="utf-8",
    )
    outcome = await containers.install_sources(manager, store, [newer], origin="user")

    assert outcome[0].overall_status == "partial"
    member = _member(store, "demo.bundle", "1.0.1", "demo.base")
    assert member is not None and member.active is True
    claim = store.active_instance("demo.base")
    assert claim is not None and claim.bundle_version == "1.0.1"
    assert Path(claim.root) != Path(old_root)
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").status == "disabled"


@pytest.mark.asyncio
async def test_standalone_newer_version_takes_over_a_bundle_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    assert store.active_instance("demo.base").kind == "bundle"

    standalone = _write_member(
        tmp_path / "standalone", "demo.base", "agentserver", version="1.0.1",
    )
    outcome = await containers.install_sources(manager, store, [standalone], origin="user")

    assert outcome[0].overall_status == "enabled"
    assert store.get("demo.base", "1.0.1").active is True
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.status == "standby" and member.active is False
    assert store.active_instance("demo.base").kind == "standalone"


@pytest.mark.asyncio
async def test_standalone_same_id_and_version_with_different_content_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")
    await asyncio.to_thread(
        store.disable_instance, "demo.base", kind="standalone", version="1.0.0",
    )
    changed = _write_member(tmp_path / "standalone-2", "demo.base", "agentserver")
    (changed / "extension.py").write_text(
        METADATA_EXTENSION + "\n# different build\n", encoding="utf-8",
    )

    with pytest.raises(ExtensionFailure) as error:
        await containers.install_sources(manager, store, [changed], origin="user")

    assert error.value.status is StatusCode.EXTENSION_PACKAGE_EXISTS
    assert "different content" in error.value.message


@pytest.mark.asyncio
async def test_restart_scan_skips_bundle_roots_and_loads_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle(tmp_path / "source")], origin="user")

    restarted = ExtensionManager(manager.install_dir, report_path=tmp_path / "report-restart.yaml")
    await restarted.init(make_bus())
    containers.register_active_managed_roots(restarted)
    reports = await restarted.load_installed()

    assert not [report for report in reports if report.package_id == "demo.bundle" and report.status != "SUCCESS"]
    assert [record.package_id for record in restarted.active_packages()] == ["demo.base"]


@pytest.mark.asyncio
async def test_enabling_an_active_bundle_reactivates_a_failed_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    store.mark_bundle_member(
        "demo.bundle", "1.0.0", "demo.base", active=False, status="failed",
    )
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SET_ENABLED,
            {"package_id": "demo.bundle", "enable": True},
        )
    )

    assert response.ok
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True and member.status == "active"


@pytest.mark.asyncio
async def test_pending_standalone_newer_version_takes_over_a_bundle_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(containers, "_retry_timeout", lambda: 5.0)
    manager = await _manager(tmp_path, monkeypatch)
    try:
        store = ExtensionInstallState.for_install_dir(manager.install_dir)
        await containers.install_sources(
            manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
        )
        standalone = _write_member(
            tmp_path / "standalone", "demo.base", "agentserver",
            requires=[("demo.dep", ">=1.0.0")], version="1.0.1",
        )
        await containers.install_sources(manager, store, [standalone], origin="user")
        assert store.get("demo.base", "1.0.1").status == "pending"
        retry = containers._RETRY_TASKS["single:demo.base@1.0.1"]

        await containers.install_sources(
            manager, store, [_write_member(tmp_path / "dep", "demo.dep", "agentserver")], origin="user",
        )
        # A new active claim precedes the old member's final projection. Await
        # the whole retry, including that write, rather than an intermediate state.
        await asyncio.wait_for(asyncio.shield(retry), timeout=6.0)
        claim = store.active_instance("demo.base")
        assert claim is not None and claim.kind == "standalone" and claim.version == "1.0.1"
        member = _member(store, "demo.bundle", "1.0.0", "demo.base")
        assert member is not None and member.status == "standby" and member.active is False
        assert [record.version for record in manager.active_packages() if record.package_id == "demo.base"] == ["1.0.1"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_reload_keeps_a_standby_member_out_of_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_RELOAD, {"package_id": "demo.bundle"})
    )

    assert response.ok
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.status == "standby" and member.active is False
    claim = store.active_instance("demo.base")
    assert claim is not None and claim.kind == "standalone"
    assert store.get("demo.base", "1.0.0").active is True


@pytest.mark.asyncio
async def test_startup_recovery_keeps_a_standby_member_out_of_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_write_member(tmp_path / "standalone", "demo.base", "agentserver")],
        origin="user",
    )
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )

    await containers.recover_containers(manager)

    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.status == "standby" and member.active is False
    claim = store.active_instance("demo.base")
    assert claim is not None and claim.kind == "standalone"


@pytest.mark.asyncio
async def test_gateway_standalone_waiting_dependency_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    gateway = _write_member(
        tmp_path / "gateway", "demo.gw", "gateway", requires=[("demo.dep", ">=1.0.0")],
    )
    await containers.install_sources(manager, store, [gateway], origin="user", timeout=5)
    assert store.get("demo.gw").status == "pending"

    await containers.install_sources(
        manager, store, [_write_member(tmp_path / "dep", "demo.dep", "agentserver")], origin="user",
    )
    await asyncio.sleep(1.0)

    record = store.get("demo.gw")
    assert record is not None and record.status == "pending"
    claim = store.active_instance("demo.gw")
    assert claim is not None and claim.kind == "standalone" and claim.version == "1.0.0"


async def _answer_gateway_package(
    store: ExtensionInstallState,
    package_id: str,
    version: str,
    *,
    error: str = "",
) -> None:
    """Stand in for the Gateway loading a pending standalone copy."""

    for _ in range(1000):
        await asyncio.sleep(0.01)
        claim = store.active_instance(package_id)
        if claim is None or claim.kind != "standalone" or claim.version != version:
            continue
        record = store.get(package_id, version)
        if record is None or record.status != "pending":
            continue
        if error:
            store.mark(
                package_id, version=version, active=False, enabled=False,
                status="failed", error=error,
            )
            return
        store.mark(package_id, version=version, active=True, enabled=True, status="active")
        store.switch_active_instance(
            ActiveInstance(package_id=package_id, version=version, kind="standalone"),
        )
        return
    raise AssertionError(f"gateway package {package_id}@{version} never became pending")


@pytest.mark.asyncio
async def test_gateway_standalone_switch_reports_a_failed_load_and_restores_the_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store,
        [_write_member(tmp_path / "v1", "demo.gw", "gateway", version="1.0.0")],
        origin="user", timeout=5,
    )
    _simulate_gateway(store, "demo.gw", "1.0.0")

    answer = asyncio.create_task(
        _answer_gateway_package(
            store, "demo.gw", "2.0.0", error="extension load failed, reason='No module named demo'",
        ),
    )
    outcomes = await containers.install_sources(
        manager, store,
        [_write_member(tmp_path / "v2", "demo.gw", "gateway", version="2.0.0")],
        origin="user", timeout=5, require_all_members=True,
    )
    await answer

    assert outcomes[0].overall_status == "failed"
    assert [report.status for report in outcomes[0].reports] == ["FAILED"]
    assert outcomes[0].reports[0].error.message == "No module named demo"
    assert store.get("demo.gw", "2.0.0").status == "failed"
    assert (manager.install_dir / "demo.gw" / "2.0.0").is_dir()
    previous = store.get("demo.gw", "1.0.0")
    assert previous is not None and previous.active is True and previous.status == "active"
    claim = store.active_instance("demo.gw")
    assert claim is not None and claim.version == "1.0.0"


@pytest.mark.asyncio
async def test_gateway_first_install_failure_removes_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    answer = asyncio.create_task(
        _answer_gateway_package(store, "demo.gw", "1.0.0", error="extension load failed, reason='No module named demo'"),
    )
    outcomes = await containers.install_sources(
        manager, store,
        [_write_member(tmp_path / "gateway", "demo.gw", "gateway")],
        origin="user", timeout=5, require_all_members=True,
    )
    await answer

    assert outcomes[0].overall_status == "failed"
    assert outcomes[0].reports[0].error.message == "No module named demo"
    assert store.get("demo.gw", "1.0.0") is None
    assert store.active_instance("demo.gw") is None
    assert not (manager.install_dir / "demo.gw" / "1.0.0").exists()


@pytest.mark.asyncio
async def test_gateway_standalone_switch_waits_for_the_gateway_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store,
        [_write_member(tmp_path / "v1", "demo.gw", "gateway", version="1.0.0")],
        origin="user", timeout=5,
    )
    _simulate_gateway(store, "demo.gw", "1.0.0")

    answer = asyncio.create_task(_answer_gateway_package(store, "demo.gw", "2.0.0"))
    outcomes = await containers.install_sources(
        manager, store,
        [_write_member(tmp_path / "v2", "demo.gw", "gateway", version="2.0.0")],
        origin="user", timeout=5, require_all_members=True,
    )
    await answer

    assert outcomes[0].overall_status == "enabled"
    assert [report.status for report in outcomes[0].reports] == ["SUCCESS"]
    record = store.get("demo.gw", "2.0.0")
    assert record is not None and record.active is True and record.status == "active"


@pytest.mark.asyncio
async def test_uninstalling_a_gateway_standalone_marks_the_copy_for_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_write_member(tmp_path / "gateway", "demo.gw", "gateway")], origin="user",
    )
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_UNINSTALL,
            {"package_id": "demo.gw", "version": "1.0.0"},
        )
    )

    assert response.ok
    record = store.get("demo.gw", "1.0.0")
    assert record is not None and record.status == "uninstalling"
    assert record.enabled is False and record.active is False


@pytest.mark.asyncio
async def test_reinstalling_a_bundle_version_drops_removed_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_with_members(
        tmp_path / "v1", "1.0.0",
        [("demo.base", None), ("demo.old", None)],
    )
    await containers.install_sources(manager, store, [first], origin="user")
    assert store.get_bundle_member("demo.bundle", "1.0.0", "demo.old") is not None

    second = _build_bundle_with_members(
        tmp_path / "v2", "1.0.0",
        [("demo.base", None)],
    )
    outcome = await containers.install_sources(manager, store, [second], origin="user")

    assert outcome[0].overall_status == "enabled"
    assert store.get_bundle_member("demo.bundle", "1.0.0", "demo.old") is None
    assert store.active_instance("demo.old") is None


@pytest.mark.asyncio
async def test_batch_rejects_a_cycle_across_two_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_with_members(
        tmp_path / "one", "1.0.0", [("demo.one", [("demo.two", ">=1.0.0")])],
    )
    second = _build_bundle_with_members(
        tmp_path / "two", "1.0.0", [("demo.two", [("demo.one", ">=1.0.0")])],
    )
    for root, bundle_id in ((first, "demo.one.bundle"), (second, "demo.two.bundle")):
        document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        document["package"]["id"] = bundle_id
        (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ExtensionFailure) as error:
        await containers.install_sources(manager, store, [first, second], origin="user")

    assert error.value.status is StatusCode.EXTENSION_DEPENDENCY_CYCLE
    assert not bundle_version_dir(manager.install_dir, "demo.one.bundle", "1.0.0").exists()


@pytest.mark.asyncio
async def test_show_resolves_a_bundle_member_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    adapter = PackageManagementAdapter()

    response = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SHOW,
            {"package_id": "demo.base", "version": "1.0.0"},
        )
    )

    assert response.ok
    assert response.payload["package"]["packageId"] == "demo.base"


async def _answer_gateway_member(
    store: ExtensionInstallState,
    package_id: str,
    *,
    error: str = "",
) -> None:
    """Stand in for the Gateway loading a pending bundle member."""

    for _ in range(1000):
        await asyncio.sleep(0.01)
        for bundle in store.bundles():
            for member in bundle.member_records:
                if member.package_id != package_id or member.status != "pending":
                    continue
                if error:
                    store.mark_bundle_member(
                        bundle.bundle_id, bundle.version, package_id,
                        enabled=False, active=False, status="failed", error=error,
                    )
                    return
                store.mark_bundle_member(
                    bundle.bundle_id, bundle.version, package_id,
                    enabled=True, active=True, status="active",
                )
                store.switch_active_instance(
                    ActiveInstance(
                        package_id=package_id,
                        version=member.version,
                        kind="bundle",
                        bundle_id=bundle.bundle_id,
                        bundle_version=bundle.version,
                        root=member.root,
                    )
                )
                return
    raise AssertionError(f"gateway member {package_id} never became pending")


@pytest.mark.asyncio
async def test_require_all_members_succeeds_after_the_gateway_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    answer = asyncio.create_task(_answer_gateway_member(store, "demo.top"))

    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
        require_all_members=True,
    )
    await answer

    assert outcomes[0].overall_status == "enabled"
    assert _member(store, "demo.bundle", "2.0.0", "demo.base").active is True
    assert _member(store, "demo.bundle", "2.0.0", "demo.top").active is True
    record = store.get_bundle("demo.bundle")
    assert record is not None and record.active is True


@pytest.mark.asyncio
async def test_require_all_members_fails_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    answer = asyncio.create_task(
        _answer_gateway_member(store, "demo.top", error="member load failed"),
    )

    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
        require_all_members=True,
    )
    await answer

    assert outcomes[0].overall_status == "failed"
    assert [report.status for report in outcomes[0].reports] == ["SUCCESS", "FAILED"]
    assert {member["packageId"]: member["status"] for member in outcomes[0].members} == {
        "demo.base": "failed",
        "demo.top": "failed",
    }
    record = store.get_bundle("demo.bundle")
    assert record is not None and record.status == "uninstalling" and record.active is False
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0").is_dir()
    assert store.active_instance("demo.base") is None
    assert store.active_instance("demo.top") is None


@pytest.mark.asyncio
async def test_bundle_member_never_projects_as_a_standalone_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetSummary

    from jiuwenswarm.extensions_v2.management.hub_catalog import hub_card

    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle(tmp_path / "source")], origin="user")
    _simulate_gateway(store, "demo.top")
    member = _member(store, "demo.bundle", "2.0.0", "demo.top")
    assert member is not None
    member_root = Path(member.root)
    state = SimpleNamespace(
        active=[SimpleNamespace(package_id="demo.top", version="1.0.0", root=member_root)],
        installed=[member_root],
    )

    index = catalog.build_state_index(state, store)
    assert "demo.top" not in index.active
    assert "demo.top" not in index.installed

    cards = catalog.list_mine(tmp_path / "examples", state, install_dir=manager.install_dir)
    assert [card["id"] for card in cards] == ["demo.bundle"]

    card = hub_card(
        HubAssetSummary(
            kind="system_plugin",
            asset_id="asset-top",
            display_name="Top",
            short_description="",
            public_latest_version="1.0.0",
            icon_uri="",
            tags=(),
            package_name="demo.top",
        ),
        index,
        store,
    )
    assert (card["installed"], card["active"], card["enabled"]) == (False, False, True)


@pytest.mark.asyncio
async def test_reload_accepts_a_managed_bundle_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(manager, store, [_build_bundle(tmp_path / "source")], origin="user")

    report = await manager.reload("demo.base")

    assert report.status == "SUCCESS"
    assert _member(store, "demo.bundle", "2.0.0", "demo.base").status == "active"


@pytest.mark.asyncio
async def test_require_all_members_keeps_a_same_version_conflict_as_standby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    store.activate(
        ExtensionInstallRecord(
            package_id="demo.top", version="1.0.0", origin="user",
            active=True, enabled=True, domain="gateway", status="active",
        )
    )
    store.switch_active_instance(
        ActiveInstance(package_id="demo.top", version="1.0.0", kind="standalone"),
    )

    outcomes = await containers.install_sources(
        manager, store, [_build_bundle(tmp_path / "source")], origin="user",
        require_all_members=True,
    )

    assert outcomes[0].overall_status == "partial"
    assert store.get("demo.top").active is True
    assert store.active_instance("demo.top").kind == "standalone"
    record = store.get_bundle("demo.bundle")
    assert record is not None and record.active is True
    member = _member(store, "demo.bundle", "2.0.0", "demo.top")
    assert member is not None and member.status == "standby" and member.active is False
    base = _member(store, "demo.bundle", "2.0.0", "demo.base")
    assert base is not None and base.active is True


@pytest.mark.asyncio
async def test_failed_reinstall_keeps_the_healthy_active_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    source = _build_bundle(tmp_path / "source")
    await containers.install_sources(manager, store, [source], origin="user")
    _simulate_gateway(store, "demo.top")

    original_enable = manager.enable_version
    calls = 0

    async def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("load failed")
        return await original_enable(*args, **kwargs)

    monkeypatch.setattr(manager, "enable_version", fail)
    outcomes = await containers.install_sources(manager, store, [source], origin="user")

    assert outcomes[0].overall_status == "failed"
    assert store.get_bundle("demo.bundle", "2.0.0") is not None
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "2.0.0").is_dir()


@pytest.mark.asyncio
async def test_same_version_standalone_install_is_a_standby_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")

    outcomes = await containers.install_sources(manager, store, [standalone], origin="user")

    assert outcomes[0].overall_status == "partial"
    record = store.get("demo.base", "1.0.0")
    assert record.status == "standby" and record.active is False
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True

    state = await manager.query()
    cards = catalog.list_mine(tmp_path / "examples", state, install_dir=manager.install_dir)
    card = next(item for item in cards if item["id"] == "demo.base")
    assert (card["installed"], card["active"], card["enabled"], card["overallStatus"]) == (
        True, False, True, "enabled",
    )
    assert any(item["id"] == "demo.bundle" for item in cards)


@pytest.mark.asyncio
async def test_startup_turns_a_shadowed_failed_standalone_into_standby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    store.upsert(
        ExtensionInstallRecord(
            package_id="demo.base",
            version="1.0.0",
            origin="hub",
            install_path=str(manager.install_dir / "demo.base" / "1.0.0"),
            active=False,
            enabled=True,
            domain="agentserver",
            status="failed",
            error="install store has no workswarm-theme 2.0.0",
        )
    )

    await containers.recover_containers(manager)

    record = store.get("demo.base", "1.0.0")
    assert record is not None
    assert (record.status, record.active, record.enabled, record.error) == (
        "standby", False, True, "",
    )
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").active is True


@pytest.mark.asyncio
async def test_disabling_a_bundle_promotes_the_same_version_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": False})
    )

    assert response.ok
    record = store.get("demo.base", "1.0.0")
    assert record is not None and record.active is True and record.status == "active"
    assert store.active_instance("demo.base").kind == "standalone"
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is False
    assert manager.active_packages()[0].root == manager.install_dir / "demo.base" / "1.0.0"


@pytest.mark.asyncio
async def test_disabling_a_standalone_promotes_the_standby_bundle_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": False})
    )
    await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": True})
    )
    enabled = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.base", "enable": True})
    )
    assert enabled.payload["reports"][0]["status"] == "SUCCESS"
    assert store.active_instance("demo.base").kind == "standalone"
    assert _member(store, "demo.bundle", "1.0.0", "demo.base").status == "standby"
    state = await manager.query()
    cards = catalog.list_mine(tmp_path / "examples", state, install_dir=manager.install_dir)
    card = next(item for item in cards if item["id"] == "demo.base")
    assert card["enabled"] is True
    manifest = manager.install_dir / "demo.base" / "1.0.0" / "manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["enable"] is True

    disabled = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.base", "enable": False})
    )

    assert disabled.ok
    assert store.get("demo.base", "1.0.0").status == "disabled"
    assert json.loads(manifest.read_text(encoding="utf-8"))["enable"] is False
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True and member.status == "active"
    assert store.active_instance("demo.base").kind == "bundle"
    assert manager.active_packages()[0].root == (
        bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0") / "plugins" / "demo.base"
    )


@pytest.mark.asyncio
async def test_disabling_a_bundle_promotes_a_gateway_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.top", "gateway")
    await containers.install_sources(manager, store, [standalone], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_SET_ENABLED, {"package_id": "demo.bundle", "enable": False})
    )

    assert response.ok
    record = store.get("demo.top", "1.0.0")
    assert record is not None and record.active is True and record.status == "pending"
    assert store.active_instance("demo.top").kind == "standalone"
    member = _member(store, "demo.bundle", "1.0.0", "demo.top")
    assert member is not None and member.active is False


@pytest.mark.asyncio
async def test_uninstalling_a_bundle_promotes_the_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.bundle"})
    )

    assert response.ok
    removed = store.get_bundle("demo.bundle")
    assert removed is not None and removed.status == "uninstalling"
    record = store.get("demo.base", "1.0.0")
    assert record is not None and record.active is True and record.status == "active"
    assert store.active_instance("demo.base").kind == "standalone"
    assert manager.active_packages()[0].root == manager.install_dir / "demo.base" / "1.0.0"


@pytest.mark.asyncio
async def test_uninstalling_an_agentserver_only_bundle_removes_it_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    source = _build_bundle_with_members(tmp_path / "bundle", "1.0.0", [("demo.one", None)])
    await containers.install_sources(manager, store, [source], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.bundle"})
    )

    assert response.ok
    assert store.get_bundle("demo.bundle") is None
    assert store.bundle_members("demo.bundle") == []
    assert not bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").exists()


@pytest.mark.asyncio
async def test_uninstalling_a_standalone_copy_keeps_a_same_version_member_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")
    standalone_root = manager.install_dir / "demo.base" / "1.0.0"
    member_root = (
        bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0") / "plugins" / "demo.base"
    )
    assert standalone_root.is_dir()
    assert store.get("demo.base", "1.0.0").status == "standby"
    assert store.active_instance("demo.base").kind == "bundle"

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(
            ReqMethod.EXTENSIONS_V2_UNINSTALL,
            {"package_id": "demo.base", "version": "1.0.0"},
        )
    )

    assert response.ok
    assert not standalone_root.exists()
    assert member_root.is_dir()
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True and member.status == "active"
    assert store.active_instance("demo.base").kind == "bundle"
    assert manager.active_packages()[0].root == member_root
    assert store.get("demo.base", "1.0.0") is None


@pytest.mark.asyncio
async def test_set_enabled_with_version_targets_that_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")
    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")
    await containers.install_sources(manager, store, [first], origin="user")
    assert store.active_bundle("demo.bundle").version == "1.0.0"

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    disabled = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SET_ENABLED,
            {"package_id": "demo.bundle", "enable": False, "version": "1.0.0"},
        )
    )
    assert disabled.ok
    assert store.get_bundle("demo.bundle", "1.0.0").active is False
    assert store.get_bundle("demo.bundle", "1.0.1").active is False

    enabled = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SET_ENABLED,
            {"package_id": "demo.bundle", "enable": True, "version": "1.0.0"},
        )
    )

    assert enabled.ok
    assert store.active_bundle("demo.bundle").version == "1.0.0"
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True
    assert [record.version for record in manager.active_packages()] == ["1.0.0"]

    missing = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SET_ENABLED,
            {"package_id": "demo.bundle", "enable": True, "version": "9.9.9"},
        )
    )
    assert not missing.ok
    assert missing.payload["code"] == "PACKAGE_NOT_FOUND"


@pytest.mark.asyncio
async def test_reimporting_an_installed_bundle_version_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    archive = archive_package(_build_bundle_version(tmp_path / "bundle", "1.0.0"))

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    first = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive)]})
    )
    again = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_INSTALL, {"paths": [str(archive)]})
    )

    assert first.ok
    assert again.ok
    failed = again.payload["reports"][0]
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == StatusCode.EXTENSION_PACKAGE_EXISTS.name
    assert "already installed" in failed["error"]["message"]
    assert manager.active_packages()[0].version == "1.0.0"


@pytest.mark.asyncio
async def test_restart_show_follows_the_loaded_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")
    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")
    await containers.install_sources(manager, store, [first], origin="user")

    restarted = ExtensionManager(manager.install_dir, report_path=tmp_path / "report-restart.yaml")
    await restarted.init(make_bus())
    containers.register_active_managed_roots(restarted)
    await restarted.load_installed()
    state = await restarted.query()

    entry = catalog.find_entry(
        "demo.bundle",
        tmp_path / "examples",
        state,
        install_dir=restarted.install_dir,
        accepted_roots=restarted.accepted_roots("demo.bundle"),
    )
    detail = catalog.project_detail(entry, state, install_dir=restarted.install_dir)

    assert entry.version == "1.0.0"
    assert detail["version"] == "1.0.0"
    assert detail["installedVersion"] == "1.0.0"
    assert detail["overallStatus"] != "disabled"


@pytest.mark.asyncio
async def test_uninstalling_every_standalone_version_keeps_the_member_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")
    older = _write_member(tmp_path / "standalone-old", "demo.base", "agentserver", version="0.9.0")
    older_root = manager.install_dir / "demo.base" / "0.9.0"
    older_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(older, older_root)

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.base"})
    )

    assert response.ok
    assert not (manager.install_dir / "demo.base").exists()
    member_root = (
        bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0") / "plugins" / "demo.base"
    )
    assert member_root.is_dir()
    assert member_root.resolve() in manager.accepted_roots("demo.base")
    assert store.active_instance("demo.base").kind == "bundle"
    assert manager.active_packages()[0].root == member_root


@pytest.mark.asyncio
async def test_file_preview_reads_the_requested_bundle_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    first = _build_bundle_version(tmp_path / "v1", "1.0.0")
    (first / "only_v1.py").write_text("VERSION = 1\n", encoding="utf-8")
    second = _build_bundle_version(tmp_path / "v2", "1.0.1")
    (second / "only_v2.py").write_text("VERSION = 2\n", encoding="utf-8")
    await containers.install_sources(manager, store, [first], origin="user")
    await containers.install_sources(manager, store, [second], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    older_tree = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_FILE_LIST, {"package_id": "demo.bundle", "version": "1.0.0"})
    )
    newer_tree = await adapter.handle(
        _request(ReqMethod.EXTENSIONS_V2_FILE_LIST, {"package_id": "demo.bundle", "version": "1.0.1"})
    )
    older_read = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_FILE_READ,
            {"package_id": "demo.bundle", "path": "only_v1.py", "version": "1.0.0"},
        )
    )
    wrong_read = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_FILE_READ,
            {"package_id": "demo.bundle", "path": "only_v1.py", "version": "1.0.1"},
        )
    )

    assert older_tree.ok and "only_v1.py" in json.dumps(older_tree.payload)
    assert newer_tree.ok and "only_v2.py" in json.dumps(newer_tree.payload)
    assert "only_v1.py" not in json.dumps(newer_tree.payload)
    assert older_read.ok and "VERSION = 1" in json.dumps(older_read.payload)
    assert not wrong_read.ok


@pytest.mark.asyncio
async def test_uninstalling_a_member_only_id_reports_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    await containers.install_sources(
        manager, store, [_build_bundle_version(tmp_path / "bundle", "1.0.0")], origin="user",
    )

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    response = await PackageManagementAdapter().handle(
        _request(ReqMethod.EXTENSIONS_V2_UNINSTALL, {"package_id": "demo.base"})
    )

    assert response.ok
    failed = response.payload["reports"][0]
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == StatusCode.EXTENSION_PACKAGE_NOT_INSTALLED.name
    member = _member(store, "demo.bundle", "1.0.0", "demo.base")
    assert member is not None and member.active is True
    assert bundle_version_dir(manager.install_dir, "demo.bundle", "1.0.0").is_dir()


@pytest.mark.asyncio
async def test_versioned_enable_is_rejected_for_a_standalone_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await _manager(tmp_path, monkeypatch)
    store = ExtensionInstallState.for_install_dir(manager.install_dir)
    standalone = _write_member(tmp_path / "standalone", "demo.base", "agentserver")
    await containers.install_sources(manager, store, [standalone], origin="user")

    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter

    adapter = PackageManagementAdapter()
    enabled = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_SET_ENABLED,
            {"package_id": "demo.base", "enable": False, "version": "1.0.0"},
        )
    )
    reloaded = await adapter.handle(
        _request(
            ReqMethod.EXTENSIONS_V2_RELOAD,
            {"package_id": "demo.base", "version": "1.0.0"},
        )
    )

    assert not enabled.ok and enabled.payload["code"] == "INVALID_PARAMS"
    assert not reloaded.ok and reloaded.payload["code"] == "INVALID_PARAMS"
    assert store.get("demo.base", "1.0.0").active is True
