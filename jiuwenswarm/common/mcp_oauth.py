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
