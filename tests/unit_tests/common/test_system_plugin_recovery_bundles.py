# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bundle-aware recovery snapshots, restore and ledger synchronization."""

from __future__ import annotations

import json
from pathlib import Path

import portalocker
import pytest

from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery


def _enabled(root: Path) -> bool:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))["enable"]


def _set_enabled(root: Path, enabled: bool) -> None:
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    document["enable"] = enabled
    (root / "manifest.json").write_text(json.dumps(document), encoding="utf-8")


def _write_package(root: Path, package_id: str, version: str, enabled: bool) -> Path:
    package = root / package_id / version
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "package": {"id": package_id, "version": version, "domain": "agentserver"},
                "enable": enabled,
            }
        ),
        encoding="utf-8",
    )
    return package


def _write_bundle(
    root: Path,
    bundle_id: str,
    bundle_version: str,
    members: list[tuple[str, str, bool]],
) -> dict[str, Path]:
    bundle = root / bundle_id / bundle_version
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "extension",
                "schemaVersion": 1,
                "is_bundle": True,
                "package": {"id": bundle_id, "version": bundle_version},
                "members": [{"path": f"plugins/{package_id}"} for package_id, _, _ in members],
            }
        ),
        encoding="utf-8",
    )
    roots: dict[str, Path] = {}
    for package_id, version, enabled in members:
        member = bundle / "plugins" / package_id
        member.mkdir(parents=True, exist_ok=True)
        (member / "manifest.json").write_text(
            json.dumps(
                {
                    "package_type": "extension",
                    "schemaVersion": 1,
                    "package": {"id": package_id, "version": version, "domain": "gateway"},
                    "enable": enabled,
                }
            ),
            encoding="utf-8",
        )
        roots[package_id] = member
    return roots


def _bundle_record(
    bundle_id: str,
    bundle_version: str,
    members: list[tuple[str, str, bool]],
    roots: dict[str, Path],
    *,
    enabled: bool,
) -> dict:
    return {
        "bundle_id": bundle_id,
        "version": bundle_version,
        "origin": "user",
        "members": [
            {"package_id": package_id, "version": version, "domain": "gateway"}
            for package_id, version, _ in members
        ],
        "member_records": [
            {
                "package_id": package_id,
                "version": version,
                "domain": "gateway",
                "root": str(roots[package_id]),
                "enabled": enabled,
                "active": enabled,
                "status": "active" if enabled else "disabled",
            }
            for package_id, version, _ in members
        ],
        "status": "active" if enabled else "disabled",
        "active": enabled,
    }


def _write_install_state(data_dir: Path, *, packages=(), bundles=()) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "system_plugin_install_state.json").write_text(
        json.dumps({"packages": list(packages), "bundles": list(bundles)}),
        encoding="utf-8",
    )


def _read_install_state(data_dir: Path) -> dict:
    return json.loads((data_dir / "system_plugin_install_state.json").read_text(encoding="utf-8"))


def _history(data_dir: Path) -> dict:
    return json.loads((data_dir / "system_plugin_recovery.json").read_text(encoding="utf-8"))


def test_snapshot_includes_bundle_members_but_not_the_bundle_root(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    members = [("demo.a", "1.0.0", True), ("demo.b", "2.0.0", False)]
    roots = _write_bundle(install_dir, "demo.bundle", "3.0.0", members)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=True)],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)

    recovery.begin_session("session-one")

    snapshot = recovery._scan_packages()
    assert [(item["package_id"], item["version"]) for item in snapshot] == [
        ("demo.a", "1.0.0"),
        ("demo.b", "2.0.0"),
    ]
    assert all(item["bundle_id"] == "demo.bundle" for item in snapshot)
    assert all(item["bundle_version"] == "3.0.0" for item in snapshot)


def test_restore_disables_bundle_members_and_syncs_the_ledger(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    members = [("demo.a", "1.0.0", False), ("demo.b", "2.0.0", False)]
    roots = _write_bundle(install_dir, "demo.bundle", "3.0.0", members)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=False)],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")

    for root in roots.values():
        _set_enabled(root, True)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=True)],
    )

    recovery.restore(startup["id"])

    for root in roots.values():
        assert _enabled(root) is False
    state = _read_install_state(data_dir)
    for record in state["bundles"][0]["member_records"]:
        assert (record["enabled"], record["active"], record["status"]) == (False, False, "disabled")
    assert (state["bundles"][0]["active"], state["bundles"][0]["status"]) == (False, "disabled")


def test_restore_enables_bundle_members_as_pending(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    members = [("demo.a", "1.0.0", True), ("demo.b", "2.0.0", True)]
    roots = _write_bundle(install_dir, "demo.bundle", "3.0.0", members)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=True)],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")

    for root in roots.values():
        _set_enabled(root, False)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=False)],
    )

    recovery.restore(startup["id"])

    for root in roots.values():
        assert _enabled(root) is True
    state = _read_install_state(data_dir)
    for record in state["bundles"][0]["member_records"]:
        assert (record["enabled"], record["active"], record["status"]) == (True, False, "pending")
    assert (state["bundles"][0]["active"], state["bundles"][0]["status"]) == (True, "pending")


def test_legacy_bundle_root_snapshot_applies_to_every_member(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    members = [("demo.a", "1.0.0", True), ("demo.b", "2.0.0", True)]
    roots = _write_bundle(install_dir, "demo.bundle", "3.0.0", members)
    _write_install_state(
        data_dir,
        bundles=[_bundle_record("demo.bundle", "3.0.0", members, roots, enabled=True)],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")
    document = _history(data_dir)
    document["records"][-1]["snapshot"] = [
        {"package_id": "demo.bundle", "version": "3.0.0", "enabled": False},
    ]
    (data_dir / "system_plugin_recovery.json").write_text(json.dumps(document), encoding="utf-8")

    recovery.restore(startup["id"])

    for root in roots.values():
        assert _enabled(root) is False


def test_settling_fills_an_empty_startup_record_after_initial_installs(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    recovery = SystemPluginRecovery(install_dir, data_dir)
    recovery.begin_session("session-one")
    assert _history(data_dir)["records"][-1]["snapshot"] == []

    package = _write_package(install_dir, "demo.first", "1.0.0", True)
    recovery.record("install", ["demo.first"])

    snapshot = _history(data_dir)["records"][-2]["snapshot"]
    assert [(item["package_id"], item["version"], item["enabled"]) for item in snapshot] == [
        ("demo.first", "1.0.0", True),
    ]
    assert _enabled(package) is True


def test_restore_keeps_standalone_and_bundle_member_states_separate(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    standalone = _write_package(install_dir, "demo.gw", "1.0.0", True)
    roots = _write_bundle(
        install_dir, "demo.bundle", "1.0.0", [("demo.gw", "1.0.0", False)],
    )
    _write_install_state(
        data_dir,
        packages=[
            {
                "package_id": "demo.gw", "version": "1.0.0", "origin": "user",
                "install_path": str(standalone), "domain": "gateway",
                "enabled": True, "active": True, "status": "active",
            },
        ],
        bundles=[_bundle_record("demo.bundle", "1.0.0", [("demo.gw", "1.0.0", False)], roots, enabled=False)],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")

    # Current state is the opposite of the recorded one: the standalone copy is
    # disabled while the bundle member (same id and version) is enabled.
    _set_enabled(standalone, False)
    _set_enabled(roots["demo.gw"], True)
    state = _read_install_state(data_dir)
    state["packages"][0].update(enabled=False, active=False, status="disabled")
    state["bundles"][0]["member_records"][0].update(enabled=True, active=True, status="active")
    state["bundles"][0].update(active=True, status="active")
    _write_install_state(
        data_dir, packages=state["packages"], bundles=state["bundles"],
    )

    recovery.restore(startup["id"])

    assert _enabled(standalone) is True
    assert _enabled(roots["demo.gw"]) is False
    state = _read_install_state(data_dir)
    assert state["packages"][0]["enabled"] is True
    assert state["bundles"][0]["member_records"][0]["enabled"] is False


def test_restore_preserves_an_uninstalling_tombstone(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    package = _write_package(install_dir, "demo.one", "1.0.0", True)
    _write_install_state(
        data_dir,
        packages=[
            {
                "package_id": "demo.one", "version": "1.0.0", "origin": "user",
                "install_path": str(package), "domain": "gateway",
                "enabled": False, "active": False, "status": "uninstalling", "error": "busy",
            },
        ],
    )
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")

    recovery.restore(startup["id"])

    assert _enabled(package) is True
    state = _read_install_state(data_dir)
    assert (state["packages"][0]["status"], state["packages"][0]["error"]) == ("uninstalling", "busy")


def test_restore_rolls_back_manifests_when_the_ledger_lock_times_out(
    tmp_path: Path, monkeypatch,
) -> None:
    install_dir = tmp_path / "extension_pkgs"
    data_dir = tmp_path / "extension"
    package = _write_package(install_dir, "demo.one", "1.0.0", True)
    _write_install_state(data_dir)
    recovery = SystemPluginRecovery(install_dir, data_dir)
    startup = recovery.begin_session("session-one")

    _set_enabled(package, False)

    def fail_lock(*_args):
        raise portalocker.LockException("ledger is busy")

    monkeypatch.setattr(recovery, "_sync_install_state", fail_lock)

    with pytest.raises(portalocker.LockException):
        recovery.restore(startup["id"])

    assert _enabled(package) is False, "the manifest write must be rolled back"
    assert [record["action"] for record in recovery.records()] == ["startup"]
