# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""聊天域 handler"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from weakref import WeakValueDictionary

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _apply_resolved_mode_to_request,
    _effective_config_for_request,
    _is_team_metadata_mode,
    _plan_exited_sessions,
    _request_query_text,
    _session_team_binding_lock,
    _sessions_dir_for_request,
    _uses_tenant_pool,
    resolve_agent_request_mode,
    resolve_request_project_dir,
)
from jiuwenswarm.server.handlers.team import _create_generated_team_binding
from jiuwenswarm.server.utils.utils import is_team_params

logger = logging.getLogger(__name__)


def _is_client_disconnect_cancel_request(request: AgentRequest) -> bool:
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    return (
        str(metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY) or "").strip()
        == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
    )


async def _cleanup_client_disconnect_session_runtime(ctx, request: AgentRequest) -> bool:
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(request.session_id or params.get("session_id") or "").strip()
    if not session_id:
        return False
    channel_id = request.channel_id or "default"
    try:
        cleaned = await ctx.services.agent_manager.cleanup_session_runtime(
            channel_id=channel_id,
            session_id=session_id,
        )
        logger.info(
            "[AgentWebSocketServer] client disconnect session runtime cleanup: "
            "channel_id=%s session_id=%s cleaned=%s",
            channel_id,
            session_id,
            cleaned,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[AgentWebSocketServer] client disconnect session runtime cleanup failed: "
            "channel_id=%s session_id=%s error=%s",
            channel_id,
            session_id,
            exc,
        )
        return False
    finally:
        # Persisted history remains on disk, but this connection-scoped
        # marker must not grow with every short-lived TUI process. Mode
        # locks are weakly cached and disappear automatically after their
        # last active/waiting user releases them.
        _plan_exited_sessions.discard(session_id)


def _build_team_interrupt_response(
    request: AgentRequest,
    *,
    intent: str,
    success: bool,
    message: str,
) -> AgentResponse:
    """Build a chat.interrupt_result response for the team-mode short-circuit.

    Mirrors the payload shape of ``JiuWenSwarm._build_interrupt_result_response``
    (interface.py:2086) — event_type/intent/success/message — without reaching
    into that class's protected API from a handler module (G.CLS.11).
    """
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": success,
            "message": message,
        },
        metadata=request.metadata,
    )


def _find_tenant_pool_agent_owning_session(
    tenant_manager: Any,
    session_id: str | None,
) -> Any | None:
    """在租户 AgentManager 中定位持有该 session 的 JiuWenSwarm。

    cancel 请求不带 project_dir，按 (channel, mode, project) 缓存键查会查不到
    或命中其它会话的 agent。agent 本身按 project 键缓存、session adapter 按
    session_id 缓存在其根 adapter 的 ``_session_adapters`` 里，因此按 session
    归属定位才能把 cancel 送到正确的 ``process_interrupt``。
    """
    target_sid = str(session_id or "").strip()
    if not target_sid:
        return None
    for channel_agents in getattr(tenant_manager, "agents", {}).values():
        if not isinstance(channel_agents, dict):
            continue
        for agent in channel_agents.values():
            adapter = getattr(agent, "_adapter", None)
            session_adapters = getattr(adapter, "_session_adapters", None)
            if isinstance(session_adapters, dict) and target_sid in session_adapters:
                return agent
    return None


async def _handle_tenant_pool_cancel(
    ctx: RequestContext,
    request: AgentRequest,
    *,
    send_response: bool,
) -> AgentResponse | None:
    """officeclaw/租户池请求的 cancel 路由：按 session 定位租户 agent 并走其
    ``process_message`` → ``_process_interrupt`` → adapter ``process_interrupt``。

    Returns:
        已处理的 AgentResponse；未找到持有该 session 的 agent 或租户池
        解析链路异常时返回 None（调用方回退到后续默认逻辑，不影响 cancel
        响应返回）。
    """
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    try:
        pool = ctx.services.tenant_pool()
        agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
        agent_id, service_id = pool.resolve_control_rpc_tenant(
            request, agent_id, service_id
        )
        tenant_manager = await pool.get_agent_manager(
            agent_id, service_id, workspace_key
        )
    except Exception:
        logger.warning(
            "[AgentWebSocketServer] cancel(tenant pool): failed to resolve tenant "
            "manager session=%s channel_id=%s",
            request.session_id,
            request.channel_id,
            exc_info=True,
        )
        return None
    agent = _find_tenant_pool_agent_owning_session(tenant_manager, request.session_id)
    if agent is None:
        logger.info(
            "[AgentWebSocketServer] cancel(tenant pool): no agent owns session=%s "
            "channel_id=%s",
            request.session_id,
            request.channel_id,
        )
        return None
    logger.info(
        "[AgentWebSocketServer] cancel(tenant pool): routed to session-owned agent "
        "session=%s channel_id=%s",
        request.session_id,
        request.channel_id,
    )
    resp = await agent.process_message(request)
    if getattr(resp, "agent_ref", None) is None:
        resp.agent_ref = request.agent_ref
    if send_response:
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
    return resp


async def _handle_cancel(
    ctx: RequestContext,
    *,
    allow_create: bool = False,
    send_response: bool = True,
) -> AgentResponse:
    """处理 CHAT_CANCEL 中断请求：复用已有 agent 实例，避免创建新实例。

    cancel 请求的 params 中可能没有 mode 信息，如果走 _handle_unary 的 get_agent(mode) 路径
    会按默认 mode 创建新的 agent 实例，导致 interrupt 设置到空实例上，无法终止真正运行的 agent。
    因此 cancel 请求必须直接定位已有 agent 来处理。

    默认 allow_create=False：找不到已有 agent 时不 fallback 新建。
    原作者的 fallback 是为"缓存竞态/意外清空"异常兜底设计；但在"agent 首次初始化慢"场景下有害——
    此时目标 agent 仍在 create_instance 的 ensure_initialized 中、尚未写入缓存，get_agent_nowait
    返回 None，fallback 会新建第二个 agent，既无法取消正在初始化的第一个（它在线程里跑、cancel 停不掉
    其同步段），又叠一次阻塞、拖垮 gateway 等不到响应而 timeout。
    改动3 已让主事件循环在初始化期间保持响应（esc 能被读到），配合这里 allow_create=False 直接回
    success，gateway 拿到结果不 timeout、前端停转圈。后端那个初始化仍会在子线程跑完、随后进缓存复用，
    不影响后续任务。
    """
    request = ctx.request
    channel_id = request.channel_id or "default"

    # Team-mode short-circuit: the team run is owned by TeamManager
    # (team_manager._stream_tasks, team_helpers.py register_stream_task),
    # NOT agent_manager. agent_manager.get_agent_nowait returns None for
    # team mode (no agent registered), so the generic
    # agent.process_message -> _process_interrupt -> _process_team_interrupt
    # chain (chat.py:166, interface.py:2048/2107) is never reached and the
    # team run driver keeps streaming after a stop click (verified
    # 2026-08-24: 19:08 cancel did not stop round1; only team.session.reset
    # did at 19:20, via the same _cleanup_runtime_locals primitive).
    # Route team-mode interrupts straight to team_manager primitives,
    # mirroring _process_team_interrupt (interface.py:2124-2168) so the
    # behaviour matches the canonical (but unreachable) agent path. Non-team
    # and all agent/chat logic below is untouched.
    params = request.params if isinstance(request.params, dict) else {}
    intent = params.get("intent", "cancel")
    if is_team_params(params):
        from jiuwenswarm.agents.harness.team import get_team_manager

        team_manager = get_team_manager(channel_id)
        sid = request.session_id or "default"
        reason = f"interrupt(intent={intent}): "

        if intent == "resume":
            resp = _build_team_interrupt_response(
                request,
                intent=intent,
                success=True,
                message="团队暂停后，直接发送下一条消息即可继续。",
            )
        elif intent == "pause":
            paused = await team_manager.pause_session_runtime(sid, reason=reason)
            resp = _build_team_interrupt_response(
                request,
                intent=intent,
                success=paused,
                message="团队已暂停" if paused else "当前没有可暂停的团队任务",
            )
        elif intent == "cancel":
            cancelled = await team_manager.cancel_session_runtime(sid, reason=reason)
            resp = _build_team_interrupt_response(
                request,
                intent=intent,
                success=cancelled,
                message="团队当前执行已结束" if cancelled else "当前没有可取消的团队任务",
            )
        else:
            resp = _build_team_interrupt_response(
                request,
                intent=intent,
                success=False,
                message=f"团队模式暂不支持中断意图: {intent}",
            )

        if send_response:
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            await ctx.sink.send_wire(wire)
        return resp

    # 1. 尝试按 params 中的 mode 查找已有 agent
    project_dir = resolve_request_project_dir(request)
    mode_param = request.params.get("mode", "")

    # 0. 租户池（officeclaw/E2A）：流式 agent 注册在 TenantAgentPool 的租户
    # AgentManager 里，不在 ctx.services.agent_manager 中；且 cancel 请求不带
    # project_dir，按缓存键查也会 miss。必须按 session 归属定位租户 agent，
    # 否则 cancel 永远到不了 adapter.process_interrupt——round 不终止、
    # interaction output lease 不释放，后续消息全部被 ACK-only 丢弃。
    if _uses_tenant_pool(request):
        tenant_resp = await _handle_tenant_pool_cancel(
            ctx, request, send_response=send_response
        )
        if tenant_resp is not None:
            return tenant_resp

    if mode_param:
        mode, sub_mode, _canonical = resolve_agent_request_mode(mode_param)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = ctx.services.agent_manager.get_agent_nowait(
            channel_id,
            mode=agent_mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )
    else:
        agent = None

    # 2. 如果按 mode 没找到，用 get_agent_nowait 找任何已有 agent
    if agent is None:
        agent = ctx.services.agent_manager.get_agent_nowait(channel_id, project_dir=project_dir)

    resp: AgentResponse | None = None

    if agent is None and not allow_create:
        # 找不到已有 agent 即视为"无运行中任务"。这覆盖 esc 命中 agent 首次初始化窗口的情况：
        # 目标 agent 仍在 create_instance 的 ensure_initialized 中、尚未写入缓存，
        # get_agent_nowait 返回 None。直接回 success，不 fallback 新建（见 docstring 说明）。
        logger.info(
            "[AgentWebSocketServer] cancel: no existing agent, skip create: "
            "channel_id=%s session_id=%s",
            channel_id,
            request.session_id,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "event_type": "chat.interrupt_result",
                "success": True,
                "message": "当前会话任务已终止",
            },
        )

    # 3. 仍然没找到时 fallback 到 get_agent（异常场景）
    if agent is None and resp is None:
        logger.warning(
            "[AgentWebSocketServer] cancel: 未找到已有 agent，fallback 创建: channel_id=%s",
            channel_id,
        )
        mode, sub_mode = _apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=project_dir,
            sub_mode=sub_mode,
        )

    if agent is None and resp is None:
        raise ValueError("Failed to get agent for cancel request")

    if resp is None:
        resp = await agent.process_message(request)

    if send_response:
        wire = encode_agent_response_for_wire(
            resp,
            response_id=request.request_id,
        )
        await ctx.sink.send_wire(wire)
    return resp


async def handle_chat_cancel_dispatch(ctx: RequestContext) -> None:
    """处理 chat.interrupt：按 intent 决定是否取消流式任务。"""
    request = ctx.request
    # 中断请求：根据 intent 决定是否取消流式任务
    sid = request.session_id or "default"
    intent = request.params.get("intent", "cancel") if isinstance(request.params, dict) else "cancel"
    cleanup_after_cancel = _is_client_disconnect_cancel_request(request)

    # 只有 cancel/supplement 才取消流式任务
    # pause/resume 不取消，因为任务仍在运行（pause 在 checkpoint 阻塞，resume 解除阻塞）
    stream_tasks: list[asyncio.Task] = []
    if intent in ("cancel", "supplement"):
        entries = ctx.services.session_stream_tasks.get(sid, {})
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
        # 专门处理 cancel，复用已有 agent（不再 fallthrough 到 _handle_unary）
        # allow_create=False：找不到已有 agent 时不 fallback 新建（见 _handle_cancel docstring）。
        cancel_response = await _handle_cancel(
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
            cleanup_succeeded = (
                await _cleanup_client_disconnect_session_runtime(ctx, request)
            )
            if cancel_response is not None:
                if not cleanup_succeeded:
                    cancel_response.ok = False
                    cancel_response.payload = {
                        "event_type": "chat.interrupt_result",
                        "success": False,
                        "error": "session runtime cleanup failed",
                    }
                wire = encode_agent_response_for_wire(
                    cancel_response,
                    response_id=request.request_id,
                )
                await ctx.sink.send_wire(wire)


#: 按 session 串行化自动建队。弱引用持有，一次性会话不累积进程级状态。


# chat.send的自动team绑定
async def _ensure_auto_team_binding_for_chat(ctx, request: AgentRequest) -> Any | None:
    """Create and bind a team before the first team chat without consuming its query."""
    if request.req_method != ReqMethod.CHAT_SEND:
        return None

    params = request.params if isinstance(request.params, dict) else {}
    if not isinstance(request.params, dict):
        request.params = params
    session_id = str(request.session_id or params.get("session_id") or "").strip()
    if not session_id:
        return None

    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        update_session_metadata,
    )
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    sessions_root = _sessions_dir_for_request(request)
    metadata = get_session_metadata(
        session_id,
        cache_bust=True,
        sessions_root=sessions_root,
    )
    raw_mode = params.get("mode")
    effective_mode = (
        raw_mode
        if isinstance(raw_mode, str) and raw_mode.strip()
        else metadata.get("mode")
    )
    _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
    if not _is_team_metadata_mode({"mode": canonical_mode}):
        return None

    existing_team_name = str(metadata.get("team_name") or "").strip()
    if existing_team_name:
        params.setdefault("team_name", existing_team_name)
        template_id = str(metadata.get("team_template_id") or "").strip()
        if template_id:
            params.setdefault("team_template_id", template_id)
        return existing_team_name

    query = _request_query_text(request)
    if not query:
        return None

    async with _session_team_binding_lock(session_id):
        metadata = get_session_metadata(
            session_id,
            cache_bust=True,
            sessions_root=sessions_root,
        )
        existing_team_name = str(metadata.get("team_name") or "").strip()
        if existing_team_name:
            params.setdefault("team_name", existing_team_name)
            template_id = str(metadata.get("team_template_id") or "").strip()
            if template_id:
                params.setdefault("team_template_id", template_id)
            return existing_team_name

        from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store
        from jiuwenswarm.server.runtime.team_entity_store import get_team_entity_store

        binding, _template = await _create_generated_team_binding(
            description=query,
            config_base=_effective_config_for_request(request),
        )
        binding_store = get_team_binding_store()
        entity_store = get_team_entity_store()
        try:
            binding = binding_store.bind_session(
                team_name=binding.team_name,
                session_id=session_id,
            )
            update_session_metadata(
                session_id=session_id,
                channel_id=request.channel_id or None,
                user_content=query,
                mode=canonical_mode,
                team_name=binding.team_name,
                runtime_team_name=TeamManager.build_session_scoped_team_name(
                    binding.team_name,
                    session_id,
                ),
                team_template_id=binding.template_id,
                touch_last_message_at=False,
                sync_write=True,
                sessions_root=sessions_root,
            )
        except Exception:
            cleanup_errors: list[str] = []
            cleanup_steps = (
                lambda: binding_store.unbind_session(
                    team_name=binding.team_name,
                    session_id=session_id,
                ),
                lambda: binding_store.delete(binding.team_name),
                lambda: entity_store.delete_team_directory(binding.team_name),
            )
            for cleanup_step in cleanup_steps:
                try:
                    cleanup_step()
                except Exception as cleanup_exc:  # noqa: BLE001
                    cleanup_errors.append(str(cleanup_exc))
            if cleanup_errors:
                logger.warning(
                    "[AgentWebSocketServer] auto team binding rollback incomplete: "
                    "session_id=%s team_name=%s errors=%s",
                    session_id,
                    binding.team_name,
                    cleanup_errors,
                )
            raise

        params["team_name"] = binding.team_name
        params["team_template_id"] = binding.template_id
        request.metadata = dict(request.metadata or {})
        request.metadata["team_name"] = binding.team_name
        request.metadata["team_template_id"] = binding.template_id
        logger.info(
            "[AgentWebSocketServer] auto-created and bound team before chat: "
            "session_id=%s team_name=%s template_id=%s",
            session_id,
            binding.team_name,
            binding.template_id,
        )
        return binding



