# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

import pytest

from jiuwenswarm.server.workspace_initialization import should_prepare_workspace


@pytest.mark.parametrize("missing", [None, "AGENT.md", "IDENTITY.md", "SOUL.md", "HEARTBEAT.md"])
def test_workspace_initialization_only_requires_active_context_files(
    tmp_path: Path,
    missing: str | None,
) -> None:
    config_file = tmp_path / "config" / "config.yaml"
    config_file.parent.mkdir()
    config_file.touch()
    new_workspace = tmp_path / "agent" / "workspace"
    new_workspace.mkdir(parents=True)
    for name in ("AGENT.md", "IDENTITY.md", "SOUL.md", "HEARTBEAT.md"):
        if name != missing:
            (new_workspace / name).touch()
    old_workspace = tmp_path / "agent" / "jiuwenclaw_workspace"

    assert should_prepare_workspace(config_file, new_workspace, old_workspace) is (missing is not None)
