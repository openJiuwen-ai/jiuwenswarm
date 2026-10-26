# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限域 handler

dispatch导入由``_register_permissions_methods()``展开
"""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.server.handlers._shared import _uses_tenant_pool

logger = logging.getLogger(__name__)

# 后台权限重载任务引用集合,防止 fire-and-forget 任务被 GC 提前回收。
# task 完成后自动从集合移除(Python 官方推荐模式)。
_background_permission_reload_tasks: set[asyncio.Task] = set()


def _log_permission_reload_failure(task: asyncio.Task) -> None:
    """后台权限重载任务完成回调: 仅在异常时记 debug(与原同步 try/except 语义一致)。"""
    exc = task.exception()
    if exc is not None:
        logger.debug(
            "[AgentWebSocketServer] post-permissions reload failed (non-critical)",
            exc_info=exc,
        )


async def handle_permissions_config(ctx: RequestContext) -> None:
    """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
    request = ctx.request
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import \
        dispatch_permissions_config_request

    def runtime_catalog() -> dict[str, dict[str, str]]:
        from jiuwenswarm.server.runtime.tool_catalog import (
            collect_tools_catalog_from_swarms,
        )

        if _uses_tenant_pool(request):
            pool = TenantAgentPool.peek_instance()
            if pool is None:
                return {}
            agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
            manager = pool.get_agent_manager_nowait(
                agent_id,
                service_id,
                workspace_key,
            )
            managers = [manager] if manager is not None else []
        else:
            managers = [ctx.services.agent_manager]
        swarms: list[Any] = []
        for manager in managers:
            iterator = getattr(manager, "iter_jiuwenswarm_instances", None)
            if callable(iterator):
                swarms.extend(iterator())
        return collect_tools_catalog_from_swarms(swarms)

    resp = dispatch_permissions_config_request(
        request,
        get_runtime_tools_catalog=runtime_catalog,
    )

    # After any successful mutation (delete / update / set / create),
    # reload agent config so the PermissionInterruptRail picks up the
    # change immediately instead of waiting for the next tool call's
    # get_permissions_snapshot refresh.
    read_only_methods = {
        ReqMethod.PERMISSIONS_ENABLED_GET,
        ReqMethod.PERMISSIONS_WORKSPACE_ENABLE_GET,
        ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET,
        ReqMethod.PERMISSIONS_TOOLS_GET,
        ReqMethod.PERMISSIONS_TOOLS_LIST,
        ReqMethod.PERMISSIONS_RULES_GET,
        ReqMethod.PERMISSIONS_APPROVAL_OVERRIDES_GET,
    }
    if resp.ok and request.req_method not in read_only_methods:
        # 后台异步重载: 不阻塞权限 RPC 回包(避免 reload 慢导致 AgentServer
        # request timed out)。reload_agents_config 内部有 _reload_lock 串行化
        # + fingerprint 去重,fire-and-forget 安全。
        reload_task = asyncio.create_task(
            ctx.services.agent_manager.reload_agents_config(get_config(), None)
        )
        _background_permission_reload_tasks.add(reload_task)
        reload_task.add_done_callback(_background_permission_reload_tasks.discard)
        reload_task.add_done_callback(_log_permission_reload_failure)

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


