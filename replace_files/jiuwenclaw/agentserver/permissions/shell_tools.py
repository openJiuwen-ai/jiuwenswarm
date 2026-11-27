# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared shell tool helpers for permission checks."""

from __future__ import annotations

from typing import Any

SHELL_PERMISSION_TOOLS = frozenset({"bash", "mcp_exec_command", "create_terminal"})
SHELL_COMMAND_ARG_KEYS = ("command", "cmd")


def is_shell_permission_tool(tool_name: str) -> bool:
    return tool_name in SHELL_PERMISSION_TOOLS


def extract_shell_command(tool_args: dict[str, Any] | None) -> str:
    if not isinstance(tool_args, dict):
        return ""
    for key in SHELL_COMMAND_ARG_KEYS:
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
