# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""对话的两种回法。

一次性回包：等整段话说完，再一次性送回去。
流式回包：一边生成一边往外送，对方不用干等。

这两种是默认路径。别的命令没接住的请求，也会走到这里。
编辑器工具的回执不在这里，仍留在 WebSocket 接入里。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


async def handle_unary(ctx: RequestContext) -> None:
    """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
    request = ctx.request
    services = ctx.services
    import jiuwenswarm.server.agent_ws_server as _ws_server
    _TurnOutcomeTracker = _ws_server._TurnOutcomeTracker
    runtime = services.execution_runtime()
    resume_state = await services.open_session_message_resume(request)
    resume_outcome = "unknown"
    resume_error = "resumed turn ended before its outcome was confirmed"

    async def _send_control_event(event: Any) -> None:
        await services.send_runtime_event(
            ctx.sink,
            event,
            streaming=False,
            sequence=0,
        )

    outcome_tracker = _TurnOutcomeTracker()
    try:
        if request.req_method == ReqMethod.CHAT_ANSWER:
            events = await runtime.answer_interaction(
                request,
                trigger_hook=False,
                on_control_event=_send_control_event,
            )
        else:
            events = await runtime.invoke(
                request,
                trigger_hook=False,
                on_control_event=_send_control_event,
            )

        for event in events:
            outcome_tracker.observe(event)
            if event.event_type == "chat.ask_user_question":
                payload = event.payload if isinstance(event.payload, dict) else {}
                await services.persist_repeated_session_question(
                    request,
                    next_interrupt_request_id=str(
                        payload.get("request_id") or ""
                    ).strip(),
                    next_interrupt_source=str(payload.get("source") or "").strip(),
                    resume_state=resume_state,
                )
            await services.send_runtime_event(
                ctx.sink,
                event,
                streaming=False,
                sequence=0,
            )
        if resume_state is not None and resume_state.waiting_user:
            outcome_tracker.waiting_user = True
        resume_outcome = outcome_tracker.outcome()
        resume_error = (
            outcome_tracker.error
            if resume_outcome == "failed"
            else outcome_tracker.unconfirmed_error()
            if resume_outcome == "unknown"
            else ""
        )
    finally:
        try:
            await services.finalize_session_message_resume(
                request,
                resume_state,
                outcome=resume_outcome,
                error=resume_error,
            )
        except Exception:
            logger.exception(
                "[SessionMessaging] failed to finalize resumed unary turn: "
                "request_id=%s",
                request.request_id,
            )
    logger.info(
        "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
        request.request_id,
    )


async def handle_stream(ctx: RequestContext) -> None:
    """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
    request = ctx.request
    services = ctx.services
    import jiuwenswarm.server.agent_ws_server as _ws_server
    _TurnOutcomeTracker = _ws_server._TurnOutcomeTracker
    from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
        PLAN_MODE_EXITED_EVENT_TYPE,
    )
    from jiuwenswarm.runtime import AgentRuntime
    from jiuwenswarm.server.handlers.organization import (
        is_targeted_expert_chat,
        organization_stream_key,
        targeted_expert_runtime_stream,
    )

    session_id = organization_stream_key(request)
    resume_state = await services.open_session_message_resume(request)
    resume_outcome = "unknown"
    resume_error = "resumed turn ended before its outcome was confirmed"
    current_task = asyncio.current_task()
    stream_stop_event = asyncio.Event()
    uses_session_runtime = AgentRuntime.uses_session_runtime(request)
    if current_task is not None and not uses_session_runtime:
        services.session_stream_tasks.setdefault(session_id, {})[current_task] = stream_stop_event

    chunk_count = 0
    outcome_tracker = _TurnOutcomeTracker()
    keepalive = ctx.sink.open_stream_keepalive(request)
    runtime_stream: Any | None = None

    async def _send_control_event(event: Any) -> None:
        await services.send_runtime_event(
            ctx.sink,
            event,
            streaming=True,
            sequence=chunk_count,
        )

    try:
        if is_targeted_expert_chat(request):
            runtime_stream = targeted_expert_runtime_stream(request)
        else:
            runtime_stream = services.execution_runtime().stream(
                request,
                trigger_hook=False,
                on_control_event=_send_control_event,
            )
        keepalive.start()
        async for event in runtime_stream:
            # Runtime control events normally use the callback above. Keep
            # compatibility with custom Runtime implementations without
            # consuming a wire sequence number or resetting the keepalive timer.
            if event.event_type == PLAN_MODE_EXITED_EVENT_TYPE:
                await _send_control_event(event)
                continue
            outcome_tracker.observe(event)
            if event.event_type == "chat.ask_user_question":
                payload = event.payload if isinstance(event.payload, dict) else {}
                await services.persist_repeated_session_question(
                    request,
                    next_interrupt_request_id=str(
                        payload.get("request_id") or ""
                    ).strip(),
                    next_interrupt_source=str(payload.get("source") or "").strip(),
                    resume_state=resume_state,
                )
            chunk_count += 1
            # 通知 keepalive 有真实 chunk 发送，重置空闲计时。
            keepalive.notify_activity(terminal=event.is_complete)
            try:
                sent_original = await services.send_runtime_event(
                    ctx.sink,
                    event,
                    streaming=True,
                    sequence=chunk_count - 1,
                )
                if not sent_original:
                    logger.warning(
                        "[AgentWebSocketServer] 流式响应因单个 chunk 超限而停止: "
                        "request_id=%s seq=%s",
                        request.request_id,
                        chunk_count - 1,
                    )
                    return
            except WebSocketConnectionClosed:
                logger.info(
                    "[AgentWebSocketServer] 流式响应停止，WebSocket 已关闭: request_id=%s",
                    request.request_id,
                )
                return
        if resume_state is not None and resume_state.waiting_user:
            outcome_tracker.waiting_user = True
        resume_outcome = outcome_tracker.outcome()
        resume_error = (
            outcome_tracker.error
            if resume_outcome == "failed"
            else outcome_tracker.unconfirmed_error()
            if resume_outcome == "unknown"
            else ""
        )
    finally:
        try:
            # 尽早阻止新的 keepalive；Runtime 清理仍先完成，以尽快释放其资源。
            keepalive.signal_stop()
            try:
                if runtime_stream is not None:
                    close_stream = getattr(runtime_stream, "aclose", None)
                    if callable(close_stream):
                        await close_stream()
            finally:
                try:
                    # 显式停止并唤醒 keepalive；Task.cancel 只作为有界的兜底。
                    await keepalive.stop()
                finally:
                    # 清除自身的宿主生命周期记录；同 session 的其它请求不受影响。
                    entries = services.session_stream_tasks.get(session_id)
                    if entries is not None and current_task is not None:
                        entries.pop(current_task, None)
                        if not entries:
                            services.session_stream_tasks.pop(session_id, None)
        finally:
            try:
                await services.finalize_session_message_resume(
                    request,
                    resume_state,
                    outcome=resume_outcome,
                    error=resume_error,
                )
            except Exception:
                logger.exception(
                    "[SessionMessaging] failed to finalize resumed stream turn: "
                    "request_id=%s",
                    request.request_id,
                )
    logger.info(
        "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
        request.request_id,
        chunk_count,
    )
