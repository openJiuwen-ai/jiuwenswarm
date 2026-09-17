# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MCP toolkit aggregator for openjiuwen tools."""

from __future__ import annotations
import os

from openjiuwen.core.foundation.tool import Tool

from jiuwenswarm.agents.harness.common.tools.command_tools import mcp_exec_command
from jiuwenswarm.agents.harness.common.tools.search_tools import mcp_free_search, mcp_paid_search
from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import mcp_fetch_webpage


def _has_paid_search_api_key() -> bool:
    """Check if any paid search API key is configured."""
    return any([
        os.environ.get("BOCHA_API_KEY"),
        os.environ.get("PERPLEXITY_API_KEY"),
        os.environ.get("SERPER_API_KEY"),
        os.environ.get("JINA_API_KEY"),
    ])


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _is_free_search_enabled() -> bool:
    return (
        _env_flag("FREE_SEARCH_DDG_ENABLED", default=False)
        or _env_flag("FREE_SEARCH_BING_ENABLED", default=False)
    )


def get_mcp_tools() -> list[Tool]:
    """Return all MCP toolkit tools for registration in Runner."""
    tools = []
    if _has_paid_search_api_key():
        tools.append(mcp_paid_search)
    if _is_free_search_enabled():
        tools.append(mcp_free_search)
    tools.extend([mcp_fetch_webpage, mcp_exec_command])
    return tools


__all__ = [
    "mcp_free_search",
    "mcp_paid_search",
    "mcp_fetch_webpage",
    "mcp_exec_command",
    "get_mcp_tools",
]
