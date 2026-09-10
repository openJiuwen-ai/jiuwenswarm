# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MCP toolkit aggregator for openjiuwen tools."""

from __future__ import annotations
import os
from weakref import WeakSet

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.runner import Runner

from jiuwenswarm.agents.harness.common.tools.command_tools import mcp_exec_command
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    configured_paid_search_providers,
    mcp_free_search,
    mcp_paid_search,
    refresh_paid_search_metadata,
)
from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import mcp_fetch_webpage

_SEARCH_ABILITY_MANAGERS = WeakSet()


def track_mcp_search_tools(ability_manager) -> None:
    """Keep live MCP subagents in the search-config reload fan-out."""
    _SEARCH_ABILITY_MANAGERS.add(ability_manager)


def refresh_mcp_paid_search_tools() -> None:
    """Refresh provider choices and reconcile registration in live subagents."""
    refresh_paid_search_metadata()
    enabled = _has_paid_search_api_key()
    managers = list(_SEARCH_ABILITY_MANAGERS)
    if enabled and managers:
        Runner.resource_mgr.add_tool(mcp_paid_search, skip_if_exists=True)
    for manager in managers:
        current = manager.get(mcp_paid_search.card.name)
        if enabled and current is None:
            manager.add(mcp_paid_search.card)
        elif not enabled and current is not None:
            manager.remove(mcp_paid_search.card.name)


def _has_paid_search_api_key() -> bool:
    """Check if any paid search API key is configured."""
    return bool(configured_paid_search_providers())


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
    refresh_paid_search_metadata()
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
