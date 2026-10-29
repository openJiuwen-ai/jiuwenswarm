# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""调用业务函数前后的包装。负责把逃出来的异常收成 HTTP 错误。

这里不查分发表，也不按请求种类做分支。聊天前的准备在聊天路由里做。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext, bind_request_tenant

logger = logging.getLogger(__name__)

Handler = Callable[[RequestContext], Awaitable[None]]


@asynccontextmanager
async def _request_tenant_scope(ctx: RequestContext):
    """HTTP 入口：有 Runtime 则绑 Manager+TenantContext；否则仅绑 TenantContext."""
    services = getattr(ctx, "services", None)
    execution_runtime = getattr(services, "execution_runtime", None) if services else None
    if callable(execution_runtime):
        try:
            runtime = execution_runtime()
        except Exception:  # noqa: BLE001 — 测试假对象可能没有完整工具箱
            runtime = None
        if runtime is not None and hasattr(runtime, "resolve_manager"):
            async with bind_request_tenant(ctx):
                yield
            return

    from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
    from jiuwenswarm.enterprise.tenant.tenant_identity import extract_tenant_key

    # 无完整 Runtime 时仍按请求绑 TenantContext（企业盘路径 / 个人 key）。
    with TenantContext.scoped(extract_tenant_key(ctx.request)):
        yield


def _error_response(ctx: RequestContext, exc: BaseException) -> AgentResponse:
    return AgentResponse(
        request_id=ctx.request.request_id,
        channel_id=ctx.request.channel_id,
        ok=False,
        payload={"code": "INTERNAL_ERROR", "error": str(exc)},
        metadata=ctx.request.metadata,
    )


async def invoke_unary(ctx: RequestContext, handler: Handler) -> None:
    """调用一次性回包。异常变成一条失败 JSON，取消仍向外抛。"""
    try:
        async with _request_tenant_scope(ctx):
            await handler(ctx)
    except Exception as exc:
        logger.exception(
            "[AgentHTTPServer] request failed: request_id=%s",
            ctx.request.request_id,
        )
        ctx.sink.status_code = 500
        await ctx.sink.send_response(_error_response(ctx, exc))


async def invoke_stream(ctx: RequestContext, handler: Handler) -> None:
    """调用流式回包。取消必须继续向外抛，不能吞掉。"""
    try:
        async with _request_tenant_scope(ctx):
            await handler(ctx)
    except asyncio.CancelledError:
        logger.info(
            "[AgentHTTPServer] stream cancelled: request_id=%s session_id=%s",
            ctx.request.request_id,
            ctx.request.session_id,
        )
        raise
    except Exception as exc:
        logger.exception(
            "[AgentHTTPServer] stream failed: request_id=%s",
            ctx.request.request_id,
        )
        await ctx.sink.send_response(_error_response(ctx, exc))
    finally:
        close = getattr(ctx.sink, "close", None)
        if callable(close):
            await close()
