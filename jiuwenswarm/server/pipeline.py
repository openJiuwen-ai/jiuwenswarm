# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求流水线，ws与http两种传输的汇合点"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from openjiuwen.core.common.logging import server_logger
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.log_context import bind_log_session, unbind_log_session
from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.common.request_ext import lift_from_metadata, reset_ext
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import AgentServerChatHookContext
from jiuwenswarm.perf.collector import get_perf_collector
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.dispatch import dispatch_with_context
from jiuwenswarm.server.handlers._default import _handle_stream, _handle_unary
from jiuwenswarm.server.handlers._shared import _request_query_text
from jiuwenswarm.server.handlers.chat import _ensure_auto_team_binding_for_chat
from jiuwenswarm.server.request_summary_log import (
    STATUS_CANCELLED,
    STATUS_CLIENT_GONE,
    STATUS_FAILED,
    STATUS_OK,
    build_summary_payload,
    classify_error_class,
    emit_request_summary,
)
from jiuwenswarm.server.runtime.swarm_skill_session import SwarmSkillSessionError
from jiuwenswarm.server.runtime.team_binding_store import TeamSessionBindingError
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)


def _emit_monitoring_summary(
    request: AgentRequest,
    *,
    t0_monotonic: float,
    t0_wall: float,
    pipeline_status: str,
    pipeline_error: BaseException | None,
    error_class: str,
) -> None:
    """Request-end monitoring summary line (issue #6745).

    Consumes the finalized perf summary retained by the collector (the adapter
    finalize runs before this pipeline finally-block, and finalize pops the
    accumulator — retention is what keeps it readable here). Chat-family
    requests that follow a pending ask_user card additionally carry the
    cross-request HITL wait (the resume may be CHAT_RESUME / CHAT_ANSWER or a
    fresh CHAT_SEND, depending on the channel). Never raises; summary problems
    must not affect the request outcome.
    """
    try:
        collector = get_perf_collector()
        perf = collector.pop_finalized_summary(request.request_id)
        hitl_wait_total_ms: float | None = None
        hitl_wait_count = 0
        if request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        ):
            # The ask line itself must not consume its own registration
            # (exclude_request_id keeps it for the follow-up request). The
            # resume may arrive as CHAT_RESUME / CHAT_ANSWER, or — flash /
            # officeclaw style — as a fresh CHAT_SEND in the same session;
            # either way the wait is attributed to this second line.
            ask = collector.pop_hitl_ask(
                request.session_id or "",
                exclude_request_id=request.request_id,
            )
            if ask is not None:
                hitl_wait_total_ms = max(0.0, (t0_wall - ask[1]) * 1000.0)
                hitl_wait_count = 1
        payload = build_summary_payload(
            request=request,
            pipeline_status=pipeline_status,
            pipeline_error=pipeline_error,
            error_class=error_class,
            t0_monotonic=t0_monotonic,
            t_end_monotonic=time.monotonic(),
            t0_wall=t0_wall,
            perf=perf,
            hitl_wait_total_ms=hitl_wait_total_ms,
            hitl_wait_count=hitl_wait_count,
        )
        emit_request_summary(payload)
    except Exception:
        logger.debug(
            "[request_summary] pipeline emit failed: request_id=%s",
            request.request_id,
            exc_info=True,
        )


def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
    return request.req_method in (
        ReqMethod.CHAT_SEND,
        ReqMethod.CHAT_RESUME,
        ReqMethod.CHAT_ANSWER,
    )


async def _trigger_before_chat_request_hook(request: AgentRequest) -> None:
    if not _should_trigger_before_chat_request_hook(request):
        return
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    params = request.params if isinstance(request.params, dict) else {}
    if not isinstance(request.params, dict):
        request.params = params
    ctx = AgentServerChatHookContext(
        request_id=request.request_id,
        channel_id=request.channel_id,
        session_id=request.session_id,
        req_method=request.req_method.value if request.req_method is not None else None,
        params=params,
    )
    await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)


async def _consume_runtime_approval_reply(ctx: RequestContext, request: AgentRequest) -> bool:
    """消费 DLL 运行时审批应答；True 表示请求已处理，不再进入 Agent 分发。

    卡片 ``request_id`` 带 ``dll-runtime:`` 前缀，此时工具仍在进程内等待。把它
    当成新的对话轮次会让 Agent 重启一次工具，所以 WS/HTTP 都要在流水线入口消费。
    """
    if request.req_method != ReqMethod.CHAT_SEND:
        return False
    params = request.params if isinstance(request.params, dict) else {}
    request_id = params.get("request_id")
    if not isinstance(request_id, str) or not request_id.startswith("dll-runtime:"):
        return False
    accepted = False
    if sys.platform == "win32":
        from jiuwenswarm.common.config import is_dll_sandbox_enabled

        if is_dll_sandbox_enabled():
            from jiuwenswarm.server.runtime.runtime_guard import resolve_runtime_reply

            accepted = bool(resolve_runtime_reply(request.session_id, params))
    await ctx.sink.send_unary(
        AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=accepted,
            payload={"event_type": "chat.runtime_approval_result", "accepted": accepted},
        ),
        response_id=request.request_id,
    )
    return True


async def dispatch_parsed_request(
    ctx: RequestContext,
    request: AgentRequest,
    *,
    peer: Any = None,
) -> None:
    """已解析出 ``AgentRequest`` 之后的**通用处理流水线**。

    HTTP 入口因此可以直接构造 ``AgentRequest`` 调本方法。
    """
    # 监控汇总日志（#6745）计时基线：recv 即 t0，total_ms/DurationMs 与全部
    # t_* 阶段偏移都以这里的 monotonic 为原点。
    _t0_monotonic = time.monotonic()
    _t0_wall = time.time()
    _pipeline_status = STATUS_OK
    _pipeline_error: BaseException | None = None
    _error_class = "none"
    _emit_summary = True
    log_bind = bind_log_session(getattr(request, "session_id", None))
    try:
        if await _consume_runtime_approval_reply(ctx, request):
            # DLL 审批应答不是真实对话轮次（工具仍在进程内等待），不出汇总行。
            _emit_summary = False
            return
        logger.info(
            "[AgentWebSocketServer] 收到请求: request_id=%s channel_id=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.is_stream,
        )
        if request.is_stream and request.req_method == ReqMethod.CHAT_SEND:
            logger.info("[latency] stage=0 name=recv request_id=%s", request.request_id)

        # First touch point of frontend chat input inside AgentServer: record it through the
        # agent-core logging system so it lands in the unified agent log stream.
        if request.req_method == ReqMethod.CHAT_SEND:
            server_logger.info(
                "[AgentServer] chat input received: request_id=%s session_id=%s channel_id=%s query=%s",
                request.request_id,
                request.session_id,
                request.channel_id,
                preview_text(_request_query_text(request)),
            )

        _ext_token = lift_from_metadata(request.metadata)
        try:
            if request.channel_id == "acp" and request.req_method != ReqMethod.INITIALIZE:
                metadata = dict(request.metadata or {})
                ws_caps = ctx.services.get_acp_client_capabilities(ctx.connection_id)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or ctx.services.agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata

            await _trigger_before_chat_request_hook(request)

            # 表驱动分发dispatch.py：命中即返回；未命中落到下方默认路径agent去调用。
            if await dispatch_with_context(ctx, request):
                return
            await _ensure_auto_team_binding_for_chat(ctx, request)
            if request.is_stream:
                await _handle_stream(ctx, request)
            else:
                await _handle_unary(ctx, request)
        except asyncio.CancelledError:
            # 流式任务被 interrupt 取消，属正常路径，记一条 info 即可 —— 但**必须重抛**。
            #
            # 吞掉它会让任务以「正常返回」告终：``task.cancelled()`` 为 False、
            # ``asyncio.gather(..., return_exceptions=True)`` 拿到 None 而非 CancelledError，
            # 上层再也分不清「跑完了」和「被中断了」。``handlers/chat.py`` 的 cancel 分支
            # 正是按「gather 会收到 CancelledError」写的。
            #
            # 更实际的后果：吞掉之后协程会继续执行后续 finally 里的 await
            #（如 ``SSESink.finish``），而此时任务的取消请求已被消费，
            # 那些 await 若阻塞就再没有人能打断它。
            logger.info(
                "[AgentWebSocketServer] 任务被取消: request_id=%s session_id=%s",
                request.request_id,
                request.session_id,
            )
            _pipeline_status = STATUS_CANCELLED
            raise
        except WebSocketConnectionClosed as e:
            _pipeline_status = STATUS_CLIENT_GONE
            logger.info(
                "[AgentWebSocketServer] WebSocket 已关闭，放弃请求回包: %s",
                format_ws_diagnostics(
                    {
                        "request_id": request.request_id,
                        "channel_id": request.channel_id,
                        "session_id": request.session_id,
                        "is_stream": request.is_stream,
                    },
                    describe_ws_peer(peer),
                    describe_ws_exception(e),
                ),
            )
        except (SwarmSkillSessionError, TeamSessionBindingError) as e:
            _pipeline_status = STATUS_FAILED
            _pipeline_error = e
            _error_class = "policy"
            logger.info(
                "[AgentWebSocketServer] session policy rejected: "
                "request_id=%s session_id=%s code=%s",
                request.request_id,
                getattr(e, "session_id", request.session_id),
                e.code,
            )
            error_resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload=dict(e.payload),
                metadata=request.metadata,
            )
            wire = encode_agent_response_for_wire(
                error_resp, response_id=request.request_id
            )
            try:
                await ctx.sink.send_wire(wire)
            except WebSocketConnectionClosed as send_exc:
                logger.info(
                    "[AgentWebSocketServer] WebSocket 已关闭，swarm 策略错误未发送: %s",
                    format_ws_diagnostics(
                        {
                            "request_id": request.request_id,
                            "channel_id": request.channel_id,
                            "session_id": request.session_id,
                            "is_stream": request.is_stream,
                        },
                        describe_ws_peer(peer),
                        describe_ws_exception(send_exc),
                    ),
                )
            except Exception as send_err:
                logger.warning(
                    "[AgentWebSocketServer] swarm 策略错误回写失败: request_id=%s: %s",
                    request.request_id,
                    send_err,
                )
        except Exception as e:
            _pipeline_status = STATUS_FAILED
            _pipeline_error = e
            _error_class = classify_error_class(e)
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
            )
            error_resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(
                error_resp, response_id=request.request_id
            )
            try:
                await ctx.sink.send_wire(wire)
            except WebSocketConnectionClosed as send_exc:
                logger.info(
                    "[AgentWebSocketServer] WebSocket 已关闭，错误响应未发送: %s",
                    format_ws_diagnostics(
                        {
                            "request_id": request.request_id,
                            "channel_id": request.channel_id,
                            "session_id": request.session_id,
                            "is_stream": request.is_stream,
                        },
                        describe_ws_peer(peer),
                        describe_ws_exception(send_exc),
                    ),
                )
            except Exception as send_err:
                logger.warning(
                    "[AgentWebSocketServer] 错误回写失败: request_id=%s: %s",
                    request.request_id,
                    send_err,
                )
        finally:
            reset_ext(_ext_token)
    finally:
        if _emit_summary:
            _emit_monitoring_summary(
                request,
                t0_monotonic=_t0_monotonic,
                t0_wall=_t0_wall,
                pipeline_status=_pipeline_status,
                pipeline_error=_pipeline_error,
                error_class=_error_class,
            )
        unbind_log_session(*log_bind)
