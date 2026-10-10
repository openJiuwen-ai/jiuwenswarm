# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP runtime package: marketplace registry, CLI driver,
credential store, skill installer, connection state store.
"""

from jiuwenswarm.server.runtime.mcp.registry import (
    build_config_entry,
    complete_cli_auth,
    register_custom_mcp,
    connect_mcp,
    delete_custom_mcp,
    disconnect_mcp,
    get_mcp,
    list_marketplace_mcps,
    save_mcp_credentials,
)

__all__ = [
    "build_config_entry",
    "complete_cli_auth",
    "register_custom_mcp",
    "connect_mcp",
    "delete_custom_mcp",
    "disconnect_mcp",
    "get_mcp",
    "list_marketplace_mcps",
    "save_mcp_credentials",
]
