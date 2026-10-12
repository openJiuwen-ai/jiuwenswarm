# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：AgentServerClient ABC 已迁 ``gateway_protocol.agent_client``。

本接口为"保留侧"与 Gateway 仓共用的抽象契约（source of truth 为 protocol 包）；
实现方（WebSocket / AgentOS Router 等）各自持有。gateway 树内
``gateway/routing/agent_client.py`` 对本模块的 re-export 链不受影响——三条 import
路径（gateway_protocol / common.client / gateway.routing）为同一类对象。
"""

from __future__ import annotations

from gateway_protocol.agent_client import AgentServerClient  # noqa: F401

__all__ = ["AgentServerClient"]
