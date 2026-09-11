# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path


_REQUIRED_WORKSPACE_FILES = (
    "AGENT.md",
    "IDENTITY.md",
    "SOUL.md",
    "HEARTBEAT.md",
)


def should_prepare_workspace(
    config_file: Path,
    new_workspace: Path,
    old_workspace: Path,
) -> bool:
    """Return whether the user workspace needs its initial files prepared."""
    if not config_file.is_file():
        return True

    if old_workspace.exists() and not new_workspace.exists():
        return True

    from jiuwenswarm.agents.harness.common.memory.external_memory_config import is_legacy_workspace_memory_enabled
    from jiuwenswarm.agents.harness.common.memory.workspace import load_workspace_memory_config

    required_files = _REQUIRED_WORKSPACE_FILES
    if is_legacy_workspace_memory_enabled(load_workspace_memory_config(config_file)):
        required_files += ("USER.md",)
    return any(
        not (new_workspace / filename).is_file()
        for filename in required_files
    )
