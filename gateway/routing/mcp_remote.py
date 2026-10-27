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
扩展 action（``oauth_callback``，服务端按握手绑定的连接级共享密钥放行）。该 action 族在 server 侧按
``channel_id == "gateway"`` 门控（``agent_ws_server._command_mcp_internal_state``），
不脱敏的原始数据不会暴露给 web/TUI 通道。

bridge 退役后本实现是 mcp_registry 域在 gateway 进程的唯一注册形态
（``app_gateway`` 启动装配）。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.external_providers import register
from jiuwenswarm.common.mcp_oauth import McpOAuthError
from jiuwenswarm.common.schema.message import ReqMethod

from gateway.routing.e2a_proxy import fetch_agent_unary

logger = logging.getLogger(__name__)

#: oauth 回调远端执行的 unary 超时（秒）。换码含 IdP 外部 HTTP 往返
#: （服务端 ``_HTTP_TIMEOUT_S=20s``）：预算来源与 session_metadata 的
#: 控制路径短等待完全不同，须覆盖服务端满额换码再加余量，否则用户看到
#: 失败落地页而凭据实际写入生效（状态与 UI 不一致）。
MCP_OAUTH_CALLBACK_TIMEOUT_SECONDS = 25.0

#: 内部调用通道标识；server 侧对 command.mcp 的内部 action 族按此门控。
_MCP_INTERNAL_CHANNEL_ID = "gateway"

_agent_client: Any = None


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
        """同步入口降级：无法同步等待 WS 往返，按失败告警（不静默成功）。"""
        logger.warning(
            "[mcp-registry-remote] sync entry degraded; "
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
            # 失败语义随线保留：handler 不检查返回值，折叠成 False 会把失败
            # 显示成"授权成功"。服务端错误文案（如 flow unknown/expired）经
            # McpOAuthError 透传，回调页仍得到 400 落地而非 500。
            raise McpOAuthError(
                str((payload or {}).get("error") or "oauth callback failed")
            )
        return bool((payload or {}).get("completed"))


def install() -> None:
    """把 WS 远端实现注册进 ``common.external_providers`` 的 mcp_registry 域。"""
    register("mcp_registry", McpRegistryRemoteProvider())
    logger.info("[mcp-registry-remote] registered mcp_registry provider (WS)")
