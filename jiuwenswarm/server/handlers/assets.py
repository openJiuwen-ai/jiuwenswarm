# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""assets.export / assets.import 入口 handler（dispatch.HANDLERS 表，HTTP 北向）。

只做请求解包、调用编排层、错误收敛与响应回写，不含业务逻辑。错误
收敛由 ``errors.converge_error_payload`` 统一实现（AssetError 透传 →
内部码查表 → 参数非法 → INTERNAL_ERROR 兜底），WS 适配器入口
（agent_adapter/interface.py）复用同一函数，保证同错同码。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.runtime.assets import service
from jiuwenswarm.server.runtime.assets.errors import converge_error_payload

logger = logging.getLogger(__name__)


def _respond(ctx: RequestContext, ok: bool, payload: dict[str, Any]) -> AgentResponse:
    request = ctx.request
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
        metadata=request.metadata,
    )


async def _finish(ctx: RequestContext, method: str, ok: bool, payload: dict[str, Any]) -> None:
    if ok:
        # 不打 payload 内容：导出成功响应携带含下载 token 的 URL，不入日志。
        logger.info("[assets] %s ok", method)
    elif payload.get("code") == "INTERNAL_ERROR":
        logger.error("[assets] %s internal failure: %s", method, payload.get("error"))
    else:
        logger.warning(
            "[assets] %s rejected: code=%s error=%s",
            method,
            payload.get("code"),
            payload.get("error"),
        )
    await ctx.sink.send_response(_respond(ctx, ok, payload))


async def handle_assets_export(ctx: RequestContext) -> None:
    """assets.export：打包资产并签发短效下载 token。"""
    request = ctx.request
    try:
        # 打包为同步磁盘操作，移出事件循环（与 WS 适配器入口一致）。
        payload = await asyncio.to_thread(
            service.export_asset,
            request.params or {},
            session_id=str(request.session_id or ""),
            user_id=str(request.user_id or ""),
        )
    except Exception as exc:  # noqa: BLE001 入口统一收敛，异常不出栈
        await _finish(ctx, "assets.export", False, converge_error_payload(exc))
        return
    await _finish(ctx, "assets.export", True, payload)


async def handle_assets_import(ctx: RequestContext) -> None:
    """assets.import：从受控上传暂存导入资产包。"""
    request = ctx.request
    try:
        payload = await asyncio.to_thread(service.import_asset, request.params or {})
    except Exception as exc:  # noqa: BLE001 入口统一收敛，异常不出栈
        await _finish(ctx, "assets.import", False, converge_error_payload(exc))
        return
    await _finish(ctx, "assets.import", True, payload)
