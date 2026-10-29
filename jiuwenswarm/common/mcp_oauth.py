# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP OAuth 回调入口（AgentServer 进程）。

异常类型与 provider 域名取自 ``openjiuwen_protocol.mcp_oauth``（跨进程契约单一
来源）；本文件只绑定母仓进程自己的 provider 注册表。
"""

from __future__ import annotations

from typing import Any

from openjiuwen_protocol.mcp_oauth import (
    MCP_REGISTRY_PROVIDER_DOMAIN,
    McpOAuthError,  # noqa: F401 — 旧 import 路径兼容
)

from jiuwenswarm.common.external_providers import provider

__all__ = ["McpOAuthError", "complete_oauth_flow", "complete_oauth_flow_async"]


def complete_oauth_flow(query: dict[str, Any]) -> bool:
    """Complete a callback through the process's registered MCP provider."""
    return provider(MCP_REGISTRY_PROVIDER_DOMAIN).complete_oauth_callback(query)


async def complete_oauth_flow_async(query: dict[str, Any]) -> bool:
    """Async twin of :func:`complete_oauth_flow` for the unified provider entry.

    The remote provider (gateway split mode) is async-only; async consumers
    such as the web callback handler use this entry directly instead of a
    threadpool hop around the sync path.
    """
    completed = await provider(
        MCP_REGISTRY_PROVIDER_DOMAIN
    ).complete_oauth_callback_async(query)
    if not completed:
        # handler 不检查返回值：不抛会把失败渲染成"授权成功"
        raise McpOAuthError("授权回调未完成，请重试连接")
    return True
