# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common import memory_rpc
from jiuwenswarm.common.coding_memory_paths import resolve_project_coding_memory_dir


@pytest.mark.asyncio
async def test_coding_memory_dir_uses_agent_workspace(tmp_path, monkeypatch):
    agent_workspace = tmp_path / "agent_workspace"
    workspace = tmp_path / "project" / "frontend"
    project_dir = tmp_path / "project"
    coding_memory_dir = Path(
        resolve_project_coding_memory_dir(
            agent_workspace_dir=agent_workspace,
            project_dir=project_dir,
        )
    )
    coding_memory_dir.mkdir(parents=True)
    monkeypatch.setattr(memory_rpc, "get_agent_workspace_dir", lambda: agent_workspace)

    result = await memory_rpc.handle_memory_open(
        str(workspace),
        {"project_dir": str(project_dir)},
    )

    assert Path(result["coding_memory_dir"]) == coding_memory_dir


def test_runtime_coding_memory_dir_uses_agent_workspace(tmp_path, monkeypatch):
    agent_workspace = tmp_path / "agent_workspace"
    project_workspace = tmp_path / "project" / "frontend"
    monkeypatch.setattr(memory_rpc, "get_agent_workspace_dir", lambda: agent_workspace)

    runtime_dirs = memory_rpc._get_runtime_memory_dirs(str(project_workspace))

    assert Path(runtime_dirs[1]) == agent_workspace / "coding_memory"
