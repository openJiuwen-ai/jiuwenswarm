# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File path extraction and tool registry for ``file_guard`` (phase 1)."""

from jiuwenclaw.agentserver.permissions.files.extract import (
    extract_path_aware_command_accesses,
    extract_paths_from_command,
    extract_shell_path_accesses,
)
from jiuwenclaw.agentserver.permissions.files.registry import (
    FileToolSpec,
    lookup_file_tool_specs,
    register_file_tool,
)

__all__ = [
    "FileToolSpec",
    "lookup_file_tool_specs",
    "register_file_tool",
    "extract_path_aware_command_accesses",
    "extract_paths_from_command",
    "extract_shell_path_accesses",
]
