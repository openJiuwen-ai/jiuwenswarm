# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer → Gateway 下行推送抽象与 WebSocket 默认实现。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# send_push 入参里的定向键：value 取 PUSH_TARGET_GATEWAY 时，实现方应把该帧
# 定向投递给 gateway 兄弟进程连接（agent-e2a 管道），而不是按业务 channel_id
# 的 channel-map 路由。desktop 会话的 cron 转发帧若按 channel-map 路由会落到
# PC 前端 stdio 连接并被静默丢弃（2026-11-14「立即执行」断链根因）。
# 实现方负责消费该键，不得透传进 wire（build_server_push_wire 只取已知字段，
# 天然满足）。
PUSH_TARGET_KEY = "claw_push_target"
PUSH_TARGET_GATEWAY = "gateway"

# agent 侧为 gateway 管道连接登记的专用绑定名（_channel_conns 键）。
# 与业务渠道名共存：run_connection 对管道连接额外登记，断连随
# _unbind_channel_conns 一并清理。
GATEWAY_CONN_NAME = "__gateway__"


@runtime_checkable
class GatewayPushTransport(Protocol):
    async def send_push(self, msg: dict[str, Any]) -> None:
        """向 Gateway 发送一条 server_push 语义的消息（与 AgentWebSocketServer.send_push 入参一致）。

        msg 可携带 ``msg[PUSH_TARGET_KEY] = PUSH_TARGET_GATEWAY`` 请求定向
        网关连接；未携带时按业务 channel_id 常规路由。
        """
        ...


class WebSocketGatewayPushTransport:
    """通过进程内 AgentWebSocketServer 单例推送（分离部署 + WebSocket 默认路径）。"""

    async def send_push(self, msg: dict[str, Any]) -> None:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        await AgentWebSocketServer.get_instance().send_push(msg)
