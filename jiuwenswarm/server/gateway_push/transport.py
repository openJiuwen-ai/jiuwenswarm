# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer → Gateway 下行推送抽象与 WebSocket 默认实现。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GatewayPushTransport(Protocol):
    async def send_push(self, msg: dict[str, Any]) -> bool:
        """向 Gateway 发送一条消息，并返回是否已写入传输。"""
        ...


class WebSocketGatewayPushTransport:
    """通过进程内 AgentWebSocketServer 单例推送（分离部署 + WebSocket 默认路径）。"""

    async def send_push(self, msg: dict[str, Any]) -> bool:
        from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

        return bool(await AgentWebSocketServer.get_instance().send_push(msg))
