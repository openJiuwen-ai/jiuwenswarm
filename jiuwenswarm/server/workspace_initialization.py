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

    return any(
        not (new_workspace / filename).is_file()
        for filename in _REQUIRED_WORKSPACE_FILES
    )
