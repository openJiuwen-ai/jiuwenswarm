# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""browser-move 不得劫持企业远程 MCP 的 ToolMgr 客户端。"""

from __future__ import annotations

from openjiuwen.core.foundation.tool import McpServerConfig

from jiuwenswarm.agents.harness.common.tools.browser_tools import (
    _BROWSER_MCP_DEFAULT_ID,
    _BROWSER_MCP_DEFAULT_NAME,
    _is_browser_runtime_mcp_config,
)


def test_browser_runtime_mcp_detected_by_default_id_and_name() -> None:
    assert _is_browser_runtime_mcp_config(
        McpServerConfig(
            server_id=_BROWSER_MCP_DEFAULT_ID,
            server_name=_BROWSER_MCP_DEFAULT_NAME,
            server_path="stdio://playwright-runtime-wrapper",
            client_type="stdio",
        )
    )
    assert _is_browser_runtime_mcp_config(
        McpServerConfig(
            server_id=f"{_BROWSER_MCP_DEFAULT_ID}_sse",
            server_name=_BROWSER_MCP_DEFAULT_NAME,
            server_path="http://127.0.0.1:8931/sse",
            client_type="sse",
        )
    )


def test_enterprise_remote_mcp_not_treated_as_browser() -> None:
    assert not _is_browser_runtime_mcp_config(
        McpServerConfig(
            server_id="qa-noauth-streamable-http",
            server_name="qa-noauth-streamable-http",
            server_path="http://192.168.1.96:18014/mcp",
            client_type="streamable-http",
            auth_headers={"Authorization": "Bearer token"},
            params={"timeout_s": 1.0},
        )
    )
