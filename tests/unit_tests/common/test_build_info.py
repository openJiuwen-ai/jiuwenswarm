from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import build_info
from jiuwenswarm.common import utils


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def test_runtime_build_info_uses_installed_git_revision(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "build-info.json"
    manifest.write_text(
        json.dumps({
            "jiuwenswarm": {
                "repository": "https://gitcode.com/openJiuwen/jiuwenswarm.git",
                "branch": "0.2.8",
                "commit_id": COMMIT,
            },
            "agent_core": {"source": "git", "branch": "release/v0.1.20", "commit_id": COMMIT},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_info, "_BUILD_INFO_PATH", manifest)
    installed = SimpleNamespace(
        version="0.1.20",
        read_text=lambda _name: json.dumps({
            "vcs_info": {"vcs": "git", "commit_id": OTHER_COMMIT},
        }),
    )
    monkeypatch.setattr(build_info.metadata, "distribution", lambda _name: installed)

    info = build_info.get_runtime_build_info()

    assert info["jiuwenswarm"]["commit_id"] == COMMIT
    assert "repository" not in info["jiuwenswarm"]
    assert info["agent_core"] == {
        "source": "git", "version": "0.1.20", "commit_id": OTHER_COMMIT,
    }


def test_runtime_build_info_uses_pypi_style_installed_version(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "build-info.json"
    manifest.write_text(
        json.dumps({"jiuwenswarm": {"branch": "0.2.8", "commit_id": COMMIT}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_info, "_BUILD_INFO_PATH", manifest)
    monkeypatch.setattr(
        build_info.metadata,
        "distribution",
        lambda _name: SimpleNamespace(version="0.1.20", read_text=lambda _file: None),
    )

    assert build_info.get_runtime_build_info()["agent_core"] == {
        "source": "package", "version": "0.1.20",
    }


def test_build_info_is_written_to_config_only_when_changed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(utils, "is_package_installation", lambda: True)
    config = tmp_path / "config.yaml"
    config.write_text(
        "config_version: 0.2.8\nbuild_info:\n  jiuwenswarm:\n"
        "    repository: https://gitcode.com/openJiuwen/jiuwenswarm.git\n"
        f"    branch: 0.2.8\n    commit_id: {COMMIT}\n"
        "preferred_language: zh\n",
        encoding="utf-8",
    )
    info = {
        "jiuwenswarm": {"branch": "0.2.8", "commit_id": COMMIT},
        "agent_core": {"source": "package", "version": "0.1.20"},
    }
    monkeypatch.setattr(build_info, "get_runtime_build_info", lambda: info)

    assert build_info.sync_build_info_to_config(config) is True
    first = config.read_text(encoding="utf-8")
    assert first.index("build_info:") < first.index("preferred_language:")
    assert "repository:" not in first
    assert build_info.sync_build_info_to_config(config) is False
    assert config.read_text(encoding="utf-8") == first


def test_unwritable_config_does_not_block_startup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(utils, "is_package_installation", lambda: True)
    config = tmp_path / "config.yaml"
    config.write_text("config_version: 0.2.8\n", encoding="utf-8")
    monkeypatch.setattr(build_info, "get_runtime_build_info", lambda: {
        "jiuwenswarm": {"branch": "develop", "commit_id": COMMIT},
    })
    def deny_replace(*_args: object) -> None:
        raise PermissionError("read-only config")

    monkeypatch.setattr(build_info, "_replace_config_file", deny_replace)
    monkeypatch.setattr(build_info.time, "sleep", lambda _seconds: None)

    assert build_info.sync_build_info_to_config(config) is False
    assert config.read_text(encoding="utf-8") == "config_version: 0.2.8\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode check")
def test_build_info_preserves_restrictive_config_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(utils, "is_package_installation", lambda: True)
    config = tmp_path / "config.yaml"
    config.write_text("config_version: 0.2.8\nsecret: token\n", encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.setattr(build_info, "get_runtime_build_info", lambda: {
        "jiuwenswarm": {"branch": "develop", "commit_id": COMMIT},
    })

    assert build_info.sync_build_info_to_config(config) is True
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "secret: token" in config.read_text(encoding="utf-8")


def test_source_mode_ignores_leftover_build_info(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("config_version: 0.2.8\n", encoding="utf-8")
    monkeypatch.setattr(utils, "is_package_installation", lambda: False)
    monkeypatch.setattr(
        build_info,
        "get_runtime_build_info",
        lambda: (_ for _ in ()).throw(AssertionError("source mode must not read build info")),
    )

    assert build_info.sync_build_info_to_config(config) is False
    assert config.read_text(encoding="utf-8") == "config_version: 0.2.8\n"
