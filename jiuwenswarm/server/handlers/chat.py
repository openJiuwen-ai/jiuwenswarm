# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""取消这一轮对话。

取消就是让正在说的那一轮停下来。一次性回包和流式回包不在这里，
它们是没被别的命令接住时的默认路径。
"""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


def _is_client_disconnect_cancel_request(request: AgentRequest) -> bool:
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    return (
        str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
        == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
    )


async def cancel_chat_request(
    ctx: RequestContext,
    *,
    allow_create: bool = False,
    send_response: bool = True,
) -> AgentResponse:
    """Cancel through the transport-independent Runtime operation."""
    from jiuwenswarm.server.handlers.organization import cancel_targeted_expert

    params = ctx.request.params if isinstance(ctx.request.params, dict) else {}
    if params.get("target_team_id"):
        resp = await cancel_targeted_expert(ctx)
    else:
        resp = await ctx.services.execution_runtime().cancel_request(
            ctx.request,
            allow_create=allow_create,
        )
    if send_response:
        await ctx.sink.send_response(resp)
    return resp


async def handle_chat_cancel(ctx: RequestContext) -> None:
    """停掉当前这一轮。对方断开时，停完再清掉这条连接占着的运行状态。"""
    request = ctx.request
    services = ctx.services
    if isinstance(request.params, dict) and request.params.get("target_team_id"):
        await cancel_chat_request(ctx)
        return
    if isinstance(request.params, dict) and request.params.get("wait_for_stop"):
        try:
            await services.execution_runtime().stop_session_for_archive(
                channel_id=request.channel_id or "default",
                session_id=request.session_id or "default",
            )
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"success": True},
            )
        except Exception as exc:  # noqa: BLE001
            response = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"success": False, "error": str(exc)},
            )
        await ctx.sink.send_response(response)
        return
    # 中断请求：根据 intent 决定是否取消流式任务
    sid = request.session_id or "default"
    intent = request.params.get("intent", "cancel") if isinstance(request.params, dict) else "cancel"
    cleanup_after_cancel = _is_client_disconnect_cancel_request(request)

    # 只有 cancel/supplement 才取消流式任务
    # pause/resume 不取消，因为任务仍在运行（pause 在 checkpoint 阻塞，resume 解除阻塞）
    stream_tasks: list[asyncio.Task] = []
    if intent in ("cancel", "supplement"):
        entries = services.session_stream_tasks.get(sid, {})
        for stream_task, stream_stop_event in list(entries.items()):
            if stream_task.done():
                continue
            logger.info(
                "[AgentWebSocketServer] cancel: 终止 session 流式任务: session_id=%s intent=%s",
                sid,
                intent,
            )
            stream_stop_event.set()
            stream_task.cancel()
            stream_tasks.append(stream_task)

    cancel_response: AgentResponse | None = None
    try:
        # 专门处理 cancel，复用已有 agent（不再 fallthrough 到默认回包）。
        # allow_create=False：找不到已有 agent 时不 fallback 新建。
        cancel_response = await cancel_chat_request(
            ctx,
            allow_create=False,
            send_response=not cleanup_after_cancel,
        )
    finally:
        if stream_tasks:
            results = await asyncio.gather(*stream_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.warning(
                        "[AgentWebSocketServer] cancel: stream task cleanup failed: "
                        "session_id=%s intent=%s error=%s",
                        sid,
                        intent,
                        result,
                    )
        if cleanup_after_cancel and intent in ("cancel", "supplement"):
            cleanup_succeeded = await services.cleanup_client_disconnect_session_runtime(request)
            if cancel_response is not None:
                if not cleanup_succeeded:
                    cancel_response.ok = False
                    cancel_response.payload = {
                        "event_type": "chat.interrupt_result",
                        "success": False,
                        "error": "session runtime cleanup failed",
                    }
                await ctx.sink.send_response(cancel_response)
