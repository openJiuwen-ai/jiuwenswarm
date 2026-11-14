# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared utility helpers for the MCP runtime — things that don't touch the
filesystem layout (``_mcp_root`` / ``_packages_dir``) so they can be imported
by any module without interfering with per-module ``get_workspace_dir`` patches.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
