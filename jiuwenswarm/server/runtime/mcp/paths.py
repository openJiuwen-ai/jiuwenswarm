# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared utility helpers for the MCP runtime — things that don't touch the
filesystem layout (``_mcp_root`` / ``_packages_dir``) so they can be imported
by any module without interfering with per-module ``get_workspace_dir`` patches,
plus the enterprise tenant-side built-in package seeding.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def ensure_builtins_packages(pkgs_dir: Path) -> None:
    """企业版租户工作区懒播种内置 MCP 包目录（幂等，目录已存在直接返回）。

    个人版的 ``mcp/mcp_builtins`` 由启动路径 ``prepare_workspace`` 解压种子；
    企业版每个 ``workspace_{tenant_key}`` 懒建时不经过它，内置包由此入口在
    首个读取点（连接器广场/凭证探测/CLI 连接）补种。仅目录缺失时动作——
    不做版本覆盖，避免抹掉 CLI MCP 连接时 npm install 进包目录的产物。
    """
    if pkgs_dir.is_dir():
        return
    from jiuwenswarm.enterprise.edition import is_enterprise

    if not is_enterprise():
        return
    from jiuwenswarm.common.utils import (
        CopyDiffResult,
        _ensure_mcp_builtins,
        _find_package_root,
    )

    package_root = _find_package_root()
    if package_root is None:
        return
    _ensure_mcp_builtins(
        template_agent_workspace=package_root / "resources" / "agent" / "workspace",
        mcp_builtins_dir=pkgs_dir,
        overwrite=False,
        cumulative_diff=CopyDiffResult(
            added_dirs=[], added_files=[], overwritten_files=[]
        ),
    )


def load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file as a dict, returning None on read/parse failure.

    Errors are logged at debug level — callers treat a missing/malformed file
    as "no data", not a hard failure (e.g. an MCP with no mcp.json).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("[mcp.paths] failed to read %s: %s", path, exc)
        return None


def has_skill_file(directory: Path) -> bool:
    """True if a directory holds a skill entry (``SKILL.md``)."""
    return (directory / "SKILL.md").is_file()
