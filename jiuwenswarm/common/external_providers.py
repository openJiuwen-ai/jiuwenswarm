# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Server 侧存储/配置能力向 common 的注入点。

分层规则 R1：common 禁止 import server / symphony。历史上 common 的
MCP 配置、配置面板等模块直接延迟 import ``jiuwenswarm.server.runtime.*``
——gateway 独立成仓时 common 副本无法自带这些实现。本模块把依赖反转：
common 只面向 ``provider(domain)`` 取实现，注册责任在使用方进程：

- **AgentServer 进程**：实现模块在各自文件末尾自注册（import 即注册），
  server 功能路径天然加载它们；
- **gateway 进程（拆仓前过渡）**：``gateway/server_store_bridge.py`` 在
  启动时加载同模块完成注册——该桥接文件在 gateway 独立成仓时整体删除，
  各域改由 WS 远端实现注册（经既有 Gateway ↔ AgentServer 通道，不新增
  HTTP 服务面；session_metadata 域已就绪
  ``gateway/routing/session_metadata_remote.py``，其余域随拆仓批次补齐）；
- **gateway 仓（拆仓后）**：注册 WS 远端实现，或对不可用域保持未注册
  （调用得到 ``ExternalProviderUnregistered``，功能降级报错）。

实现对象通常就是实现模块本身；返回模块对象意味着测试对实现模块属性
的 ``monkeypatch.setattr`` 对本注入点同样生效（取属性发生在调用时）。

域清单（与 ``check_layering.py`` 的 R3 白名单一一对应）：

- ``mcp_state``        → ``server.runtime.mcp.state_store``
- ``mcp_registry``     → ``server.runtime.mcp.registry``
- ``mcp_credential``   → ``server.runtime.mcp.credential``
- ``a2ui``             → ``server.runtime.a2ui.integration``
- ``team_binding_store`` → ``server.runtime.team_binding_store``
- ``team_entity_store``  → ``server.runtime.team_entity_store``
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ExternalProviderUnregistered(RuntimeError):
    """请求的外部能力域尚未注册实现（进程未装配或功能不可用）。"""


_providers: dict[str, Any] = {}


def register(domain: str, impl: Any) -> None:
    """注册一个能力域的实现（重复注册以后者为准，供测试覆盖）。"""
    _providers[domain] = impl
    logger.debug("[external-providers] registered %s", domain)


def provider(domain: str) -> Any:
    """取能力域实现；未注册时抛 ``ExternalProviderUnregistered``。"""
    impl = _providers.get(domain)
    if impl is None:
        raise ExternalProviderUnregistered(
            f"external provider '{domain}' is not registered in this process"
        )
    return impl


def registered_domains() -> list[str]:
    """当前已注册的能力域（诊断用）。"""
    return sorted(_providers)
