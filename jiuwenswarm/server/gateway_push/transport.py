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
    """Send through the active Runtime host's injected delivery capability."""

    async def send_push(self, msg: dict[str, Any]) -> bool:
        from jiuwenswarm.runtime.host_services import send_runtime_push

        return await send_runtime_push(msg)
