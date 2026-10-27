# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared MCP OAuth callback contract without server implementation imports."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.external_providers import provider


class McpOAuthError(RuntimeError):
    """An expected authorization failure safe to display on the callback page."""


def complete_oauth_flow(query: dict[str, Any]) -> bool:
    """Complete a callback through the process's registered MCP provider."""
    return provider("mcp_registry").complete_oauth_callback(query)


async def complete_oauth_flow_async(query: dict[str, Any]) -> bool:
    """Async twin of :func:`complete_oauth_flow` for the unified provider entry.

    The remote provider (gateway split mode) is async-only; async consumers
    such as the web callback handler use this entry directly instead of a
    threadpool hop around the sync path.
    """
    return await provider("mcp_registry").complete_oauth_callback_async(query)
