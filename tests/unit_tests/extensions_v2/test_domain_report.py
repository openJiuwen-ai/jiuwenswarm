"""AgentServer report configuration and product manifest contracts."""

import json

import pytest

from openjiuwen.core.extension.install_report import PackageInstallState
from openjiuwen.core.extension.management import QueryResult
from jiuwenswarm.extensions_v2.config import (
    resolve_extension_data_dir, resolve_install_report_path, resolve_install_report_settings,
)
from jiuwenswarm.extensions_v2.management.adapter import _encode
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager
from .management.support import make_bus, write_package


def test_report_paths_and_unit_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    assert resolve_extension_data_dir({}) == tmp_path / "extension"
    assert resolve_install_report_path({}) == tmp_path / "extension" / "extension_install_report.yaml"
    config = {"extensions": {"data_dir": "custom", "install_report": {
        "poll_interval_ms": 25, "dependency_timeout_seconds": 8, "lock_timeout_seconds": 2,
    }}}
    assert resolve_install_report_path(config) == tmp_path / "custom" / "extension_install_report.yaml"
    settings = resolve_install_report_settings(config)
    assert (settings.poll_interval, settings.dependency_timeout, settings.lock_timeout) == (0.025, 8, 2)
    defaults = resolve_install_report_settings({})
    assert (defaults.poll_interval, defaults.dependency_timeout, defaults.lock_timeout) == (0.5, 300, 5)


@pytest.mark.parametrize("key", ["poll_interval_ms", "dependency_timeout_seconds", "lock_timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, "500", float("nan"), float("inf")])
def test_invalid_report_time_is_rejected(key, value):
    with pytest.raises(ValueError):
        resolve_install_report_settings({"extensions": {"install_report": {key: value}}})


async def test_missing_or_foreign_domain_is_never_loaded(tmp_path):
    manager = ExtensionManager(tmp_path / "installed", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    try:
        source = write_package(tmp_path / "source")
        manifest_path = source / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["package"].pop("domain")
        manifest_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        assert (await manager.install([source]))[0].error.code == "EXTENSION_MANIFEST_INVALID"
        raw["package"]["domain"] = "gateway"
        manifest_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        assert (await manager.install([source]))[0].error.code == "EXTENSION_DOMAIN_MISMATCH"
        assert not manager.active_packages()
        assert not list((tmp_path / "installed").glob("*/*/manifest.json"))
    finally:
        await manager.shutdown()


def test_query_ids_come_from_all_successful_report_rows():
    rows = (
        PackageInstallState("server", "agentserver", "p1", "1.0.0", {}, "success", None, None),
        PackageInstallState("gateway", "gateway", "p2", "1.0.0", {}, "failed", {"code": "reload"}, None),
        PackageInstallState("waiting", "gateway", "p2", None, {}, "waiting", None,
                            {"operation_id": "wait", "operation": "install", "version": "1.0.0", "requires": {}}),
    )
    encoded = _encode(QueryResult(packages=rows))
    assert encoded["package_ids"] == ["gateway", "server"]
    assert encoded["active"] == []
    assert [(item["package_id"], item["domain"], item["status"], item["active_version"])
            for item in encoded["packages"]] == [
        ("server", "agentserver", "success", "1.0.0"),
        ("gateway", "gateway", "failed", "1.0.0"),
        ("waiting", "gateway", "waiting", None),
    ]
