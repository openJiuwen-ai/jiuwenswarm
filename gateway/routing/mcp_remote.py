# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""mcp_registry 域的 WS 远端 provider（server_store_bridge 的拆仓替代件）。

核实结论（gateway 进程 MCP 消费面）：mcp 三域在 gateway 进程的唯一消费是
web 通道的 oauth 回调（``mcp_oauth_http`` → ``common.mcp_oauth`` →
``provider("mcp_registry").complete_oauth_callback``）；状态读写与凭据
解析全部在 AgentServer 进程执行（config 面板请求已统一 E2A 代理）。因此
远端壳只承接 ``mcp_registry`` 域的回调方法，``mcp_state`` /
``mcp_credential`` 两域在远端模式保持未注册（调用得到
``ExternalProviderUnregistered``，注入点原生降级）。

远端调用经既有 Gateway ↔ AgentServer WebSocket 通道上的 ``command.mcp``
扩展 action（``oauth_callback``）。该 action 族在 server 侧按
``channel_id == "gateway"`` 门控（``agent_ws_server._command_mcp_internal_state``），
不脱敏的原始数据不会暴露给 web/TUI 通道。

启用：环境变量 ``JIUWENSWARM_MCP_REMOTE=1`` 时由
``server_store_bridge.install()`` 改注册本实现（默认仍走本地桥）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jiuwenswarm.common.external_providers import register
from jiuwenswarm.common.schema.message import ReqMethod

from gateway.routing.e2a_proxy import fetch_agent_unary

logger = logging.getLogger(__name__)

#: oauth 回调远端执行的 unary 超时（秒）。回调是浏览器重定向后的等待页，
#: 5s 上限与 session_metadata 远端读一致（超时按失败降级，不悬挂页面）。
MCP_OAUTH_CALLBACK_TIMEOUT_SECONDS = 5.0

#: 内部调用通道标识；server 侧对 command.mcp 的内部 action 族按此门控。
_MCP_INTERNAL_CHANNEL_ID = "gateway"

_REMOTE_ENV = "JIUWENSWARM_MCP_REMOTE"

_agent_client: Any = None


def remote_enabled() -> bool:
    """是否以 WS 远端实现注册 mcp_registry 域（默认本地桥）。"""
    return os.environ.get(_REMOTE_ENV, "").strip() in ("1", "true", "True")


def bind_agent_client(client: Any) -> None:
    """绑定 Gateway → AgentServer 客户端（app_gateway 启动装配时调用）。"""
    global _agent_client
    _agent_client = client


class McpRegistryRemoteProvider:
    """mcp_registry 域的 WS 远端实现（oauth 回调）。

    异步为主入口；同步入口保留协议签名但降级返回 ``False`` 并告警——
    oauth 回调的唯一消费方（``mcp_oauth_http``）是 async handler，随
    本模块一并迁移到异步入口。
    """

    @staticmethod
    def complete_oauth_callback(query: dict[str, Any]) -> bool:
        """同步入口降级：无法同步等待 WS 往返，返回失败并告警。"""
        logger.warning(
            "[mcp-registry-remote] sync entry degraded in remote mode; "
            "oauth callback treated as failed（migrate to "
            "complete_oauth_callback_async）"
        )
        return False

    @staticmethod
    async def complete_oauth_callback_async(query: dict[str, Any]) -> bool:
        """经 WS 执行 oauth 回调完成；不可达/失败/超时一律按失败返回。"""
        ok, payload = await fetch_agent_unary(
            agent_client=_agent_client,
            req_method=ReqMethod.COMMAND_MCP,
            params={"action": "oauth_callback", "query": dict(query or {})},
            session_id=None,
            user_id=None,
            channel_id=_MCP_INTERNAL_CHANNEL_ID,
            label="mcp.oauth_callback",
            timeout_seconds=MCP_OAUTH_CALLBACK_TIMEOUT_SECONDS,
        )
        if not ok:
            logger.warning(
                "[mcp-registry-remote] oauth callback failed: error=%s code=%s",
                (payload or {}).get("error"),
                (payload or {}).get("code"),
            )
            return False
        return bool((payload or {}).get("completed"))


def install() -> None:
    """把 WS 远端实现注册进 ``common.external_providers`` 的 mcp_registry 域。"""
    register("mcp_registry", McpRegistryRemoteProvider())
    logger.info("[mcp-registry-remote] registered mcp_registry provider (WS)")
