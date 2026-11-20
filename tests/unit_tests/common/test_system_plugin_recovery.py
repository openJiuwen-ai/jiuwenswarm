# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import system_plugin_recovery
from jiuwenswarm.common.system_plugin_recovery import SystemPluginRecovery
from jiuwenswarm.common.system_plugin_recovery_sidecar import _RecoveryHandler, _page, _parent_origin


def _write_package(root: Path, package_id: str, version: str, enabled: bool) -> Path:
    package = root / package_id / version
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_type": "extension",
        "schemaVersion": 1,
        "package": {"id": package_id, "version": version, "domain": "agentserver"},
        "enable": enabled,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package


def _enabled(package: Path) -> bool:
    document = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    return document["enable"]


def test_recovery_page_keeps_its_confirmation_dialog() -> None:
    page = _page("token", "zh")

    assert '<button id="confirm-restore" class="danger" type="button"></button>' in page
    assert ".dialog-actions .danger { background: var(--danger); }" in page
    assert "RECOVERY_DIALOG_POSITION_MESSAGE" in page
    assert "function updateDialogPosition(centerY)" in page
    assert "body.is-embedded .page-header { display: none; }" in page
    assert "status.textContent = texts.restoring;" not in page
    assert "status.textContent = texts.restored;" not in page
    assert '"restoring":' not in page
    assert '"restored":' not in page
    assert "let restoring = false;" in page
    assert "restoring: restoring," in page
    assert "if (restoring || confirmDialog.open) return;" in page
    assert "restoring = true;" in page
    assert "restoring = false;" in page


def test_recovery_page_renders_collapsible_details() -> None:
    page = _page("token", "zh")

    assert '<button id="restore" type="button"></button>' in page
    assert ".record-detail {\n  display: none;" in page
    assert ".record-detail.is-open {\n  display: grid;" in page
    assert ".chevron::before" in page
    assert "flex: 1 1 auto;" in page
    assert "width: auto;" in page
    assert 'font: 400 14px/1.55 "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;' in page
    assert "#status:empty { display: none; }" in page
    assert ".record-selector" in page
    assert "selector.type = 'radio';" in page
    assert "selector.name = 'recovery-record';" in page
    assert "selector.onchange = () => selectRecord(record.id);" in page
    assert ".record-detail.is-open { grid-template-columns: 1fr; gap: 14px; }" in page
    assert "function toggleDetail(item, toggle, detail, record, packages)" in page
    assert "void loadPreview(record.id, packages, detail);" in page
    assert "/api/preview?token=${query}&record_id=${recordQuery}" in page
    assert "detail.hidden" not in page
    assert "restoreButton.onclick = () => {" in page
    assert "if (selectedRecordId) confirmRestore(selectedRecordId, restoreButton);" in page
    assert "RECOVERY_SELECTED_RECORD_MESSAGE" in page
    assert "detail.append(detailCopy);" in page
    assert "detail.append(detailCopy, restoreButton);" not in page
    assert "header.append(selector, toggle);" in page
    assert "item.append(header, detail);" in page
    assert "item.append(copy, button);" not in page
    assert '<button id="restore" type="button"></button>' in page
    assert ".restore-actions" not in page
    assert 'records.replaceChildren(...data.records.map((record) => row(record)));' in page
    assert 'RECORDS_PER_PAGE' not in page
    assert 'new ResizeObserver(scheduleContentHeight).observe(page);' in page
    assert 'jiuwenswarm.system-plugin-recovery.height' in page


def test_parent_origin_rejects_non_origin_urls() -> None:
    assert _parent_origin("https://example.com") == "https://example.com"
    assert _parent_origin("http://127.0.0.1:3000") == "http://127.0.0.1:3000"
    assert _parent_origin("https://example.com/path") == ""
    assert _parent_origin("javascript:alert(1)") == ""


def test_restore_disables_packages_installed_after_the_selected_record(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    first = _write_package(install_dir, "demo.first", "1.0.0", True)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    startup = recovery.begin_session("session-one")
    second = _write_package(install_dir, "demo.second", "1.0.0", True)
    recovery.record("install", ["demo.second"])

    restored = recovery.restore(startup["id"])

    assert restored["action"] == "restore"
    assert _enabled(first) is True
    assert _enabled(second) is False
    assert [record["action"] for record in recovery.records()] == ["restore", "install", "startup"]


def test_preview_matches_the_restore_result_for_currently_installed_packages(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    retained = _write_package(install_dir, "demo.retained", "1.0.0", True)
    removed = _write_package(install_dir, "demo.removed", "1.0.0", True)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    startup = recovery.begin_session("session-one")
    shutil.rmtree(removed)
    installed_later = _write_package(install_dir, "demo.installed-later", "1.0.0", True)

    preview = recovery.preview(startup["id"])

    assert preview == [
        {"package_id": "demo.installed-later", "version": "1.0.0", "enabled": False},
        {"package_id": "demo.retained", "version": "1.0.0", "enabled": True},
    ]
    assert _enabled(installed_later) is True
    assert [record["action"] for record in recovery.records()] == ["startup"]
    recovery.restore(startup["id"])
    assert _enabled(retained) is True
    assert _enabled(installed_later) is False


def test_restore_rolls_back_written_manifests_when_a_later_write_fails(tmp_path: Path, monkeypatch) -> None:
    install_dir = tmp_path / "extension_pkgs"
    first = _write_package(install_dir, "demo.first", "1.0.0", False)
    second = _write_package(install_dir, "demo.second", "1.0.0", True)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    startup = recovery.begin_session("session-one")
    _write_package(install_dir, "demo.first", "1.0.0", True)
    _write_package(install_dir, "demo.second", "1.0.0", False)

    original_write = system_plugin_recovery._write_json
    writes = 0

    def fail_second_manifest_write(path, document):
        nonlocal writes
        if path.name == "manifest.json":
            writes += 1
            if writes == 2:
                raise OSError("simulated manifest write failure")
        original_write(path, document)

    monkeypatch.setattr(system_plugin_recovery, "_write_json", fail_second_manifest_write)

    with pytest.raises(OSError, match="simulated manifest write failure"):
        recovery.restore(startup["id"])

    assert _enabled(first) is True
    assert _enabled(second) is False
    assert [record["action"] for record in recovery.records()] == ["startup"]


def test_recovery_sidecar_rejects_non_ascii_token() -> None:
    handler = SimpleNamespace(server=SimpleNamespace(token="expected-token"))

    assert _RecoveryHandler._authorized(handler, {"token": ["非 ASCII"]}) is False


def test_beginning_a_new_session_keeps_previous_records_and_adds_a_startup_snapshot(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    _write_package(install_dir, "demo.first", "1.0.0", True)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    recovery.begin_session("session-one")
    recovery.record("enable", ["demo.first"])

    startup = recovery.begin_session("session-two")

    assert startup["action"] == "startup"
    assert [record["action"] for record in recovery.records()] == ["startup", "enable", "startup"]


def test_restore_requests_one_delayed_backend_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_dir = tmp_path / "extension_pkgs"
    _write_package(install_dir, "demo.first", "1.0.0", True)
    now = 100.0
    monkeypatch.setattr(system_plugin_recovery.time, "time", lambda: now)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    startup = recovery.begin_session("session-one")

    recovery.restore(startup["id"])

    assert recovery.consume_backend_restart("session-one") is False
    now += system_plugin_recovery._RESTART_DELAY_SECONDS
    assert recovery.consume_backend_restart("session-one") is True
    assert recovery.consume_backend_restart("session-one") is False


def test_restore_publishes_electron_restart_signal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_dir = tmp_path / "extension_pkgs"
    _write_package(install_dir, "demo.first", "1.0.0", True)
    signal_path = tmp_path / "electron-restart.json"
    monkeypatch.setenv(system_plugin_recovery._RESTART_SIGNAL_PATH_ENV, str(signal_path))
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    startup = recovery.begin_session("session-one")

    recovery.restore(startup["id"])

    signal = json.loads(signal_path.read_text(encoding="utf-8"))
    assert signal["session_id"] == "session-one"
    assert signal["record_id"] == startup["id"]


def test_recovery_history_retains_the_latest_fifty_records(tmp_path: Path) -> None:
    install_dir = tmp_path / "extension_pkgs"
    _write_package(install_dir, "demo.first", "1.0.0", True)
    recovery = SystemPluginRecovery(install_dir, tmp_path / "extension")
    recovery.begin_session("session-one")

    for _ in range(49):
        recovery.record("enable", ["demo.first"])

    recovery.begin_session("session-two")

    records = recovery.records()

    assert len(records) == 50
    assert records[-1]["action"] == "enable"
    assert records[0]["action"] == "startup"
