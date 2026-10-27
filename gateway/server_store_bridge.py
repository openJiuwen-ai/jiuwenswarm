# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""过渡装配：把 server 侧存储实现注册进 common 注入点。

gateway 进程当前与 AgentServer 同机部署、共享存储文件；config 面板与
TUI 的 MCP 管理仍直连 server 的 store。分层规则 R1 要求 common 不
import server（gateway 独立成仓时 common 副本必须自包含），因此依赖经
``common.external_providers`` 注入，由本模块在 gateway 启动时加载实现
模块（模块加载即完成自注册）。

**gateway 独立成仓时本文件整体删除**，各域改由 WS 远端实现注册（经既有
Gateway ↔ AgentServer 通道，不新增 HTTP 服务面）；session_metadata 域的
远端实现已就绪（``gateway/routing/session_metadata_remote.py``，复用
``SESSION_GET_METADATA``），可经 ``JIUWENSWARM_SESSION_METADATA_REMOTE=1``
预演；其余域随拆仓批次同模式补齐。届时 ``check_layering.py`` 的 R3 白名单
条目同步消除。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


class _LocalSessionMetadataProvider:
    """本地 session_metadata 存储的双入口适配器。

    保持同步入口（既有消费方零改动）并增补 ``get_session_metadata_async``
    （统一异步接口，供已迁移消费方与 WS 远端实现互换）。持有模块引用而非
    绑定函数，测试对实现模块属性的 ``monkeypatch.setattr`` 对本注入点
    仍然生效（取属性发生在调用时）；异步入口经 ``asyncio.to_thread``
    包装，避免在事件循环内做文件 IO。

    ``__getattr__`` 把其余符号透明委托给实现模块：本域的注册对象历史上
    就是模块本身（全量接口，含 ``update_session_model_selection`` 等写
    方法），适配器只允许"增补异步入口"，不允许收窄接口——否则同进程内
    经 ``provider("session_metadata")`` 取用其它方法的消费方（如
    ``model_selection_store`` 的写路径）会断裂。
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def get_session_metadata(
        self,
        session_id: str,
        cache_bust: bool = False,
        *,
        enable_writeback: bool = True,
    ) -> dict[str, Any]:
        return self._store.get_session_metadata(
            session_id, cache_bust, enable_writeback=enable_writeback
        )

    async def get_session_metadata_async(
        self,
        session_id: str,
        cache_bust: bool = False,
        *,
        enable_writeback: bool = True,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        # user_id 仅供远端实现路由使用；本地文件读无路由身份，接受即忽略
        return await asyncio.to_thread(
            self._store.get_session_metadata,
            session_id,
            cache_bust,
            enable_writeback=enable_writeback,
        )


def _install_session_metadata() -> None:
    """按运行形态注册 session_metadata 域：WS 远端（预演）或本地适配器。"""
    from gateway.routing.session_metadata_remote import remote_enabled

    if remote_enabled():
        # 拆仓预演：不加载 server 侧模块，session_metadata 域走 WS 远端实现
        from gateway.routing import session_metadata_remote

        session_metadata_remote.install()
        return

    import jiuwenswarm.server.runtime.session.session_metadata as store

    from jiuwenswarm.common.external_providers import register

    register("session_metadata", _LocalSessionMetadataProvider(store))


class _LocalMcpRegistryProvider:
    """本地 mcp_registry 的双入口适配器。

    同 ``_LocalSessionMetadataProvider``：同步透传 + ``__getattr__`` 全量
    委托（模块上其余符号经本注入点取用的消费方不受影响）+ 增补
    ``complete_oauth_callback_async``（``to_thread`` 包装，统一异步入口）。
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def complete_oauth_callback(self, query: dict[str, Any]) -> bool:
        return self._store.complete_oauth_callback(query)

    async def complete_oauth_callback_async(self, query: dict[str, Any]) -> bool:
        return await asyncio.to_thread(self._store.complete_oauth_callback, query)


def _install_mcp_domains() -> None:
    """按运行形态注册 MCP 三域：WS 远端（预演）或本地实现。

    核实结论：gateway 进程对 MCP 域的唯一消费是 oauth 回调
    （mcp_registry 域）；状态/凭据的消费全部在 AgentServer 进程。
    远端模式仅注册 mcp_registry 域的 WS 壳，mcp_state / mcp_credential
    保持未注册（调用得到 ``ExternalProviderUnregistered``，注入点原生
    降级），三个 server 侧模块完全不加载。
    """
    from gateway.routing.mcp_remote import remote_enabled

    if remote_enabled():
        from gateway.routing import mcp_remote

        mcp_remote.install()
        return

    import jiuwenswarm.server.runtime.mcp.state_store  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.credential  # noqa: F401
    import jiuwenswarm.server.runtime.mcp.registry as registry

    from jiuwenswarm.common.external_providers import register

    register("mcp_registry", _LocalMcpRegistryProvider(registry))


def install() -> None:
    """加载 server 侧实现模块，触发其向 common 注入点自注册（幂等）。"""
    global _INSTALLED
    if _INSTALLED:
        return
    # import 即注册：各模块文件末尾调用
    # common.external_providers.register(<domain>, <module>)。
    import jiuwenswarm.server.runtime.a2ui.integration  # noqa: F401
    import jiuwenswarm.server.runtime.team_binding_store  # noqa: F401
    import jiuwenswarm.server.runtime.team_entity_store  # noqa: F401

    _install_session_metadata()
    _install_mcp_domains()

    from gateway.runtime.cron import factory as cron_factory

    from jiuwenswarm.common.external_providers import register

    register("cron_store_factory", cron_factory)

    _INSTALLED = True
    logger.info("[server-store-bridge] server store providers installed")
