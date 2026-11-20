# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A self-contained TTSE fixture uses the real ZIP/import/install boundary."""

import json
import zipfile

import pytest

from scripts import package_ttse_plugin as packaging
from scripts.package_ttse_plugin import package_ttse_plugin
from jiuwenswarm.server.runtime import extension_package_manager as catalog


def test_ttse_zip_is_reproducible_and_imports_as_an_uninstalled_plugin(
    extension_workspace, tmp_path, monkeypatch, ttse_plugin_source,
):
    monkeypatch.setattr(packaging, "DEFAULT_SOURCE_DIR", tmp_path / "missing-local-resources")
    first = package_ttse_plugin(tmp_path / "ttse_2.0.0.zip", source=ttse_plugin_source)
    second = package_ttse_plugin(tmp_path / "again.zip", source=ttse_plugin_source)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {
            "README.md", "manifest.json", "config.yaml", "rails/ttse_plugin_rail.py",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == "2.0.0"
        assert manifest["metadata"]["plugin_runtime"]["contract_version"] == 1
        assert manifest["metadata"]["plugin_runtime"]["requires"] == [
            "trajectory", "main_model", "agent_data_workspace", "embedding_factory",
        ]
    assert catalog.import_plugin_package({"path": str(first)}) == {"id": "ttse"}
    assert not catalog.is_plugin_allowed("ttse")
    catalog.install_plugin_package({"id": "ttse"})
    assert catalog.is_plugin_allowed("ttse")
    installed = catalog.resolve_plugin_dir("ttse")
    assert (installed / "config.yaml").is_file()
    assert (installed / "rails/ttse_plugin_rail.py").is_file()


def test_missing_default_plugin_source_reports_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(packaging, "DEFAULT_SOURCE_DIR", tmp_path / "missing-local-resources")
    output = tmp_path / "ttse.zip"

    with pytest.raises(FileNotFoundError, match="provide a plugin source with --source"):
        package_ttse_plugin(output)

    assert not output.exists()


def test_missing_rail_does_not_create_archive(tmp_path, ttse_plugin_source):
    (ttse_plugin_source / "rails/ttse_plugin_rail.py").unlink()
    output = tmp_path / "ttse.zip"

    with pytest.raises(ValueError, match="Missing TTSE rail file"):
        package_ttse_plugin(output, source=ttse_plugin_source)

    assert not output.exists()
