# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS tool-sandbox mounts (projectless task workspace visibility)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder


@pytest.fixture()
def agentos_mount_env(monkeypatch, tmp_path):
    """Anchor the task root and the agent workspace under a temporary home."""
    home = tmp_path / "home"
    tasks_root = home / "Documents" / "JiuwenSwarm"
    workspace = home / ".jiuwenswarm" / "agent" / "workspace"
    monkeypatch.setattr(sysop_builder, "get_projectless_tasks_dir", lambda: tasks_root)
    monkeypatch.setattr(sysop_builder, "_resolve_workspace_dir", lambda: workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return tasks_root


def test_task_root_mount_uses_the_host_user_directory(monkeypatch, tmp_path, agentos_mount_env) -> None:
    """Projectless task cwds live under ``<home>/Documents/JiuwenSwarm`` in the
    server sandbox; without this mount every tool command starts with a failing
    ``cd`` inside the tool sandbox. The target stays the server-sandbox path
    while the source points at the host user directory."""
    tasks_root = agentos_mount_env
    user_dir = tmp_path / "user"
    (user_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("JIUWENSWARM_USER_DIRECTORY", str(user_dir))

    mounts, _workspace = sysop_builder._build_agentos_workspace_mounts()

    task_mount = next((m for m in mounts if m["target"] == str(tasks_root)), None)
    assert task_mount is not None
    assert task_mount["source"] == str(user_dir / "Documents" / "JiuwenSwarm")
    assert task_mount["readonly"] is False
    # The bind source must exist before the container is created.
    assert tasks_root.is_dir()


def test_task_root_mount_without_user_directory(monkeypatch, agentos_mount_env) -> None:
    tasks_root = agentos_mount_env
    monkeypatch.delenv("JIUWENSWARM_USER_DIRECTORY", raising=False)

    mounts, _workspace = sysop_builder._build_agentos_workspace_mounts()

    task_mount = next((m for m in mounts if m["target"] == str(tasks_root)), None)
    assert task_mount is not None
    assert task_mount["source"] == str(tasks_root)
    assert task_mount["readonly"] is False
