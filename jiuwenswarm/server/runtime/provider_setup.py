# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Server 家族进程的注入点装配。

common 经 ``common.external_providers`` 反向取用 server 侧存储实现
（分层规则 R1），注册由实现模块在文件末尾自注册（import 即注册）。
server 家族的进程形态中：

- **AgentServer 主进程（Front）**：控制面直接处理 config.*（front/router →
  control/config_service → config_panel handlers），a2ui / team 两个 store /
  mcp_credential 域**不经功能路径自然加载**（实测 Front 链零装配）——
  ``app_agentserver._run`` 入口调用 ``install_front()``，首次使用时加载实现；
- **runtime worker 子进程**：启动链（worker.main → service → ipc/... →
  lifecycle）不加载任何 server runtime 模块，而 ``_preload_runtime_backend``
  会在功能模块加载**之前**调用 ``migrate_legacy_model_selections``（经
  ``provider("session_metadata")``）——必须在进入任何注入点调用前显式
  装配（P0 回归：worker 启动即 ExternalProviderUnregistered，AGENT_READY
  永不到达），``worker.runtime_backend.preload_runtime_backend`` 入口调用。

（gateway 侧的过渡桥已退役删除，gateway 进程现注册 WS 远端实现）
不同，本模块是 server 侧的常驻服务装配。
"""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


class _LazyProvider:
    """Load a provider implementation only when its first attribute is used."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name

    def __getattr__(self, name: str) -> Any:
        return getattr(import_module(self._module_name), name)


def install_front() -> None:
    """Register control-plane providers without loading execution dependencies."""
    from jiuwenswarm.common.external_providers import register, registered_domains

    modules = {
        "mcp_state": "mcp.state_store",
        "mcp_registry": "mcp.registry",
        "mcp_credential": "mcp.credential",
        "a2ui": "a2ui.integration",
        "team_binding_store": "team_binding_store",
        "team_entity_store": "team_entity_store",
        "session_metadata": "session.session_metadata",
    }
    installed = set(registered_domains())
    for domain, module_name in modules.items():
        if domain not in installed:
            register(domain, _LazyProvider(f"jiuwenswarm.server.runtime.{module_name}"))
    if "cron_store_factory" not in installed:
        # 不在 server.runtime 前缀下，单独注册（懒加载同款）
        register("cron_store_factory", _LazyProvider("jiuwenswarm.runtime.cron.factory"))


def install() -> None:
    """加载实现模块，触发其向 common 注入点自注册（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        return
    import jiuwenswarm.server.runtime.mcp.state_store  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.registry  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.credential  # noqa: F401
    import jiuwenswarm.server.runtime.a2ui.integration  # noqa: F401
    import jiuwenswarm.server.runtime.team_binding_store  # noqa: F401
    import jiuwenswarm.server.runtime.team_entity_store  # noqa: F401
    import jiuwenswarm.server.runtime.session.session_metadata  # noqa: F401

    from jiuwenswarm.common.external_providers import register
    from jiuwenswarm.runtime.cron import factory as cron_factory

    register("cron_store_factory", cron_factory)

    _INSTALLED = True
    logger.debug("[server-provider-setup] common external providers installed")
