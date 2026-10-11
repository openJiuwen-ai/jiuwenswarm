# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""会话、历史记录和回退。

会话就是一段对话。历史记录是这段对话里已经发生的消息。
回退是把对话撤回到某一步，可选地把改过的文件也撤回去。

创建会话和从旧会话分出新会话在 bootstrap 里。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import math
import sys
from typing import Any

from jiuwenswarm.common.mode_matrix import is_plan_mode, is_team_mode
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session_provisioner import (
    SessionProvisionCommitContext,
    SessionProvisionCommitTiming,
    SessionProvisionState,
    SessionSwitchInput,
)
from jiuwenswarm.server.context import AgentServerServices, RequestContext
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history import (
    HistorySnapshotChanged,
    InvalidHistoryCursor,
    append_history_record,
    enqueue_history_request_completion,
    history_exists,
    is_valid_session_id,
    load_history_records,
    read_history_cursor_page,
    wait_for_history_receipt,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessagingError,
)
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
    get_session_metadata,
)
from jiuwenswarm.server.wire_truncate import (
    _HISTORY_PAGE_SIZE,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _sanitize_history_record_for_wire,
    split_history_record_for_stream,
)

logger = logging.getLogger(__name__)


def _active_plan_sessions():
    """本进程里进过计划模式的会话。

    计划模式就是先想清楚再改代码。这份名单仍放在服务器模块里，
    换一轮服务器时会换成新的那一份，所以每次用都重新去拿。
    """
    import jiuwenswarm.server.agent_ws_server as ws_server

    return ws_server._plan_active_sessions


def _session_switch_locks():
    """同一条连接上的切换要排队。锁表仍放在服务器模块里，测试也盯着那里。"""
    import jiuwenswarm.server.agent_ws_server as ws_server

    return ws_server._session_switch_locks


async def _send_error(ctx: RequestContext, error: str, code: str | None = None) -> None:
    """回一条失败。失败原因放在 error 里，机器可读的编号放在 code 里。"""
    request = ctx.request
    payload: dict[str, Any] = {"error": error}
    if code:
        payload["code"] = code
    await ctx.sink.send_response(
        AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload=payload,
            metadata=request.metadata,
        )
    )


def load_todo_snapshot_for_frontend(*args, **kwargs):
    """读取待办快照。待办就是这份会话还没做完的清单。

    真正的读取函数会带上一大串依赖，所以第一次用到才导入。
    """
    from jiuwenswarm.common.todo_snapshot import (
        load_todo_snapshot_for_frontend as _load,
    )

    return _load(*args, **kwargs)


async def _send_chunk(ctx: RequestContext, chunk: AgentResponseChunk, sequence: int) -> bool:
    """送出历史的一片。sequence 是这一片的序号，从 0 开始。"""
    return await ctx.sink.send_chunk(chunk, sequence=sequence)


async def handle_history_get(ctx: RequestContext) -> None:
    """读取历史。流式请求一片一片回，普通请求整页回。"""
    if ctx.request.is_stream:
        await handle_history_get_stream(ctx)
    else:
        await handle_history_get_page(ctx)


def _is_restorable_history_record(record: Any) -> bool:
    """Coarsely filter records that the web history UI cannot use for pagination."""
    if not isinstance(record, dict):
        return False

    role = record.get("role")
    content = record.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    has_media = (
        isinstance(record.get("media_items"), list) and bool(record["media_items"])
    ) or (
        isinstance(record.get("mediaItems"), list) and bool(record["mediaItems"])
    )
    files = record.get("files")
    if isinstance(files, dict):
        has_media = has_media or (
            isinstance(files.get("uploaded_images"), list)
            and bool(files["uploaded_images"])
        )

    if role == "user":
        mode = record.get("mode", "")
        if is_team_mode(mode):
            channel_id = record.get("channel_id", "")
            if channel_id not in ("web", "tui"):
                return False
        return has_content or has_media

    event_type = record.get("event_type")
    if not event_type:
        return has_content
    return event_type in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


def _todo_snapshot_session_fields(session_id: str) -> dict[str, str | None]:
    """Read locked session fields that decide where ``todo.json`` lives."""
    try:
        metadata = get_session_metadata(session_id, enable_writeback=False) or {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    project_dir = str(metadata.get("project_dir") or "").strip() or None
    work_mode = metadata.get("work_mode")
    mode = metadata.get("mode")
    return {
        "project_dir": project_dir,
        "work_mode": work_mode if isinstance(work_mode, str) else None,
        "mode": mode if isinstance(mode, str) else None,
    }


def _combine_session_in_plan(
    *,
    live_plan_mode: str | None,
    session_id: str,
    metadata_mode: Any,
) -> bool:
    """Combine live agent plan_mode, in-process marker, and metadata.mode.

    Team sessions ignore live DeepAgent ``plan_mode``: cluster plan is
    persisted on ``metadata.mode`` (``team.*.plan``), while a live
    DeepAdapter on the same session_id typically still has the default
    ``normal`` plan_mode and would falsely report not-in-plan.
    """
    if is_team_mode(metadata_mode):
        if session_id in _active_plan_sessions():
            return True
        return is_plan_mode(metadata_mode)
    if isinstance(live_plan_mode, str) and live_plan_mode.strip():
        return live_plan_mode.strip() == "plan"
    if session_id in _active_plan_sessions():
        return True
    return is_plan_mode(metadata_mode)


def _try_read_live_plan_mode(agent_manager: Any, session_id: str) -> str | None:
    """Read ``plan_mode.mode`` from a live DeepAgent, if one is already running.

    Does not start a session or build an adapter. Missing live state is
    not an error: the caller falls back to metadata.
    """
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        resolve_live_agent_session,
    )

    agents_by_channel = getattr(agent_manager, "agents", None) or {}
    if not isinstance(agents_by_channel, dict):
        return None
    for channel_agents in agents_by_channel.values():
        if not isinstance(channel_agents, dict):
            continue
        for agent in channel_agents.values():
            getter = getattr(agent, "get_live_session_instance", None)
            if not callable(getter):
                continue
            try:
                deep_agent = getter(session_id)
                if deep_agent is None:
                    continue
                session = resolve_live_agent_session(deep_agent, session_id)
                if session is None:
                    continue
                load_state = getattr(deep_agent, "load_state", None)
                if not callable(load_state):
                    continue
                state = load_state(session)
                mode = getattr(getattr(state, "plan_mode", None), "mode", None)
            except Exception as exc:
                logger.warning(
                    "[session.plan_status] skip live agent while reading "
                    "plan_mode: session=%s error=%s",
                    session_id,
                    exc,
                )
                continue
            if isinstance(mode, str) and mode.strip():
                return mode.strip()
    return None

async def handle_session_plan_status(ctx: RequestContext) -> None:
    """``session.plan_status``：只读查询当前会话是否处于计划模式。

    不调用 ``switch_mode`` / ``ensure_live_session_instance``，也不改
    ``_plan_active_sessions``。单 agent 有 live session 时以 ``plan_mode``
    为准（能纠正 metadata 仍是 ``*.plan``、agent 已退出的情况）；否则回退
    metadata.mode。集群的 plan 写在 metadata / team runtime，不走
    DeepAgent ``plan_mode``——同 session 上常有为 Goal 等 RPC 拉起的
    DeepAdapter，默认 ``plan_mode=normal``，若当成权威会把
    ``team.work.plan`` 误判成未在计划里。
    """
    request = ctx.request
    services = ctx.services
    params = request.params if isinstance(request.params, dict) else {}
    sid = str(params.get("session_id") or request.session_id or "").strip()
    if not sid:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session_id is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    else:
        meta = get_session_metadata(
            sid,
            cache_bust=True,
            enable_writeback=False,
        )
        if not meta:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session not found", "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        else:
            metadata_mode = meta.get("mode")
            live_plan_mode = (
                None
                if is_team_mode(metadata_mode)
                else _try_read_live_plan_mode(services.agent_manager, sid)
            )
            in_plan = _combine_session_in_plan(
                live_plan_mode=live_plan_mode,
                session_id=sid,
                metadata_mode=metadata_mode,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"session_id": sid, "in_plan": in_plan},
                metadata=request.metadata,
            )
    if getattr(resp, "agent_ref", None) is None:
        resp.agent_ref = request.agent_ref
    await ctx.sink.send_response(resp)

async def handle_session_list(ctx: RequestContext) -> None:
    """处理 session.list 请求：返回历史会话基础信息列表。

    响应格式与 Web fallback ``_session_list`` 保持一致:
    ``{"sessions": [...], "total": int, "limit": int, "offset": int}``,
    确保按新接口接入分页的 Web 前端能拿到分页元信息。
    """
    request = ctx.request
    # 解析 limit/offset(与 Web fallback 一致的宽松解析)
    params = request.params if isinstance(request.params, dict) else {}
    limit = 20
    offset = 0
    raw_limit = params.get("limit")
    if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
        limit = raw_limit
    elif isinstance(raw_limit, float) and raw_limit.is_integer():
        limit = int(raw_limit)
    elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
        limit = int(raw_limit.strip())

    raw_offset = params.get("offset")
    if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
        offset = raw_offset
    elif isinstance(raw_offset, float) and raw_offset.is_integer():
        offset = int(raw_offset)
    elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
        offset = int(raw_offset.strip())

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    try:
        sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)
    except Exception as exc:
        logger.warning("[AgentWebSocketServer] 获取会话列表失败: %s", exc)
        sessions, total = [], 0

    resp = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload={
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
        metadata=request.metadata,
    )
    await ctx.sink.send_response(resp)

async def handle_session_rename(ctx: RequestContext) -> None:
    """处理 session.rename：与 CLI Gateway 本地回退共用 apply_session_rename。"""
    request = ctx.request
    from jiuwenswarm.server.runtime.session.session_rename import apply_session_rename

    sid = request.session_id or ""
    ch = (request.channel_id or "").strip() or "tui"
    ok, payload, err, code = apply_session_rename(
        request.params,
        sid,
        init_channel_id=ch,
    )
    if ok:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload or {},
            metadata=request.metadata,
        )
    else:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": err or "session.rename failed", "code": code or ""},
            metadata=request.metadata,
        )
    await ctx.sink.send_response(resp)

async def handle_session_rebind_project(ctx: RequestContext) -> None:
    """处理 session.rebind_project：TUI /workspace set 切换工作目录时重绑当前会话。

    AgentServer 拥有会话运行态与 metadata 写入权，因此重绑必须在 AgentServer
    完成：分离部署 / user_id 隔离目录时，Gateway 本地写不会落到 AgentServer
    所在的会话目录。本 handler 复用 ``find_or_create_code_project_for_tui_params``
    解析/创建新目录对应的 code 项目，再 ``rebind_session_project`` 强制更新
    metadata（含 channel_metadata.project_dir/cwd，保证 /resume current-dir 过滤
    正确归位）。下一轮 chat.send 会读到新 project_dir 并据此重选 agent 实例。
    """
    request = ctx.request
    from jiuwenswarm.server.runtime.session.project_store import (
        find_or_create_code_project_for_tui_params,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import (
        rebind_session_project,
    )

    sid = (request.session_id or "").strip()
    params = request.params if isinstance(request.params, dict) else {}
    candidate_dir = str(params.get("project_dir") or params.get("cwd") or "").strip()
    if not sid:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session_id is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    elif not candidate_dir:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "project_dir is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    elif not get_session_metadata(sid):
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session not found", "code": "NOT_FOUND"},
            metadata=request.metadata,
        )
    else:
        try:
            project = find_or_create_code_project_for_tui_params(
                {"project_dir": candidate_dir}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentServer] session.rebind_project resolve project failed: %s",
                exc,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(exc),
                    "code": "PROJECT_RESOLVE_FAILED",
                },
                metadata=request.metadata,
            )
        else:
            if project is None:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": "project_dir must be a non-empty absolute path",
                        "code": "BAD_REQUEST",
                    },
                    metadata=request.metadata,
                )
            else:
                updated = rebind_session_project(
                    session_id=sid,
                    project_id=project.project_id,
                    project_dir=project.project_dir,
                    work_mode=project.work_mode,
                )
                if not updated:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "session not found", "code": "NOT_FOUND"},
                        metadata=request.metadata,
                    )
                else:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "session_id": sid,
                            "project_id": project.project_id,
                            "project_dir": project.project_dir,
                            "project_name": project.name,
                            "work_mode": project.work_mode,
                        },
                        metadata=request.metadata,
                    )
    await ctx.sink.send_response(resp)

async def handle_session_input_intent(ctx: RequestContext) -> None:
    """Publish a user input intent without naming optional consumers."""
    request = ctx.request
    services = ctx.services
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    intent_id = str(params.get("intent_id") or request.request_id or "").strip()
    if not session_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session_id is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    else:
        try:
            outcome = await services.execution_runtime().record_session_input_intent(
                request,
                view_id=str(params.get("view_id") or "default-view"),
            )
            logger.info(
                "[AgentWebSocketServer] session.input.intent processed: "
                "session_id=%s intent_id=%s outcome=%s",
                session_id,
                intent_id,
                outcome,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "scheduled": outcome == "scheduled",
                    "outcome": outcome,
                },
                metadata=request.metadata,
            )
        except Exception as exc:
            logger.warning(
                "[AgentWebSocketServer] session.input.intent failed closed: "
                "session_id=%s error=%s",
                session_id,
                exc,
            )
            # KVC is an optional optimization; typing must not fail.
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "scheduled": False,
                    "outcome": "failed",
                },
                metadata=request.metadata,
            )

    await ctx.sink.send_response(resp)

async def handle_session_message_continue_queued(ctx: RequestContext) -> None:
    request = ctx.request
    services = ctx.services
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    service = services.session_message_service
    try:
        if not session_id:
            raise SessionMessagingError("INVALID_ARGUMENT", "session_id is required")
        if service is None:
            raise SessionMessagingError(
                "HOST_CAPABILITY_UNAVAILABLE", "Session messaging is unavailable"
            )
        payload = await service.continue_queued_for_target(session_id, request.user_id)
        ok = True
    except SessionMessagingError as exc:
        payload = {"code": exc.code, "error": str(exc)}
        ok = False
    response = AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
        metadata=request.metadata,
    )
    await ctx.sink.send_response(response)

async def handle_session_switch(ctx: RequestContext) -> None:
    """Translate ``session.switch`` between WebSocket wire and Runtime."""
    request = ctx.request
    services = ctx.services
    params = request.params if isinstance(request.params, dict) else {}
    target = str(params.get("session_id") or request.session_id or "").strip()
    previous_session_id = str(params.get("previous_session_id") or "").strip()

    if not target:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session_id is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
        await ctx.sink.send_response(resp)
        return

    channel_id = str(request.channel_id or "").strip() or "default"
    lock_key = f"{ctx.connection_id}:{channel_id}"
    switch_locks = _session_switch_locks()
    switch_lock = switch_locks.get(lock_key)
    if switch_lock is None:
        switch_lock = asyncio.Lock()
        switch_locks[lock_key] = switch_lock

    async with switch_lock:
        runtime = services.execution_runtime()
        prepared = None
        commit_context = SessionProvisionCommitContext(
            foreground_scope_id=str(
                params.get("view_id") or f"ws:{ctx.connection_id}"
            )
        )
        try:
            await runtime.start()
            prepared = await runtime.prepare_session_switch(
                SessionSwitchInput(
                    channel_id=channel_id,
                    target_session_id=target,
                    previous_session_id=previous_session_id,
                    mode=params.get("mode", "agent.plan"),
                    previous_mode=params.get("previous_mode"),
                    team_hint=bool(params.get("team")),
                )
            )
            result = await runtime.commit_session_provision(
                prepared,
                timing=(
                    SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
                ),
                context=commit_context,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": result.session_id,
                    "mode": result.mode,
                    "switched": result.switched,
                },
                metadata=request.metadata,
            )

            await ctx.sink.send_response(resp)
        finally:
            primary_error = sys.exception()
            if (
                prepared is not None
                and prepared.state is SessionProvisionState.PREPARED
            ):
                try:
                    await runtime.abort_session_provision(prepared)
                except asyncio.CancelledError:
                    if primary_error is None:
                        raise
                    logger.warning(
                        "[AgentServer] session.switch abort was cancelled "
                        "while preserving %s",
                        type(primary_error).__name__,
                    )
                except Exception as abort_exc:  # noqa: BLE001
                    logger.warning(
                        "[AgentServer] session.switch abort failed: %s",
                        abort_exc,
                    )

async def resolve_rewind_agent(
    services: AgentServerServices,
    channel_id: str,
    session_id: str | None = None,
) -> tuple[Any, Any] | None:
    """Return (deep_agent, react_agent) for rewind context rebuild.

    Prefer the live **session-scoped** DeepAgent used by chat.send.
    Root ``agent.get_instance()`` is a separate DeepAgent whose
    context_engine / ``_interaction_session`` are not the ones the next
    user turn will read — updating them leaves the model still seeing
    rewound turns.
    """
    sid = str(session_id or "").strip()
    agent = (
        services.agent_manager.get_agent_for_session_nowait(
            channel_id=channel_id or "default",
            session_id=sid,
        )
        if sid
        else None
    )
    if agent is None:
        agent = services.agent_manager.get_agent_nowait(
            channel_id=channel_id or "default"
        )
    if agent is None:
        return None

    deep_agent = None
    if sid:
        adapter = services.resolve_adapter(agent)
        if adapter is not None:
            # Already session-scoped (rare): use it directly.
            if getattr(adapter, "_is_session_scoped_adapter", False):
                deep_agent = getattr(adapter, "_instance", None)
            else:
                get_cached = getattr(adapter, "_get_cached_session_adapter", None)
                if callable(get_cached):
                    session_adapter = get_cached(sid)
                    if session_adapter is not None:
                        deep_agent = getattr(session_adapter, "_instance", None)
                        if deep_agent is None:
                            logger.warning(
                                "[AgentWS] rewind: cached session adapter has no "
                                "instance for session_id=%s",
                                sid,
                            )

    if deep_agent is None:
        # Fallback: no live session adapter yet (e.g. rewind before any chat
        # on this process). Checkpointer-only rebuild still helps cold start,
        # so build the root DeepAgent here if it has not been needed yet.
        deep_agent = await agent.ensure_instance()
        if deep_agent is not None and sid:
            logger.info(
                "[AgentWS] rewind: no session-scoped DeepAgent for %s; "
                "falling back to root instance",
                sid,
            )

    if deep_agent is None:
        return None
    react_agent = deep_agent.react_agent
    if react_agent is None:
        return None
    return (deep_agent, react_agent)

async def handle_session_rewind(ctx: RequestContext) -> None:
    """Full rewind: truncate history.json + context_engine + update checkpointer."""
    request = ctx.request
    services = ctx.services
    restore_files = request.req_method == ReqMethod.SESSION_REWIND_AND_RESTORE
    compact = request.req_method == ReqMethod.SESSION_REWIND_COMPACT
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        rewind_session,
        rewind_session_context,
    )

    params = request.params if isinstance(request.params, dict) else {}
    target_sid = str(params.get("session_id") or request.session_id or "").strip()
    turn_index = params.get("turn_index")
    compact_summary = params.get("compact_summary") if compact else None
    direction = str(params.get("direction") or "from").strip() if compact else "from"
    summarized_count = int(params.get("summarized_count", 0) or 0) if compact else 0

    if not target_sid or turn_index is None:
        await _send_error(ctx, "session_id and turn_index required", "BAD_REQUEST")
        return

    try:
        turn_index = int(turn_index)
    except (ValueError, TypeError):
        await _send_error(ctx, "turn_index must be integer", "BAD_REQUEST")
        return

    try:
        # Step 1: Optionally restore files first
        restore_result: dict[str, Any] = {}
        if restore_files:
            from jiuwenswarm.agents.harness.common.session_ops_service import (
                restore_session_files,
            )
            restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)

        # Step 2: Truncate history.json (local file operation)
        # "up_to" direction: keep messages from turn_index onward, summarize the prefix.
        # compact_partial_session handles this correctly (rewind_session only supports
        # the "from" direction — keeping the prefix and truncating the tail).
        if compact and direction == "up_to":
            from jiuwenswarm.agents.harness.common.session_ops_service import (
                compact_partial_session,
            )
            rewind_result = compact_partial_session(
                session_id=target_sid,
                turn_index=turn_index,
                direction="up_to",
                llm_summary=compact_summary,
            )
        else:
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)

        # Step 3: Truncate context_engine in-place + persist to checkpointer.
        # rewind_session_context reads the already-truncated history.json and
        # converts ALL records to context messages, so it naturally produces the
        # correct result for both "from" and "up_to" directions.
        context_ok = False
        pair = await resolve_rewind_agent(services, 
            request.channel_id or "default",
            session_id=target_sid,
        )
        if pair is None:
            logger.warning(
                "[AgentWS] session.rewind: no agent for context rebuild "
                "(session_id=%s channel=%s); history truncated but model "
                "context may still contain rewound turns",
                target_sid,
                request.channel_id,
            )
        else:
            deep_agent, _react_agent = pair
            try:
                context_ok = await rewind_session_context(
                    deep_agent=deep_agent,
                    session_id=target_sid,
                    turn_index=turn_index,
                )
            except Exception as exc:
                logger.warning(
                    "[AgentWS] session.rewind context truncation failed: %s", exc,
                )
            if not context_ok:
                logger.warning(
                    "[AgentWS] session.rewind: history truncated but "
                    "rewind_context=false (session_id=%s)",
                    target_sid,
                )

        payload = {**rewind_result, "rewind_context": context_ok}
        if restore_files:
            payload["restored_files"] = restore_result.get("restored_files", [])
            payload["deleted_files"] = restore_result.get("deleted_files", [])
            payload["restore_errors"] = restore_result.get("errors", [])

        # Step 4: For compact mode, append boundary + rewind_summary + compact_summary records.
        # compact_partial_session already writes these for "up_to", so only append for "from".
        if compact and direction == "from":
            import time as _time
            import uuid as _uuid

            request_id = str(_uuid.uuid4())
            now = _time.time()

            short_text = (
                f"Summarized {summarized_count} messages from this point."
                if direction == "from"
                else f"Summarized {summarized_count} messages up to this point."
            )

            await run_history_io(append_history_record,
                session_id=target_sid,
                request_id=request_id,
                channel_id=request.channel_id or "tui",
                role="assistant",
                event_type="context.compact_boundary",
                content="Conversation compacted",
                timestamp=now,
                extra={
                    "compact_metadata": {
                        "trigger": "manual_rewind",
                        "direction": direction,
                        "turn_index": turn_index,
                        "summarized_messages": summarized_count,
                    },
                },
            )

            await run_history_io(append_history_record,
                session_id=target_sid,
                request_id=request_id,
                channel_id=request.channel_id or "tui",
                role="assistant",
                event_type="context.rewind_summary",
                content=short_text,
                timestamp=now + 0.001,
                extra={
                    "compact_metadata": {
                        "trigger": "manual_rewind",
                        "direction": direction,
                        "turn_index": turn_index,
                        "summarized_messages": summarized_count,
                    },
                    "is_compact_summary": True,
                },
            )

            if isinstance(compact_summary, str) and compact_summary.strip():
                await run_history_io(append_history_record,
                    session_id=target_sid,
                    request_id=request_id,
                    channel_id=request.channel_id or "tui",
                    role="assistant",
                    event_type="context.compact_summary",
                    content=compact_summary.strip(),
                    timestamp=now + 0.002,
                    extra={
                        "compact_metadata": {
                            "trigger": "manual_rewind",
                            "direction": direction,
                            "turn_index": turn_index,
                            "summarized_messages": summarized_count,
                        },
                        "is_compact_summary": True,
                        "transcript_only": True,
                    },
                )

            payload["summarized_messages"] = summarized_count

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    except Exception as exc:
        logger.exception("[AgentWS] session.rewind failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )

    await ctx.sink.send_response(resp)

async def handle_session_rewind_context(ctx: RequestContext) -> None:
    """Truncate history.json + in-memory context_engine for a session."""
    request = ctx.request
    services = ctx.services
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        rewind_session,
        rewind_session_context,
    )

    params = request.params if isinstance(request.params, dict) else {}
    target_sid = str(params.get("session_id") or request.session_id or "").strip()
    turn_index = params.get("turn_index")

    if not target_sid or turn_index is None:
        await _send_error(ctx, "session_id and turn_index required", "BAD_REQUEST")
        return

    try:
        turn_index = int(turn_index)
    except (ValueError, TypeError):
        await _send_error(ctx, "turn_index must be integer", "BAD_REQUEST")
        return

    pair = await resolve_rewind_agent(services, 
        request.channel_id or "default",
        session_id=target_sid,
    )
    if pair is None:
        await _send_error(ctx, "no agent instance available")
        return
    deep_agent, _react_agent = pair

    try:
        # Truncate history.json first so rewind_session_context reads the
        # correct truncated state (the new implementation rebuilds context
        # from history.json on disk).
        rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
        context_ok = await rewind_session_context(
            deep_agent=deep_agent,
            session_id=target_sid,
            turn_index=turn_index,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={**rewind_result, "rewind_context": context_ok},
            metadata=request.metadata,
        )
    except ValueError as exc:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    except Exception as exc:
        logger.exception("[AgentWS] session.rewind_context failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
            metadata=request.metadata,
        )

    await ctx.sink.send_response(resp)

async def handle_history_get_page(ctx: RequestContext) -> None:
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id")
    page_idx = params.get("page_idx")
    subagent_id = params.get("subagent_id")
    data = get_conversation_history(
        session_id=session_id,
        page_idx=page_idx,
        subagent_id=subagent_id if isinstance(subagent_id, str) else None,
    )
    if data is None:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "invalid page_idx or session history not found"},
        )
    else:
        # 非流式整页塞进单个 wire 帧（AgentResponse.payload=data），没法像流式那样
        # 按 channel_id 分片流——这里所有通道统一走 _sanitize_history_record_for_wire
        # 把每条 record 裁剪到 16KB + 64KB collapse 之内，保证 wire 帧有界。
            # 流式路径在 handle_history_get_stream 里按 channel_id 分流（web 走 split，
        # 其他走 sanitize），与此处无关。
        if isinstance(data.get("messages"), list):
            data["messages"] = [
                _sanitize_history_record_for_wire(record)
                for record in data["messages"]
                if isinstance(record, dict)
            ]
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=data,
        )
    await ctx.sink.send_response(resp)

async def handle_history_get_stream(ctx: RequestContext) -> None:
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id")
    page_idx = params.get("page_idx")
    cursor_protocol = "cursor" in params
    request_cursor = params.get("cursor")
    subagent_id = params.get("subagent_id")
    try:
        if cursor_protocol:
            if request_cursor is not None and not isinstance(request_cursor, str):
                raise InvalidHistoryCursor("history cursor must be null or a string")
            data = await asyncio.to_thread(
                get_conversation_history_cursor,
                session_id,
                request_cursor,
                limit=params.get("limit", _HISTORY_PAGE_SIZE),
                subagent_id=subagent_id if isinstance(subagent_id, str) else None,
            )
        else:
            data = await asyncio.to_thread(
                get_conversation_history,
                session_id=session_id,
                page_idx=page_idx,
                subagent_id=subagent_id if isinstance(subagent_id, str) else None,
            )
    except (InvalidHistoryCursor, HistorySnapshotChanged) as exc:
        error_code = (
            "HISTORY_SNAPSHOT_CHANGED"
            if isinstance(exc, HistorySnapshotChanged)
            else "INVALID_HISTORY_CURSOR"
        )
        error_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "history.message",
                "status": "error",
                "error": str(exc),
                "code": error_code,
                "session_id": str(session_id or ""),
                "subagent_id": str(subagent_id or ""),
                "cursor": request_cursor,
            },
            is_complete=True,
        )
        await _send_chunk(ctx, error_chunk, 0)
        return
    if data is None:
        err_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "chat.error",
                "error": "invalid page_idx or session history not found",
            },
            is_complete=True,
        )
        await _send_chunk(ctx, err_chunk, 0)
        return

    messages = data.get("messages", [])
    total_pages = data.get("total_pages")
    page = data.get("page_idx")
    next_cursor = data.get("next_cursor")
    has_more = data.get("has_more")
    snapshot_id = data.get("snapshot_id")
    snapshot_end = data.get("snapshot_end")
    response_subagent_id = data.get("subagent_id")
    sequence = 0
    # 仅 web 通道走分片流（前端 HistoryRecordReassembler 重组）。
    # 其他通道（tui/acp/...）不认 _part 字段，走旧 _sanitize_history_record_for_wire
    # 单帧 + collapse 路径，维持现状，零回归。
    use_split = request.channel_id == "web"
    if isinstance(messages, list):
        for item in messages:
            if use_split:
                chunks_for_record = split_history_record_for_stream(item)
            else:
                chunks_for_record = [_sanitize_history_record_for_wire(item)]
            for chunk_record in chunks_for_record:
                chunk = AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "history.message",
                        "message": chunk_record,
                        "session_id": str(session_id or ""),
                        "subagent_id": str(response_subagent_id or subagent_id or ""),
                        "total_pages": total_pages,
                        "page_idx": page,
                        "cursor": request_cursor if cursor_protocol else None,
                        "next_cursor": next_cursor,
                        "has_more": has_more,
                        "snapshot_id": snapshot_id,
                        "snapshot_end": snapshot_end,
                    },
                    is_complete=False,
                )
                sent_chunk = await _send_chunk(ctx, chunk, sequence)
                sequence += 1
                if not sent_chunk:
                    logger.warning(
                        "[AgentWebSocketServer] history 流式响应因 chunk 超限而停止: "
                        "request_id=%s sequence=%s",
                        request.request_id,
                        sequence,
                    )
                    return

    next_seq = sequence

    # Session open / refresh: push full todo snapshot before history "done"
    # so the frontend todo panel restores without reading workspace files.
    # Only the initial page/batch — pagination must not re-flash the panel.
    is_initial_history_batch = (
        cursor_protocol and request_cursor is None
    ) or (not cursor_protocol and page_idx == 1)
    if is_initial_history_batch and isinstance(session_id, str) and session_id.strip():
        todos = load_todo_snapshot_for_frontend(
            session_id.strip(),
            **_todo_snapshot_session_fields(session_id.strip()),
        )
        todo_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "todo.updated",
                "todos": todos,
                "session_id": session_id.strip(),
            },
            is_complete=False,
        )
        sent_todo = await _send_chunk(ctx, todo_chunk, next_seq)
        if not sent_todo:
            # chat timeline still finishes; log so oversized snapshots are visible.
            logger.warning(
                "[AgentWebSocketServer] history todo.updated snapshot send failed "
                "(oversized or replaced): request_id=%s session_id=%s seq=%s "
                "todo_count=%s",
                request.request_id,
                session_id.strip(),
                next_seq,
                len(todos),
            )
        next_seq += 1

    done_chunk = AgentResponseChunk(
        request_id=request.request_id,
        channel_id=request.channel_id,
        payload={
            "event_type": "history.message",
            "status": "done",
            "session_id": str(session_id or ""),
            "subagent_id": str(response_subagent_id or subagent_id or ""),
            "total_pages": total_pages,
            "page_idx": page,
            "cursor": request_cursor if cursor_protocol else None,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "snapshot_id": snapshot_id,
            "snapshot_end": snapshot_end,
        },
        is_complete=True,
    )
    await _send_chunk(ctx, done_chunk, next_seq)

async def handle_history_append_record(ctx: RequestContext) -> None:
    """Append a supplied history record without creating an Agent turn.

    Cron uses this for terminal failures.  Falling through to the normal
    request path would treat the failure text as chat input and can invoke
    the unavailable model a second time, leaving the frontend with no
    durable record when it reloads the execution session.
    """
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = str(params.get("session_id") or request.session_id or "").strip()
    content = params.get("content")
    if not session_id or not is_valid_session_id(session_id) or content is None:
        await _send_error(ctx, "session_id and content required", "BAD_REQUEST")
        return

    request_id = str(params.get("request_id") or request.request_id or "").strip()
    channel_id = str(params.get("channel_id") or request.channel_id or "").strip()
    role = "assistant" if str(params.get("role") or "assistant") == "assistant" else "user"
    event_type = str(params.get("event_type") or "").strip() or None
    mode = str(params.get("mode") or "").strip() or None
    try:
        timestamp = float(params.get("timestamp") or request.timestamp or 0.0)
    except (TypeError, ValueError):
        timestamp = 0.0
    if timestamp <= 0:
        timestamp = _dt.datetime.now().timestamp()

    try:
        await run_history_io(append_history_record,
            session_id=session_id,
            request_id=request_id,
            channel_id=channel_id,
            role=role,
            content=content,
            timestamp=timestamp,
            event_type=event_type,
            mode=mode,
        )
        # Cron 失败补写可能落到尚未有标题的会话（会话分配失败的 run 写
        # 占位会话）。assistant 记录不触发 auto_title，这里按调用方传入
        # 的 title 回填空标题；已有标题（正常 run 的 auto_title）不覆盖。
        title = str(params.get("title") or "").strip()
        if title:
            try:
                # get_session_metadata 已在模块顶部导入，不再局部重复导入
                # （redefined-outer-name）；update_session_metadata 沿用本文件
                # 使用点局部导入的既有风格。
                from jiuwenswarm.server.runtime.session.session_metadata import (
                    update_session_metadata,
                )

                current = get_session_metadata(session_id) or {}
                if not str(current.get("title") or "").strip():
                    update_session_metadata(
                        session_id=session_id,
                        title=title,
                        touch_last_message_at=False,
                    )
            except Exception as title_exc:  # noqa: BLE001
                logger.warning(
                    "[AgentWebSocketServer] history.append_record title "
                    "backfill failed: session_id=%s error=%s",
                    session_id,
                    title_exc,
                )
        # The history writer is asynchronous.  Wait for a FIFO completion
        # marker so a frontend history reload immediately after this RPC
        # observes the newly appended terminal record.
        receipt = enqueue_history_request_completion(
            session_id, request_id, terminal_status="failed"
        )
        if receipt is not None:
            await wait_for_history_receipt(receipt, timeout=5.0)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"persisted": True, "session_id": session_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[AgentWebSocketServer] history.append_record failed: session_id=%s error=%s",
            session_id,
            exc,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
        )
    await ctx.sink.send_response(resp)

def get_conversation_history(
    session_id: str,
    page_idx: int,
    *,
    subagent_id: str | None = None,
) -> dict[str, Any] | None:
    # 按照 session_id 和分页消息获取历史记录
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if not isinstance(page_idx, int) or page_idx <= 0:
        return None

    normalized_session_id = session_id.strip()
    normalized_subagent_id = (
        subagent_id.strip() if isinstance(subagent_id, str) and subagent_id.strip() else None
    )
    if normalized_subagent_id:
        if not history_exists(normalized_session_id, subagent_id=normalized_subagent_id):
            return {
                "messages": [],
                "total_pages": 1,
                "page_idx": page_idx,
                "subagent_id": normalized_subagent_id,
            }
    elif not history_exists(normalized_session_id):
        return None
    try:
        raw = load_history_records(
            normalized_session_id,
            subagent_id=normalized_subagent_id,
        )
    except Exception:
        return None
    if not isinstance(raw, list):
        return None

    page_size = _HISTORY_PAGE_SIZE
    restorable = [
        item for item in raw
        if _is_restorable_history_record(item)
    ]
    total = len(restorable)
    total_pages = max(1, math.ceil(total / page_size))
    if page_idx > total_pages:
        return None

    ordered = list(reversed(restorable))
    start = (page_idx - 1) * page_size
    end = start + page_size
    # 不在此处 sanitize：split_history_record_for_stream（在 handle_history_get_stream
    # 里调）需要拿到原文 content 才能正确切片；先 sanitize 会把 content 砍到 16KB，
    # 切片器拿到的就只剩 16KB，分片就失去意义。
    page_messages = list(ordered[start:end])
    logger.debug(
        "[history.get] session_id=%s subagent_id=%s page_idx=%s "
        "raw_total=%s restorable_total=%s total_pages=%s returned=%s",
        normalized_session_id,
        normalized_subagent_id or "",
        page_idx,
        len(raw),
        total,
        total_pages,
        len(page_messages),
    )
    result = {
        "messages": page_messages,
        "total_pages": total_pages,
        "page_idx": page_idx,
    }
    if normalized_subagent_id:
        result["subagent_id"] = normalized_subagent_id
    return result


def get_conversation_history_cursor(
    session_id: str,
    cursor: str | None,
    *,
    limit: int = _HISTORY_PAGE_SIZE,
    subagent_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(session_id, str) or not session_id.strip():
        raise InvalidHistoryCursor("session_id is required")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _HISTORY_PAGE_SIZE:
        raise InvalidHistoryCursor(
            f"history limit must be between 1 and {_HISTORY_PAGE_SIZE}"
        )
    normalized_subagent_id = (
        subagent_id.strip()
        if isinstance(subagent_id, str) and subagent_id.strip()
        else None
    )
    result = read_history_cursor_page(
        session_id.strip(),
        cursor=cursor,
        limit=limit,
        is_restorable=_is_restorable_history_record,
        subagent_id=normalized_subagent_id,
    )
    if normalized_subagent_id:
        result["subagent_id"] = normalized_subagent_id
    return result

