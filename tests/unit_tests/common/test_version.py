from __future__ import annotations

from importlib import metadata
import json

import pytest

from jiuwenswarm.common import version as version_module


def test_runtime_version_prefers_installed_distribution(monkeypatch) -> None:
    monkeypatch.setattr(
        version_module.metadata,
        "version",
        lambda package: "0.2.5.beta1.dev20261009" if package == "workswarm" else "",
    )

    assert version_module.get_runtime_version() == "0.2.5.beta1.dev20261009"


def test_runtime_version_falls_back_to_generated_version(monkeypatch) -> None:
    def missing_distribution(_package: str) -> str:
        raise metadata.PackageNotFoundError("workswarm")

    monkeypatch.setattr(version_module.metadata, "version", missing_distribution)

    assert version_module.get_runtime_version() == version_module.VERSION


def test_runtime_version_falls_back_when_metadata_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(version_module.metadata, "version", lambda _package: "   ")

    assert version_module.get_runtime_version() == version_module.VERSION


def test_runtime_build_date_reads_artifact_not_current_clock(monkeypatch, tmp_path) -> None:
    path = tmp_path / "build-metadata.json"
    path.write_text(json.dumps({"version": "0.2.8", "build_date": "2026-10-20"}), encoding="utf-8")
    monkeypatch.setattr(version_module, "_BUILD_METADATA_PATH", path)

    assert version_module.get_runtime_build_date() == "2026-10-20"


@pytest.mark.parametrize("content", [None, "broken json", "[]", "null", "{}", '{"build_date": 42}',
                                     '{"build_date": "2026-02-30"}', '{"build_date": "20261020"}'])
def test_runtime_build_date_missing_or_invalid_is_unknown(monkeypatch, tmp_path, content) -> None:
    path = tmp_path / "build-metadata.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(version_module, "_BUILD_METADATA_PATH", path)

    assert version_module.get_runtime_build_date() == ""


@pytest.mark.parametrize("build_date", ["2026-10-26", ""])
def test_diagnostic_label_keeps_machine_version_unchanged(monkeypatch, build_date) -> None:
    monkeypatch.setattr(version_module.metadata, "version", lambda _package: "0.2.8.dev20261026")
    monkeypatch.setattr(version_module, "get_runtime_build_date", lambda: build_date)

    expected = f"0.2.8.dev20261026 · {build_date}" if build_date else "0.2.8.dev20261026"
    assert version_module.get_runtime_version_label() == expected
    assert version_module.get_runtime_version() == "0.2.8.dev20261026"
