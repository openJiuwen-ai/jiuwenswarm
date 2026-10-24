# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""过渡装配：把 server 侧存储实现注册进 common 注入点。

gateway 进程当前与 AgentServer 同机部署、共享存储文件；config 面板与
TUI 的 MCP 管理仍直连 server 的 store。分层规则 R1 要求 common 不
import server（gateway 独立成仓时 common 副本必须自包含），因此依赖经
``common.external_providers`` 注入，由本模块在 gateway 启动时加载实现
模块（模块加载即完成自注册）。

**gateway 独立成仓时本文件整体删除**，由 server 侧 HTTP API（A-API）
的远端实现替代注册；届时 ``check_layering.py`` 的 R3 白名单条目同步
消除。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    """加载 server 侧实现模块，触发其向 common 注入点自注册（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        return
    # import 即注册：各模块文件末尾调用
    # common.external_providers.register(<domain>, <module>)。
    import jiuwenswarm.server.runtime.mcp.state_store  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.registry  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.credential  # noqa: F401
    import jiuwenswarm.server.runtime.a2ui.integration  # noqa: F401
    import jiuwenswarm.server.runtime.team_binding_store  # noqa: F401
    import jiuwenswarm.server.runtime.team_entity_store  # noqa: F401
    import jiuwenswarm.server.runtime.session.session_metadata  # noqa: F401

    from gateway.runtime.cron import factory as cron_factory

    from jiuwenswarm.common.external_providers import register

    register("cron_store_factory", cron_factory)

    _INSTALLED = True
    logger.info("[server-store-bridge] server store providers installed")
