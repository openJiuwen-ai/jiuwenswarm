# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MCP toolkit aggregator for openjiuwen tools."""

from __future__ import annotations

from openjiuwen.core.foundation.tool import Tool

from jiuwenswarm.agents.harness.common.tools.command_tools import mcp_exec_command
from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import mcp_fetch_webpage
from jiuwenswarm.agents.harness.common.tools.web_search import web_search
from jiuwenswarm.common.mcp_config import (  # noqa: F401
    _check_dangerous_args,
    _is_blocked_host,
    _loopback_mcp_allowed,
    _normalize_mcp_client_type,
    _normalize_stdio_command_kind,
    _optional_auth_dict,
    _path_is_under_trusted_root,
    _pick_mcp_url,
    _trusted_cat_cafe_stdio_roots,
    _validate_cat_cafe_request_scoped_stdio,
    _validate_request_scoped_remote_mcp,
    create_mcp_tool,
)


def get_mcp_tools() -> list[Tool]:
    """Return all MCP toolkit tools for registration in Runner."""
    return [web_search, mcp_fetch_webpage, mcp_exec_command]


__all__ = [
    "web_search",
    "mcp_fetch_webpage",
    "mcp_exec_command",
    "get_mcp_tools",
    "create_mcp_tool",
    "_check_dangerous_args",
    "_is_blocked_host",
    "_loopback_mcp_allowed",
    "_normalize_mcp_client_type",
    "_normalize_stdio_command_kind",
    "_optional_auth_dict",
    "_path_is_under_trusted_root",
    "_pick_mcp_url",
    "_trusted_cat_cafe_stdio_roots",
    "_validate_cat_cafe_request_scoped_stdio",
    "_validate_request_scoped_remote_mcp",
]
