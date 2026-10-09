from __future__ import annotations

from importlib import metadata

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
