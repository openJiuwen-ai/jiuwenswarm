# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The partitioned Gateway config path must bridge onto the real config dir.

The pinned agent-gateway resolves its config through ``<root>/infra/config``
while this repository owns ``<root>/config``. ``prepare_runtime_workspace``
bridges the two with a directory link so Gateway saves land in the real
``config.yaml`` and share the same write lock. These tests pin that contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common import utils as common_utils


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialized flat-layout workspace; package-side work is stubbed."""
    workspace = tmp_path / "runtime"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "config.yaml").write_text("channels: {}\n", encoding="utf-8")
    monkeypatch.setattr(common_utils, "get_user_workspace_dir", lambda: workspace)
    monkeypatch.setattr(common_utils, "cleanup_stale_openjiuwen_descs", lambda: None)
    monkeypatch.setattr(common_utils, "cleanup_team_files", lambda *_args: None)
    monkeypatch.setattr(common_utils, "mcp_builtins_seed_update_needed", lambda *_args: False)
    monkeypatch.setattr(common_utils, "ensure_config_migrated_from_template", lambda *_args: False)
    monkeypatch.setattr(common_utils, "prepare_workspace", lambda **_kwargs: None)
    monkeypatch.setattr(common_utils, "ensure_default_builtin_skills", lambda: None)
    return workspace


def _link_kind(path: Path) -> str:
    """Classify a directory entry as ``symlink``, ``junction`` or ``real``."""
    if path.is_symlink():
        return "symlink"
    if sys.platform == "win32":
        try:
            attributes = os.lstat(path).st_file_attributes
        except (AttributeError, OSError):
            return "real"
        if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return "junction"
    return "real"


def test_prepare_links_infra_config_onto_real_dir(workspace: Path) -> None:
    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    link = workspace / "infra" / "config"
    assert _link_kind(link) in {"symlink", "junction"}
    if _link_kind(link) == "symlink":
        # POSIX branch uses a relative target; it must resolve against the
        # link's parent (<root>/infra/) to the real config dir, never to
        # <root>/infra/config itself (a self-reference loop).
        assert os.readlink(link) == "../config"
    assert (link / "config.yaml").read_text(encoding="utf-8") == "channels: {}\n"
    # The same file through both layouts, so one write lock covers both paths.
    assert os.path.samefile(link / "config.yaml", workspace / "config" / "config.yaml")


def test_prepare_is_idempotent_across_restarts(workspace: Path) -> None:
    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)
    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    link = workspace / "infra" / "config"
    assert _link_kind(link) in {"symlink", "junction"}
    assert (workspace / "config" / "config.yaml").exists()


def test_real_infra_config_directory_is_never_replaced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied = workspace / "infra" / "config"
    occupied.mkdir(parents=True)
    (occupied / "config.yaml").write_text("gateway-owned: true\n", encoding="utf-8")

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    # A real directory is user data: kept in place, not shadowed by a link.
    assert _link_kind(occupied) == "real"
    assert (occupied / "config.yaml").read_text(encoding="utf-8") == "gateway-owned: true\n"
    assert (workspace / "config" / "config.yaml").read_text(encoding="utf-8") == "channels: {}\n"


def test_stale_link_to_other_target_is_replaced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = workspace / "elsewhere"
    other.mkdir()
    link = workspace / "infra" / "config"
    link.parent.mkdir(parents=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(other), str(link))
    else:
        os.symlink("elsewhere", link, target_is_directory=True)
    assert _link_kind(link) in {"symlink", "junction"}

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert _link_kind(link) in {"symlink", "junction"}
    assert os.path.samefile(link, workspace / "config")


def test_missing_config_dir_skips_link(workspace: Path) -> None:
    import shutil

    shutil.rmtree(workspace / "config")

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)

    assert not (workspace / "infra" / "config").exists()


def test_gateway_config_write_lands_in_real_config(workspace: Path) -> None:
    """End-to-end contract: a Gateway-side save reaches the flat-layout file."""
    pytest.importorskip("agent_gateway")
    from agent_gateway.common import config as gateway_config

    common_utils.prepare_runtime_workspace(cleanup_stale_descs=False)
    gateway_config.CONFIG_YAML_PATH = workspace / "infra" / "config" / "config.yaml"

    def mutate(data):
        channels = data.setdefault("channels", {})
        feishu = channels.setdefault("feishu", {})
        feishu["apps"] = [{"name": "app", "app_id": "cli_x", "enabled": True}]
        return data

    gateway_config.update_config(mutate)

    from ruamel.yaml import YAML

    saved = YAML().load((workspace / "config" / "config.yaml").read_text(encoding="utf-8"))
    apps = saved["channels"]["feishu"]["apps"]
    assert apps[0]["app_id"] == "cli_x"
    # The cross-process write lock lives next to the real config file.
    assert (workspace / "config" / "config.lock").exists()
