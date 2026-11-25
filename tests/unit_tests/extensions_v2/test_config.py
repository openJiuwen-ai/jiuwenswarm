# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.extensions_v2.config import resolve_install_dir, resolve_state_dir, resolve_store_dir


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


def test_store_directory_reads_extensions_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)

    assert resolve_store_dir({"extensions": {"store_dir": "data/store"}}) == tmp_path / "data" / "store"
    assert resolve_store_dir({"extensions": {"store_dir": str(tmp_path / "abs-store")}}) == tmp_path / "abs-store"


def test_store_directory_falls_back_to_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXTENSION_STORE_DIR", raising=False)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)

    assert resolve_store_dir({}) == tmp_path / "extension" / "store"
    assert not (tmp_path / "extension").exists()


def test_state_directory_reads_extensions_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)

    assert resolve_state_dir({"extensions": {"state_dir": "data/state"}}) == tmp_path / "data" / "state"
    assert resolve_state_dir({"extensions": {"state_dir": str(tmp_path / "abs-state")}}) == tmp_path / "abs-state"


def test_state_directory_falls_back_to_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXTENSION_STATE_DIR", raising=False)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)

    assert resolve_state_dir({}) == tmp_path / "extension" / "state"
    assert not (tmp_path / "extension").exists()
