# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.extensions_v2.config import resolve_install_dir


def test_absolute_config_path_is_used(tmp_path: Path) -> None:
    target = tmp_path / "extensions"

    assert resolve_install_dir({"extensions": {"install_dir": str(target)}}) == target


def test_relative_config_path_is_anchored_at_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path,
    )

    assert resolve_install_dir({"extensions": {"install_dir": "extensions_v2"}}) == (
        tmp_path / "extensions_v2"
    )


def test_environment_is_used_when_config_has_no_value(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "from-env"
    monkeypatch.setenv("EXTENSION_INSTALL_DIR", str(target))

    assert resolve_install_dir({}) == target


def test_default_directory_is_under_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXTENSION_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: tmp_path,
    )

    assert resolve_install_dir({}) == tmp_path / "extension_pkgs"
