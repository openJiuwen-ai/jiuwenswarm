# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""默认路径，分发表未命中时走的通用agent调用。

分发表内handler是按方法特化的：一个 ``ReqMethod`` 对应一个
``async def handle_x(ctx)``，函数体只处理那一个方法。
本模块相反，是方法无关的：签名为 ``(ctx, request)``，内部不按方法名分支，
"""

from __future__ import annotations

import asyncio

import logging
from typing import Any

from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_MODE_EXITED_EVENT_TYPE,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    is_interrupt_resume_payload,
)
from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.ws_diagnostics import describe_ws_exception, format_ws_diagnostics
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _uses_tenant_pool,
    _CODE_MODE_SYNC_METHODS,
    _apply_resolved_mode_to_request,
    _inject_plan_mode_activation_reminder,
    _plan_exited_sessions,
    _session_mode_sync_locks,
    _sessions_dir_for_request,
    _sync_chat_request_metadata,
    resolve_request_project_dir,
)
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool


logger = logging.getLogger(__name__)

# ── 流式心跳间隔：Agent 处理超过此阈值时发心跳 chunk 保活 WebSocket，
# 避免 ping_timeout 关连接。默认 10 秒，小于服务端 ping_timeout=20s。
_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0


def _is_stateless_method_request(request: AgentRequest) -> bool:
    """skills / skilldev / plugins / symphony 为无状态 RPC，无需 mode 解析与 adapter.

        恢复 5084467df 引入、8f54b26a7 合入 team 时误删的短路判定。
        """
    return (
        request.req_method is not None
        and request.req_method.value.startswith(
            ("skills.", "skilldev.", "plugins.", "symphony.",
             "agent_templates.", "plugin_packages.")
        )
    )


def _is_readonly_goal_get_request(request: AgentRequest) -> bool:
    """``command.goal`` + ``action=get``：只读查询，不得兜底新建 session metadata.

        与 skills.list 同类问题：走 ``_prepare_code_mode_chat_turn`` 会触发
        ``sync_session_request_metadata`` 在无 metadata 时写出
        ``metadata.json``。get 仍需要真实 agent（可能从 checkpointer 读已有
        Goal），故不能整段塞进 ``_is_stateless_method_request``。
        """
    if request.req_method != ReqMethod.COMMAND_GOAL:
        return False
    params = request.params if isinstance(request.params, dict) else {}
    action = str(params.get("action") or "get").strip().lower()
    return action == "get"


def _is_explicit_plan_entry_request(request: AgentRequest) -> bool:
    if not isinstance(request.params, dict):
        return False
    source = str(request.params.get("plan_entry_source") or "").strip().lower()
    return source in {"slash_command", "e2a"}


def _should_sync_code_mode_state(request: AgentRequest) -> bool:
    """Only agent chat turns may change plan/normal mode.

        Background RPCs (e.g. ``skills.list``) also send ``mode: code.normal`` but
        must not run plan-mode restore logic or race with an in-flight approval.
        """
    method = request.req_method
    if method is None:
        return True
    return method in _CODE_MODE_SYNC_METHODS


def _session_mode_sync_lock(session_id: str) -> asyncio.Lock:
    lock = _session_mode_sync_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_mode_sync_locks[session_id] = lock
    return lock


# ── 需要服务端协作者（经 ctx.services 白名单访问）──────────────────────────

async def _get_stateless_agent(ctx, channel_id: str) -> Any:
    """为无状态请求取 agent，**不触发任何 mode 的 adapter 重建**.

        优先用 AgentManager 已缓存的 agent 模式 agent（get_agent_nowait 命中即返回，
        不命中返回 None，绝不创建）；都没缓存时复用（或首次构造）本 server 上按
        channel 缓存的轻量 JiuWenSwarm()（**不调 create_instance**，_adapter 保持
        None）——其 process_message 内部对 skills/skilldev/plugins/symphony 的无状态
        短路会在 _ensure_adapter 之前 return，碰不到 adapter。真正的 adapter 重建
        留给 chat.send。

        相比 5084467df 原版用 get_agent(mode="agent") 作 fallback（会触发 agent 模式
        adapter 重建，治标不治本），此处彻底解耦。Fallback 必须按 channel 复用，
        否则每次 cache miss 新建 SkillManager，SkillNet install/install_status 会
        落到不同实例并误报「安装会话已过期」。
        """
    cached = ctx.services.agent_manager.get_agent_nowait(
        channel_id=channel_id, mode="agent"
    )
    if cached is not None:
        return cached
    agent = ctx.services.stateless_fallback_agents.get(channel_id)
    if agent is not None:
        return agent
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    agent = JiuWenSwarm()  # 不调 create_instance，_adapter 保持 None
    ctx.services.stateless_fallback_agents[channel_id] = agent
    return agent


async def _push_plan_mode_exited(ctx, request: AgentRequest) -> None:
    """Notify the client that plan mode ended after user approval."""
    session_id = request.session_id
    if not session_id:
        return
    await ctx.services.send_push({
        "channel_id": request.channel_id or "default",
        "session_id": session_id,
        "payload": {
            "event_type": PLAN_MODE_EXITED_EVENT_TYPE,
            "mode": "code.normal",
        },
    })


# ── code-mode 状态准备 ────────────────────────────────────────────────────

async def _check_post_process_plan_exit(
    ctx,
    request: AgentRequest,
    agent: Any,
) -> None:
    """Detect plan→normal transition that happened inside tool execution.

        When ``exit_plan_mode`` is approved, ``ExitPlanModeTool.invoke()``
        calls ``restore_mode_after_plan_exit()`` to persist the mode change
        to the session checkpointer.  This runs AFTER ``_ensure_code_mode_state``
        has already completed (which only syncs the mode BEFORE processing).

        We check the persisted state here and push a ``plan.mode_exited``
        event so the TUI status bar updates immediately, rather than waiting
        for the next user request.

        Only checks requests whose sub_mode is ``"plan"`` — the transition
        from plan→normal can only happen during a plan-mode request (the LLM
        calls ``exit_plan_mode``).  Checking ``sub_mode == "normal"`` requests
        would produce false positives for every background RPC (e.g.
        ``skills.list``) that uses ``code.normal`` but never had an active
        plan session.
        """
    session_id = request.session_id
    if not session_id:
        return
    mode, sub_mode = _apply_resolved_mode_to_request(request)
    if mode != "code" or sub_mode != "plan":
        return
    from openjiuwen.core.single_agent import create_agent_session
    deep_agent = await agent.ensure_instance()
    session = create_agent_session(
        session_id=session_id,
        card=deep_agent.card,
    )
    await session.pre_run(inputs=None)
    state = deep_agent.load_state(session)
    if state.plan_mode.mode == "normal":
        _plan_exited_sessions.add(session_id)
        await _push_plan_mode_exited(ctx, request)
        logger.info(
            "[_check_post_process_plan_exit] Detected plan→normal after "
            "tool execution for session=%s",
            session_id,
        )


async def _ensure_code_mode_state(
    ctx,
    request: AgentRequest,
    mode: str,
    sub_mode: str,
    agent: Any,
) -> bool:
    """code 模式：确保 agent 的 plan_mode 状态正确，必要时执行 switch_mode 并持久化.

        当 plan 刚完成时跳过陈旧的 normal→plan switch_mode，
        避免 exit_plan_mode 已恢复的模式被覆盖；显式用户 /plan 进入除外.
        switch_mode 内部已通过 save_state 写入正确的 "deepagent" key，
        此处只需 post_run 持久化到 checkpointer.

        切换到 plan 模式且尚未调用 enter_plan_mode 时，注入 <system-reminder>
        告知 LLM 调用 enter_plan_mode。

        ``exit_plan_mode`` now restores mode immediately inside the tool
        (via ``restore_mode_after_plan_exit``), so this method no longer needs
        to gate plan→normal transitions with an approval flag.

        Returns:
            ``True`` if plan mode was restored to normal (mode sync occurred).
        """
    if mode != "code" or sub_mode == "team":
        return False
    if not _should_sync_code_mode_state(request):
        return False
    if is_interrupt_resume_payload(request.params):
        logger.info(
            "[_ensure_code_mode_state] Skip mode sync while resuming tool interrupt "
            "for session=%s source=%s",
            request.session_id,
            (request.params or {}).get("source") if isinstance(request.params, dict) else None,
        )
        return False
    session_id = request.session_id or "default"
    restored_after_approval = False
    async with _session_mode_sync_lock(session_id):
        from openjiuwen.core.single_agent import create_agent_session
        deep_agent = await agent.ensure_instance()
        session = create_agent_session(
            session_id=request.session_id, card=deep_agent.card
        )
        await session.pre_run(inputs=None)  # 从 checkpointer 加载历史 state
        state = deep_agent.load_state(session)
        # 仅在目标模式与当前模式不同时执行模式切换
        mode_changed_to_plan = False
        if state.plan_mode.mode != sub_mode:
            # Guard: block stale normal→plan switches when plan was already exited.
            # Explicit user /plan requests bypass this guard and start a fresh plan.
            # Two mechanisms:
            #   1. _plan_exited_sessions flag (precise — set by _check_post_process_plan_exit)
            #   2. plan_slug fallback (defense-in-depth — plan exists but mode is normal)
            if state.plan_mode.mode == "normal" and sub_mode == "plan":
                blocked = False
                explicit_plan_entry = _is_explicit_plan_entry_request(request)
                if explicit_plan_entry:
                    _plan_exited_sessions.discard(session_id)
                elif session_id in _plan_exited_sessions:
                    _plan_exited_sessions.discard(session_id)
                    blocked = True
                    logger.info(
                        "[_ensure_code_mode_state] Blocked stale plan re-entry via "
                        "flag for session=%s",
                        session_id,
                    )
                elif state.plan_mode.plan_slug is not None:
                    # Fallback: plan was completed, checkpoint is authoritative.
                    # Clear slug so this guard is one-shot.
                    state.plan_mode.plan_slug = None
                    deep_agent.save_state(session, state)
                    await session.post_run()
                    blocked = True
                    logger.info(
                        "[_ensure_code_mode_state] Blocked stale plan re-entry via "
                        "plan_slug for session=%s",
                        session_id,
                    )
                if blocked:
                    if isinstance(request.params, dict):
                        request.params["mode"] = "code.normal"
                    await _push_plan_mode_exited(ctx, request)
                    return False
            deep_agent.switch_mode(session=session, mode=sub_mode)
            if state.plan_mode.mode == "plan" and sub_mode == "normal":
                restored_after_approval = True
                logger.info(
                    "[_ensure_code_mode_state] Synced plan→normal for session=%s",
                    session_id,
                )
            if sub_mode == "plan":
                mode_changed_to_plan = True
                # Clear stale plan_slug from previous plan session so
                # enter_plan_mode creates a fresh plan file.
                state = deep_agent.load_state(session)
                if state.plan_mode.plan_slug:
                    state.plan_mode.plan_slug = None
                    deep_agent.save_state(session, state)
            # switch_mode 内部已通过 save_state 写入 "deepagent" key，
            # 只需 post_run 持久化到 checkpointer
            await session.post_run()
        # 切换到 plan 模式时注入 <system-reminder> 告知 LLM 调用 enter_plan_mode。
        # 使用 mode_changed_to_plan 而非 plan_slug 判断，因为 restore_mode_after_plan_exit
        # 不清除 plan_slug，导致后续 /plan 时提醒被错误跳过。
        if sub_mode == "plan" and mode_changed_to_plan:
            _inject_plan_mode_activation_reminder(request)
    return restored_after_approval


async def _prepare_code_mode_chat_turn(
    ctx,
    request: AgentRequest,
    channel_id: str,
    *,
    sync_metadata: bool = True,
    agent_manager: Any | None = None,
) -> tuple[str, str | None, Any]:
    """Mode resolution and correct agent instance selection."""
    # [新增] 在 _apply_resolved_mode_to_request 把 canonical mode 写回 params 之前，
    # 先记录请求是否「显式」携带了 mode。下游 sync 用它做守卫：未显式携带则不覆盖
    # 磁盘已锁定的会话 mode（避免只读 RPC 用默认推断值腐蚀 team 等已锁定 mode）。
    # model 的显式与否由 _sync_chat_request_request_metadata 内部从 params 判断
    # （model_name 不会被规范化改写），故此处只捕获 mode 标志。
    # 注意：用与下游一致的严格判断——纯空白串 "   " 不算显式携带（bool("   ") 为 True
    # 会误判，导致空白 mode 走默认推断 agent.plan 并写盘腐蚀已锁定 mode）。
    params = request.params if isinstance(request.params, dict) else {}
    _raw_mode = params.get("mode")
    explicit_mode_provided = isinstance(_raw_mode, str) and bool(_raw_mode.strip())
    source = str(params.get("source") or "").strip()
    is_interrupt_continuation = (
        source
        in {"permission_interrupt", "confirm_interrupt", "ask_user_interrupt"}
        and isinstance(params.get("answers"), list)
        and bool(str(params.get("request_id") or "").strip())
    )
    runtime_work_mode = None
    sid = str(request.session_id or "").strip()
    sessions_root = _sessions_dir_for_request(request)
    session_metadata: dict[str, Any] = {}
    if sid:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        loaded_metadata = get_session_metadata(
            sid,
            cache_bust=True,
            enable_writeback=False,
            sessions_root=sessions_root,
        )
        if isinstance(loaded_metadata, dict):
            session_metadata = loaded_metadata

        # Control continuations normally contain only answers + the interrupted
        # tool_call id.  Restore the original canonical mode before applying
        # defaults; otherwise the request is irreversibly rewritten to
        # ``agent`` and the tenant pool creates a different adapter instance.
        if is_interrupt_continuation and not explicit_mode_provided:
            stored_mode = str(session_metadata.get("mode") or "").strip()
            if stored_mode and stored_mode.lower() != "unknown":
                params["mode"] = stored_mode
                _raw_mode = stored_mode
                logger.info(
                    "[AgentWebSocketServer] interrupt continuation routing "
                    "restored: session_id=%s mode=%s source=%s",
                    sid,
                    stored_mode,
                    source,
                )
        stored_work_mode = (
            session_metadata.get("work_mode")
            if isinstance(session_metadata, dict)
            else None
        )
        if isinstance(stored_work_mode, str) and stored_work_mode.strip().lower() in {
            "code",
            "work",
        }:
            runtime_work_mode = stored_work_mode.strip().lower()
    if runtime_work_mode is None:
        request_work_mode = params.get("work_mode")
        if isinstance(request_work_mode, str) and request_work_mode.strip().lower() in {
            "code",
            "work",
        }:
            runtime_work_mode = request_work_mode.strip().lower()
    if runtime_work_mode is not None:
        params["work_mode"] = runtime_work_mode
    # An explicit/hydrated concrete mode is the stronger routing identity.
    # A stale session work_mode (OfficeClaw historically initialized it as
    # ``work``) must not turn an explicit ``code.normal`` request into agent.
    mode_work_mode = runtime_work_mode
    concrete_mode = str(_raw_mode or "").strip().lower()
    if concrete_mode.startswith(("code.", "team")) or concrete_mode == "code":
        mode_work_mode = None
    mode, sub_mode = _apply_resolved_mode_to_request(
        request,
        work_mode=mode_work_mode,
    )
    agent_mode = "agent" if mode == "auto_harness" else mode
    requested_project_dir = resolve_request_project_dir(request)
    # [改动] 写盘用 canonical mode（request.params["mode"]，已被规范化为
    # "agent.plan"/"team" 等），而非一级 mode（"agent"），使磁盘出现你期望的两类值。
    canonical_mode = (
        request.params.get("mode") if isinstance(request.params, dict) else None
    )
    if sync_metadata:
        project_dir = _sync_chat_request_metadata(
            request,
            requested_project_dir,
            canonical_mode if canonical_mode else mode,
            explicit_mode_provided=explicit_mode_provided,
        )
    else:
        # Read-only path (e.g. command.goal get): never create/update
        # metadata.json. Prefer request project_dir, else locked disk value.
        project_dir = requested_project_dir
        if not (isinstance(project_dir, str) and project_dir.strip()):
            sid = str(request.session_id or "").strip()
            if sid:
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    get_session_metadata,
                )
                meta = get_session_metadata(
                    sid,
                    cache_bust=True,
                    enable_writeback=False,
                    sessions_root=sessions_root,
                )
                locked = meta.get("project_dir") if isinstance(meta, dict) else None
                if isinstance(locked, str) and locked.strip():
                    project_dir = locked.strip()
    if isinstance(project_dir, str) and project_dir.strip():
        project_dir = project_dir.strip()
        request.params["project_dir"] = project_dir
        request.metadata = dict(request.metadata or {})
        request.metadata["project_dir"] = project_dir
    manager = agent_manager or ctx.services.agent_manager
    await manager.wait_for_session_prewarm(request.session_id)
    agent = await manager.get_agent(
        channel_id=channel_id,
        mode=agent_mode,
        project_dir=project_dir,
        sub_mode=sub_mode,
    )
    if agent is None:
        raise ValueError("Failed to get agent")
    return mode, sub_mode, agent


async def _get_tenant_agent_manager(ctx, request: AgentRequest) -> Any:
    """Resolve the tenant-pool AgentManager for an officeclaw/E2A request."""
    pool = ctx.services.tenant_pool()
    agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
    agent_id, service_id = pool.resolve_control_rpc_tenant(
        request, agent_id, service_id
    )
    return await pool.get_agent_manager(agent_id, service_id, workspace_key)


async def _prepare_tenant_code_mode_chat_turn(
    ctx,
    request: AgentRequest,
    channel_id: str,
) -> tuple[str, str | None, Any] | None:
    """Run the same code.plan sync as the default WS path on tenant-pool chats.

    officeclaw previously skipped ``_ensure_code_mode_state``, so
    ``mode=code.plan`` never switched durable plan_mode.
    """
    if request.req_method not in _CODE_MODE_SYNC_METHODS:
        return None
    if _is_readonly_goal_get_request(request):
        return None
    manager = await _get_tenant_agent_manager(ctx, request)
    mode, sub_mode, agent = await _prepare_code_mode_chat_turn(
        ctx,
        request,
        channel_id,
        agent_manager=manager,
    )
    restored_plan = await _ensure_code_mode_state(
        ctx, request, mode, sub_mode, agent
    )
    if restored_plan:
        await _push_plan_mode_exited(ctx, request)
    return mode, sub_mode, agent


# ── 入口：表未命中时由 pipeline 调用 ──────────────────────────────────────

async def _handle_unary(
    ctx: RequestContext, request: AgentRequest
) -> None:
    from jiuwenswarm.telemetry.context_propagation import (
        bind_incoming_request,
        reset_incoming_request,
    )

    manager = getattr(ctx.services, "agent_manager", None)
    foreground = (
        request.req_method in _CODE_MODE_SYNC_METHODS
        and manager is not None
        and hasattr(manager, "begin_foreground_chat")
        and hasattr(manager, "end_foreground_chat")
    )
    binding = bind_incoming_request(request)
    try:
        if foreground:
            await manager.begin_foreground_chat()
        try:
            await _handle_unary_impl(ctx, request)
        finally:
            if foreground:
                await manager.end_foreground_chat()
    finally:
        reset_incoming_request(binding)


async def _handle_unary_impl(
    ctx: RequestContext, request: AgentRequest
) -> None:
    """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
    # 兜底确保 checkpointer 就绪: start() 里改为后台预热后, 首条请求可能赶在
    # 预热完成前到达。不要在事件循环上同步 import interface_deep（会和
    # to_thread 预热抢 import 锁、饿死 WS recv）。
    from jiuwenswarm.server.agent_ws_server import (
        ensure_interface_deep_and_checkpointer,
    )

    await ensure_interface_deep_and_checkpointer()
    channel_id = request.channel_id or "default"

    if _uses_tenant_pool(request):
        prepared = await _prepare_tenant_code_mode_chat_turn(ctx, request, channel_id)
        try:
            resp = await ctx.services.tenant_pool().process_message(request)
            if getattr(resp, "agent_ref", None) is None:
                resp.agent_ref = request.agent_ref
            await ctx.sink.send_unary(resp, response_id=request.request_id)
        finally:
            if prepared is not None:
                await _check_post_process_plan_exit(ctx, request, prepared[2])
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送 (tenant pool): request_id=%s",
            request.request_id,
        )
        return

    # Disk-only evolution RPCs must go through AgentManager so skill_path roots
    # are bound into session-registered dirs (enterprise parity). Do not use the
    # skills.* stateless short-circuit, which skips that binding.
    if request.req_method in (
        ReqMethod.SKILLS_EVOLUTION_ARCHIVES,
        ReqMethod.SKILLS_EVOLUTION_ROLLBACK,
    ):
        resp = await ctx.services.agent_manager.process_message(request)
        if getattr(resp, "agent_ref", None) is None:
            resp.agent_ref = request.agent_ref
        await ctx.sink.send_unary(resp, response_id=request.request_id)
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送 (disk-only evolution): request_id=%s",
            request.request_id,
        )
        return

    # 无状态请求（skills / skilldev / plugins / symphony）不需要 mode 解析和
    # code mode 状态管理，直接走 process_message 即可。用轻量 agent 获取，不触发
    # adapter 重建（恢复 8f54b26a7 误删的短路，并修正 5084467df 触发重建的缺陷）。
    if _is_stateless_method_request(request):
        agent = await _get_stateless_agent(ctx, channel_id)
        resp = await agent.process_message(request)
        if getattr(resp, "agent_ref", None) is None:
            resp.agent_ref = request.agent_ref
        await ctx.sink.send_unary(resp, response_id=request.request_id)
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
        )
        return

    readonly_goal_get = _is_readonly_goal_get_request(request)
    mode, sub_mode, agent = await _prepare_code_mode_chat_turn(ctx, 
        request,
        channel_id,
        sync_metadata=not readonly_goal_get,
    )

    if not readonly_goal_get:
        restored_plan = await _ensure_code_mode_state(ctx, 
            request, mode, sub_mode, agent
        )
        if restored_plan:
            await _push_plan_mode_exited(ctx, request)

    resp = None
    try:
        resp = await agent.process_message(request)
    finally:
        # Push plan.mode_exited if exit_plan_mode restored mode during processing
        if not readonly_goal_get:
            await _check_post_process_plan_exit(ctx, request, agent)

    # V2: 非流式响应回带请求侧 agent_ref，供 gateway 3 元组路由（设计 §6.3）。
    # is None 守卫：保留 agent 层显式设置的 agent_ref（如 team 模式由事件派生）。
    if getattr(resp, "agent_ref", None) is None:
        resp.agent_ref = request.agent_ref

    await ctx.sink.send_unary(resp, response_id=request.request_id)
    logger.info(
        "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
        request.request_id,
    )


async def _handle_stream(
    ctx: RequestContext, request: AgentRequest
) -> None:
    from jiuwenswarm.telemetry.context_propagation import (
        bind_incoming_request,
        reset_incoming_request,
    )

    manager = getattr(ctx.services, "agent_manager", None)
    foreground = (
        request.req_method in _CODE_MODE_SYNC_METHODS
        and manager is not None
        and hasattr(manager, "begin_foreground_chat")
        and hasattr(manager, "end_foreground_chat")
    )
    binding = bind_incoming_request(request)
    try:
        if foreground:
            await manager.begin_foreground_chat()
        try:
            await _handle_stream_impl(ctx, request)
        finally:
            if foreground:
                await manager.end_foreground_chat()
    finally:
        reset_incoming_request(binding)


async def _handle_stream_impl(
    ctx: RequestContext, request: AgentRequest
) -> None:
    """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
    # 兜底确保 checkpointer 就绪 (见 _handle_unary 同名注释)。
    from jiuwenswarm.server.agent_ws_server import (
        ensure_interface_deep_and_checkpointer,
    )

    await ensure_interface_deep_and_checkpointer()
    channel_id = request.channel_id or "default"
    session_id = request.session_id or "default"
    current_task = asyncio.current_task()
    stream_stop_event = asyncio.Event()
    if current_task is not None:
        ctx.services.session_stream_tasks.setdefault(session_id, {})[current_task] = stream_stop_event

    if _uses_tenant_pool(request):
        prepared = await _prepare_tenant_code_mode_chat_turn(ctx, request, channel_id)
        chunk_count = 0
        try:
            async for chunk in ctx.services.tenant_pool().process_message_stream(request):
                chunk_count += 1
                if chunk.agent_ref is None:
                    chunk.agent_ref = request.agent_ref
                sent_original = await ctx.sink.send_chunk(
                    chunk,
                    sequence=chunk_count - 1,
                    response_id=request.request_id,
                )
                if not sent_original:
                    return
        finally:
            if prepared is not None:
                await _check_post_process_plan_exit(ctx, request, prepared[2])
        logger.info(
            "[AgentWebSocketServer] 流式响应完成 (tenant pool): request_id=%s chunks=%s",
            request.request_id,
            chunk_count,
        )
        return

    # 无状态流式请求（skills / skilldev / plugins / symphony）不需要 mode 解析和
    # code mode 状态管理，直接走 process_message_stream 即可。用轻量 agent 获取，
    # 不触发 adapter 重建（恢复 8f54b26a7 误删的短路，并修正 5084467df 触发重建的缺陷）。
    readonly_goal_get = _is_readonly_goal_get_request(request)
    if _is_stateless_method_request(request):
        agent = await _get_stateless_agent(ctx, channel_id)
    else:
        mode, sub_mode, agent = await _prepare_code_mode_chat_turn(ctx, 
            request,
            channel_id,
            sync_metadata=not readonly_goal_get,
        )

        if not readonly_goal_get:
            restored_plan = await _ensure_code_mode_state(ctx, 
                request, mode, sub_mode, agent
            )
            if restored_plan:
                await _push_plan_mode_exited(ctx, request)

    chunk_count = 0
    # 心跳控制：当有真实 chunk 发送时重置，空闲时发送心跳
    heartbeat_event = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None

    async def _heartbeat_loop() -> None:
        """后台心跳任务：在空闲期间定期发送 keepalive chunk."""
        try:
            while True:
                # 等待心跳间隔，如果期间有真实 chunk 发送则 heartbeat_event 被设置，重置等待
                try:
                    await asyncio.wait_for(
                        heartbeat_event.wait(),
                        timeout=_STREAM_HEARTBEAT_INTERVAL_SECONDS,
                    )
                    # 有真实 chunk 发送，重置 event 继续等待
                    heartbeat_event.clear()
                except asyncio.TimeoutError:
                    # 超时：空闲超过心跳间隔，发送 keepalive chunk
                    heartbeat_chunk = AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=channel_id,
                        payload={"event_type": "keepalive"},
                        is_complete=False,
                    )
                    # V2: 心跳 chunk 也回带 agent_ref，避免切换 mode 后
                    # 旧 session 心跳错路由到新 agent 窗口（设计 §5.2 场景 2）。
                    if heartbeat_chunk.agent_ref is None:
                        heartbeat_chunk.agent_ref = request.agent_ref
                    # 心跳使用特殊序列号 -1。与主循环共用同一个 ctx.sink，
                    # 因此共用同一把发送锁 —— 两条写路径必须互斥，否则帧会交错。
                    await ctx.sink.send_chunk(
                        heartbeat_chunk,
                        sequence=-1,
                        response_id=request.request_id,
                    )
                    logger.info(
                        "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                        request.request_id,
                    )
        except asyncio.CancelledError:
            pass
        except WebSocketConnectionClosed as ws_closed_exc:
            logger.info(
                "[AgentWebSocketServer] keepalive 停止，WebSocket 已关闭: %s",
                format_ws_diagnostics(
                    {"request_id": request.request_id},
                    describe_ws_exception(ws_closed_exc),
                ),
            )

    # 启动心跳任务
    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    try:
        async for chunk in agent.process_message_stream(request):
            chunk_count += 1
            # 通知心跳任务有真实 chunk 发送，重置心跳计时
            heartbeat_event.set()
            # V2: chunk 回带请求侧 agent_ref，供 gateway 3 元组精确路由
            # （设计 §6.3）。is None 守卫：保留 team 模式由事件派生的 agent_ref
            # （_build_team_event_chunk_meta 已设值），不覆盖。
            if chunk.agent_ref is None:
                chunk.agent_ref = request.agent_ref
            # 诊断：打印前 3 个和每 50 个 chunk 的发送情况。
            # wire 只在这里算 —— 编码本身由 ctx.sink 负责，不必为一条偶尔打印
            # 的日志给每个 chunk 都编一次。
            if chunk_count <= 3 or chunk_count % 50 == 0:
                _pl = getattr(chunk, "payload", None) or {}
                _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=chunk_count - 1,
                )
                logger.info(
                    "[AgentWebSocketServer] chunk sent: request_id=%s seq=%s"
                    " event_type=%s wire_keys=%s",
                    request.request_id, chunk_count - 1, _et,
                    list(wire.keys())[:10] if isinstance(wire, dict) else "non-dict",
                )
            try:
                sent_original = await ctx.sink.send_chunk(
                    chunk,
                    sequence=chunk_count - 1,
                    response_id=request.request_id,
                )
                if not sent_original:
                    logger.warning(
                        "[AgentWebSocketServer] 流式响应因单个 chunk 超限而停止: "
                        "request_id=%s seq=%s",
                        request.request_id,
                        chunk_count - 1,
                    )
                    return
            except WebSocketConnectionClosed as ws_closed_exc:
                logger.info(
                    "[AgentWebSocketServer] 流式响应停止，WebSocket 已关闭: %s",
                    format_ws_diagnostics(
                        {"request_id": request.request_id},
                        describe_ws_exception(ws_closed_exc),
                    ),
                )
                return
            # 清除 event，让心跳任务重新开始计时
            heartbeat_event.clear()
    finally:
        # 停止心跳任务
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except WebSocketConnectionClosed:
                pass
        # 清除自身的宿主生命周期记录；同 session 的其它请求不受影响。
        entries = ctx.services.session_stream_tasks.get(session_id)
        if entries is not None and current_task is not None:
            entries.pop(current_task, None)
            if not entries:
                ctx.services.session_stream_tasks.pop(session_id, None)
        # Push plan.mode_exited if exit_plan_mode restored mode during processing
        if not readonly_goal_get:
            await _check_post_process_plan_exit(ctx, request, agent)

    logger.info(
        "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
        request.request_id,
        chunk_count,
    )
