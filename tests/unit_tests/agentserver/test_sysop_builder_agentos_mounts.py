# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS tool-sandbox mounts (projectless task workspace visibility)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder

_USER_DIR = "/home/agentos/users/alice"


@pytest.fixture()
def agentos_mount_env(monkeypatch, tmp_path):
    """Anchor the AgentOS home and workspace under a temporary directory."""
    home = tmp_path / "home"
    workspace = home / ".jiuwenswarm" / "agent" / "workspace"
    workspace.mkdir(parents=True)
    tasks = home / "Documents" / "JiuwenSwarm"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    monkeypatch.setenv("JIUWENSWARM_USER_DIRECTORY", _USER_DIR)
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tasks))
    monkeypatch.setattr(sysop_builder, "_resolve_workspace_dir", lambda: workspace)
    monkeypatch.setattr(sysop_builder, "get_sandbox_endpoint", lambda: {"type": "adx-environment"})
    return tasks


def test_task_root_mount_uses_the_host_user_directory(agentos_mount_env) -> None:
    """Projectless task cwds live under ``<home>/Documents/JiuwenSwarm`` in the
    server sandbox; without this mount every tool command starts with a failing
    ``cd`` inside the tool sandbox. The target stays the server-sandbox path
    while the source points at the host user directory."""
    tasks_root = agentos_mount_env

    extra = sysop_builder._build_adx_extra_params()

    task_mount = next((m for m in extra["mounts"] if m["target"] == str(tasks_root)), None)
    assert task_mount is not None
    assert task_mount["host_path"] == str(Path(_USER_DIR) / "Documents" / "JiuwenSwarm")
    assert task_mount["options"] == ["rw"]
    # The bind source must exist before the container is created.
    assert tasks_root.is_dir()


def test_task_root_mount_requires_the_host_user_directory(
    agentos_mount_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_USER_DIRECTORY")

    with pytest.raises(ValueError, match="user directory"):
        sysop_builder._build_adx_extra_params()
