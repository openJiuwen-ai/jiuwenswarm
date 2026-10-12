# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServerClient - 与 AgentServer 通信的客户端接口（南北向契约）。

本接口为"保留侧"与 Gateway 仓共用的抽象契约：Gateway 仓另持副本，
两仓通过 E2A 线协议对齐；实现方（WebSocket / AgentOS Router 等）各自持有。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk


class AgentServerClient(ABC):
    """AgentServer WebSocket 客户端接口."""

    @abstractmethod
    async def connect(self, uri: str) -> None:
        """建立与 AgentServer 的 WebSocket 连接."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接."""
        ...

    @abstractmethod
    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        """缓存或更新服务端配置快照，供自定义 client 后续使用."""
        ...

    @abstractmethod
    async def send_request(
        self,
        envelope: E2AEnvelope,
        *,
        timeout: float | None = None,
    ) -> AgentResponse:
        """发送 E2A 信封，等待完整响应.

        Args:
            envelope: E2A 信封.
            timeout: 等待响应的上限（秒）。``None`` 时使用客户端默认值。
                调用方可传入更大的值以覆盖默认上限（例如 cron 任务的
                ``timeout_seconds``），使任务自身的超时真正生效，而非被内层
                默认值提前截断.
        """
        ...

    @abstractmethod
    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        """发送 E2A 信封，流式接收响应."""
        ...