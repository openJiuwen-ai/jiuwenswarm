# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求级租户信封：解析 Manager + 绑定 TenantContext / Runtime 切片.

宿主（dispatch / HTTP invoke）经此进入业务 handler；个人版走 SingleStrategy，
企业版走 MultiStrategy。``RequestContext`` 仍属 ``server.context``。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from jiuwenswarm.server.context import RequestContext

__all__ = ["bind_request_tenant"]


@asynccontextmanager
async def bind_request_tenant(ctx: RequestContext) -> AsyncIterator[RequestContext]:
    """按请求解析 Manager，并绑定 TenantContext + Runtime 执行切片。

    handlers 通过 ``ctx.services.agent_manager`` 拿到本租户 Manager；
    SessionProvisioner 等经 ``get_current_agent_manager()`` 读取同一绑定。
    并发安全：不写 Runtime / Provisioner 上的共享字段。
    租户语义委托 Runtime 上的 TenantStrategy（企业 Multi / 个人 Single）。
    """
    from jiuwenswarm.runtime.context import (
        reset_runtime_context,
        set_runtime_context,
    )

    runtime = ctx.services.execution_runtime()
    strategy = getattr(runtime, "tenant_strategy", None)
    resolve = getattr(runtime, "resolve_manager", None)
    if callable(resolve):
        manager = await resolve(ctx.request)
    else:
        manager = ctx.services.agent_manager

    ctx.services.set_scoped_agent_manager(manager)
    runtime_token = set_runtime_context(runtime, manager)
    try:
        if strategy is not None:
            async with strategy.bind_request(ctx.request, manager):
                yield ctx
        else:
            # 兼容未注入 TenantStrategy 的 runtime 桩：仍按身份绑 TenantContext。
            from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
            from jiuwenswarm.enterprise.tenant.tenant_identity import extract_tenant_key

            tenant_ctx = TenantContext.bind(extract_tenant_key(ctx.request))
            try:
                yield ctx
            finally:
                tenant_ctx.reset()
    finally:
        reset_runtime_context(runtime_token)
        ctx.services.set_scoped_agent_manager(None)
