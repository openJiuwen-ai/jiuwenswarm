# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply memory configuration before creating workspace files."""

from pathlib import Path
from typing import Any

import yaml

from .config import get_memory_mode, is_memory_enabled
from .external_memory_config import is_builtin_memory_allowed, is_legacy_workspace_memory_enabled


def load_workspace_memory_config(config_file: Path) -> dict[str, Any]:
    """Read memory/modes from this instance's ``config.yaml`` (env placeholders resolved).

    活配置只有 yaml；不要读 ``config.user.yaml``，也不走进程全局 ``get_config()``
    （测试和多实例要用传入路径）。
    """
    from jiuwenswarm.common.config import resolve_env_vars
    from jiuwenswarm.common.utils import get_package_config_file

    source = config_file if config_file.is_file() else get_package_config_file()
    config = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return resolve_env_vars({key: config.get(key, {}) for key in ("memory", "modes")})


def configure_workspace_memory(workspace: Any, config: dict[str, Any]) -> Any:
    """Filter this instance's SDK creation schema; existing files stay untouched."""
    if workspace is None:
        return None
    excluded = set()
    if not is_legacy_workspace_memory_enabled(config):
        excluded.update(("USER.md", "MEMORY.md", "memory"))
    if not (get_memory_mode(config) == "local"
            and is_builtin_memory_allowed(config) and is_memory_enabled("code", config)):
        excluded.add("coding_memory")
    workspace.directories = [node for node in workspace.directories if node.get("name") not in excluded]
    return workspace
