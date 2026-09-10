# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""会话域 handler"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
from typing import Any
from weakref import WeakValueDictionary

from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _background_session_kvc_tasks,
    _sessions_dir_for_request,
    _is_team_metadata_mode,
    _log_background_session_kvc_failure,
    _plan_exited_sessions,
    send_error_wire,
)
from jiuwenswarm.server.runtime.session.session_history import (
    enrich_history_messages_session_id,
    history_exists,
    load_history_records,
)
from jiuwenswarm.server.runtime.session.session_metadata import remove_session_metadata_cache
from jiuwenswarm.server.wire_truncate import (
    _HISTORY_PAGE_SIZE,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _sanitize_history_record_for_wire,
)

logger = logging.getLogger(__name__)

#: ``session.list`` 分页边界，与 Web fallback 一致。
_LIMIT_DEFAULT = 20
_LIMIT_MAX = 200

# Serialize switch owner preparation and acknowledgements per client
# connection. AgentServer handles WebSocket frames in independent tasks, so
# rapid navigation requests would otherwise race even on one socket.
_session_switch_locks: WeakValueDictionary[str, asyncio.Lock] = (
    WeakValueDictionary()
)


def _coerce_int(value: object, default: int) -> int:
    """宽松解析整数：兼容 int / 整数值 float / 数字字符串。

    与原 ``_handle_session_list`` 的解析规则逐条对应（含 bool 不算 int）。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


async def _resolve_rewind_agent(
    ctx,
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
    agent = ctx.services.agent_manager.get_agent_nowait(
        channel_id=channel_id or "default"
    )
    if agent is None:
        return None
    deep_agent = None
    sid = str(session_id or "").strip()
    if sid:
        adapter = ctx.services.resolve_adapter(agent)
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


def get_conversation_history(
    session_id: str, page_idx: int, sessions_root: str | None = None,
) -> dict[str, Any] | None:
    # 按照 session_id 和分页消息获取历史记录
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if not isinstance(page_idx, int) or page_idx <= 0:
        return None
    normalized_session_id = session_id.strip()
    if not history_exists(normalized_session_id, sessions_root=sessions_root):
        return None
    try:
        raw = load_history_records(normalized_session_id, sessions_root=sessions_root)
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
    resolved_sid = session_id.strip()
    page_slice = ordered[start:end]
    messages_out = enrich_history_messages_session_id(page_slice, resolved_sid)
    page_messages = [
        _sanitize_history_record_for_wire(item)
        for item in messages_out
    ]
    logger.debug(
        "[history.get] session_id=%s page_idx=%s raw_total=%s restorable_total=%s total_pages=%s returned=%s",
        normalized_session_id,
        page_idx,
        len(raw),
        total,
        total_pages,
        len(page_messages),
    )
    return {
        "messages": page_messages,
        "total_pages": total_pages,
        "page_idx": page_idx,
    }


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
        if mode in ("team", "team.plan", "code.team"):
            channel_id = record.get("channel_id", "")
            if channel_id not in ("web", "tui"):
                return False
        return has_content or has_media

    event_type = record.get("event_type")
    if not event_type:
        return has_content
    return event_type in _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES


async def handle_session_list(ctx: RequestContext) -> None:
    """处理 session.list 请求：返回历史会话基础信息列表。

    响应格式与 Web fallback ``_session_list`` 保持一致:
    ``{"sessions": [...], "total": int, "limit": int, "offset": int}``,
    确保按新接口接入分页的 Web 前端能拿到分页元信息。
    """
    from jiuwenswarm.server.runtime.session.session_metadata import get_all_sessions_metadata

    params = ctx.params
    limit = max(1, min(_coerce_int(params.get("limit"), _LIMIT_DEFAULT), _LIMIT_MAX))
    offset = max(0, _coerce_int(params.get("offset"), 0))

    try:
        sessions_root = _sessions_dir_for_request(ctx.request)
        sessions, total = get_all_sessions_metadata(
            limit=limit, offset=offset, sessions_root=sessions_root,
        )
    except Exception as exc:  # noqa: BLE001 - 与原实现一致：失败降级为空列表
        logger.warning("[handlers.session] 获取会话列表失败: %s", exc)
        sessions, total = [], 0

    await ctx.sink.send_unary(
        AgentResponse(
            request_id=ctx.request_id,
            channel_id=ctx.channel_id,
            ok=True,
            payload={
                "sessions": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            metadata=ctx.request.metadata,
        )
    )


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
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_session_switch(ctx: RequestContext) -> None:
    """Switch product sessions without deleting recoverable session state."""
    request = ctx.request
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
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    channel_id = str(request.channel_id or "").strip() or "default"
    # 上游用 id(ws) 给切换锁分槽，这里改用 ctx.connection_id，不依赖连接对象。
    #
    # 前提：``ctx.connection_id`` 必须**跨请求稳定**。WS 是 id(ws)，HTTP 是常量
    # ``HTTP_CONNECTION_ID``；若某个传输给出每请求唯一的值，每个请求会各造一把锁，
    # 这把锁就完全不起作用。守护：``test_http_connection_id_is_stable_across_requests``。
    #
    # 已知边界：键含 connection_id，所以**跨传输不互斥** —— WS 客户端与 HTTP 客户端
    # 在同一 channel 上并发切换会重叠。彻底修法是把键降为被保护的资源本身
    # （``lock_key = channel_id``），但那必须与「把下面的 ``send_wire`` 移出临界区」
    # 一起做：键一旦共享，锁内的 socket 写就会把一条慢连接的阻塞传播给同 channel 的
    # 其他连接（WSSink.send_wire 持连接级 send_lock 做真实 I/O）。两件事拆开做都会留坑，
    # 因此留到确有「同 channel 多连接」场景时单独立项。
    lock_key = f"{ctx.connection_id}:{channel_id}"
    switch_lock = _session_switch_locks.get(lock_key)
    if switch_lock is None:
        switch_lock = asyncio.Lock()
        _session_switch_locks[lock_key] = switch_lock

    async with switch_lock:
        (
            _,
            resolved_mode,
            context,
            team_manager,
            dispatch_signals,
        ) = await ctx.services.prepare_session_switch_owner(
            channel_id=channel_id,
            target_session_id=target,
            previous_session_id=previous_session_id,
            params=params,
            reason="session.switch: ",
        )
        kvc_args: dict[str, Any] | None = None
        if context is not None and dispatch_signals is not None:
            kvc_args = {
                "channel_id": channel_id,
                "target_session_id": target,
                "previous_session_id": previous_session_id,
                "reason": "session.switch: ",
                "context": context,
                "team_manager": team_manager,
                "dispatch_signals": dispatch_signals,
            }
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "session_id": target,
                "mode": resolved_mode,
                "switched": True,
            },
            metadata=request.metadata,
        )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)

        if kvc_args is not None:
            kvc_task = asyncio.create_task(
                ctx.services.dispatch_session_switch_kvc(**kvc_args),
                name=f"session-switch-kvc-{target}",
            )
            _background_session_kvc_tasks.add(kvc_task)
            kvc_task.add_done_callback(_background_session_kvc_tasks.discard)
            kvc_task.add_done_callback(_log_background_session_kvc_failure)


async def handle_session_delete(ctx: RequestContext) -> None:
    """Delete a single session and its recoverable runtime state."""
    request = ctx.request
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    from jiuwenswarm.agents.harness.team import get_team_manager

    params = request.params if isinstance(request.params, dict) else {}
    target = str(params.get("session_id") or "").strip()
    if not target:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "session_id is required", "code": "BAD_REQUEST"},
            metadata=request.metadata,
        )
    else:
        from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir

        sessions_root = _sessions_dir_for_request(request)
        session_dir, invalid_reason = resolve_session_dir(
            target, sessions_root=sessions_root
        )
        if session_dir is not None:
            # 合法的永久删除请求首先回收进程内授权。即使会话目录已被外部删除
            # 或随后文件删除失败，也不能让 ACTIVE/PENDING Grant 继续存活。
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
                    clear_deleted_session,
                )

                clear_deleted_session(target)
            except Exception:  # noqa: BLE001 — Grant 回收失败不掩盖原删除结果
                logger.warning(
                    "[AgentWebSocketServer] session.delete skill grant cleanup failed: "
                    "session_id=%s",
                    target,
                    exc_info=True,
                )
        if session_dir is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": invalid_reason or "invalid session_id", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        elif not session_dir.exists():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session not found", "code": "NOT_FOUND"},
                metadata=request.metadata,
            )
        elif not session_dir.is_dir():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "session is not a directory", "code": "BAD_REQUEST"},
                metadata=request.metadata,
            )
        else:
            checkpoint_resp = await ctx.services.ensure_persistent_checkpointer_response(request)
            if checkpoint_resp is not None:
                resp = checkpoint_resp
            else:
                metadata = get_session_metadata(
                    target,
                    sessions_root=sessions_root,
                )
                is_team_mode = _is_team_metadata_mode(metadata)
                team_name = str(metadata.get("team_name") or "").strip()
                channel_id = str(metadata.get("channel_id") or request.channel_id or "").strip() or None
                if not is_team_mode:
                    from jiuwenswarm.server.runtime.session.kv_cache_product_hooks import (
                        evict_plan_session,
                    )

                    await evict_plan_session(
                        session_id=target,
                        agent_manager=ctx.services.agent_manager,
                        channel_id=channel_id,
                    )
                try:
                    if is_team_mode:
                        team_manager = get_team_manager(channel_id)
                        deleted = await team_manager.delete_session_runtime(
                            target,
                            reason="session.delete: ",
                            sessions_root=sessions_root,
                        )
                    else:
                        await Runner.release(target)
                        deleted = True
                except Exception as exc:
                    logger.warning(
                        "[AgentWebSocketServer] session.delete runtime cleanup failed: session_id=%s error=%s",
                        target,
                        exc,
                    )
                    deleted = False

                if not deleted:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "session runtime cleanup failed", "code": "DELETE_FAILED"},
                        metadata=request.metadata,
                    )
                else:
                    shutil.rmtree(session_dir)
                    _plan_exited_sessions.discard(target)
                    remove_session_metadata_cache(
                        target,
                        sessions_root=sessions_root,
                    )
                    if is_team_mode:
                        try:
                            from jiuwenswarm.server.runtime.team_binding_store import get_team_binding_store

                            get_team_binding_store().unbind_session(
                                team_name=team_name or None,
                                session_id=target,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "[AgentWebSocketServer] failed to unbind deleted team session: "
                                "session_id=%s team_name=%s error=%s",
                                target,
                                team_name,
                                exc,
                            )
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={"session_id": target},
                        metadata=request.metadata,
                    )

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_session_rewind_full(ctx: RequestContext, restore_files: bool = False, compact: bool = False) -> None:
    """Full rewind: truncate history.json + context_engine + update checkpointer."""
    request = ctx.request
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
        wire = send_error_wire(
            request,
            "session_id and turn_index required", "BAD_REQUEST",
        )
        await ctx.sink.send_wire(wire)
        return

    try:
        turn_index = int(turn_index)
    except (ValueError, TypeError):
        wire = send_error_wire(
            request,
            "turn_index must be integer", "BAD_REQUEST",
        )
        await ctx.sink.send_wire(wire)
        return

    try:
        # Step 1: Optionally restore files first
        restore_result: dict[str, Any] = {}
        if restore_files:
            from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files
            restore_result = restore_session_files(session_id=target_sid, turn_index=turn_index)

        # Step 2: Truncate history.json (local file operation)
        # "up_to" direction: keep messages from turn_index onward, summarize the prefix.
        # compact_partial_session handles this correctly (rewind_session only supports
        # the "from" direction — keeping the prefix and truncating the tail).
        if compact and direction == "up_to":
            from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session
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
        pair = await _resolve_rewind_agent(ctx,
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
            import uuid as _uuid
            import time as _time
            from jiuwenswarm.server.runtime.session.session_history import append_history_record
            request_id = str(_uuid.uuid4())
            now = _time.time()

            short_text = (
                f"Summarized {summarized_count} messages from this point."
                if direction == "from"
                else f"Summarized {summarized_count} messages up to this point."
            )

            append_history_record(
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

            append_history_record(
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
                append_history_record(
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_session_rewind_context(ctx: RequestContext) -> None:
    """Truncate history.json + in-memory context_engine for a session."""
    request = ctx.request
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        rewind_session,
        rewind_session_context,
    )

    params = request.params if isinstance(request.params, dict) else {}
    target_sid = str(params.get("session_id") or request.session_id or "").strip()
    turn_index = params.get("turn_index")

    if not target_sid or turn_index is None:
        wire = send_error_wire(
            request,
            "session_id and turn_index required", "BAD_REQUEST",
        )
        await ctx.sink.send_wire(wire)
        return

    try:
        turn_index = int(turn_index)
    except (ValueError, TypeError):
        wire = send_error_wire(
            request,
            "turn_index must be integer", "BAD_REQUEST",
        )
        await ctx.sink.send_wire(wire)
        return

    pair = await _resolve_rewind_agent(ctx,
        request.channel_id or "default",
        session_id=target_sid,
    )
    if pair is None:
        wire = send_error_wire(
            request, "no agent instance available",
        )
        await ctx.sink.send_wire(wire)
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_history_get(ctx: RequestContext) -> None:
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id")
    # 归一化数字型参数
    page_idx = _coerce_int(params.get("page_idx"), 0)
    sessions_root = str(_sessions_dir_for_request(request))
    data = get_conversation_history(
        session_id=session_id, page_idx=page_idx, sessions_root=sessions_root,
    )
    if data is None:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "invalid page_idx or session history not found"},
        )
    else:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=data,
        )
    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_history_get_stream(ctx: RequestContext) -> None:
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    session_id = params.get("session_id")
    # 归一化数字型参数
    page_idx = _coerce_int(params.get("page_idx"), 0)
    sessions_root = str(_sessions_dir_for_request(request))
    data = get_conversation_history(
        session_id=session_id, page_idx=page_idx, sessions_root=sessions_root,
    )
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
        wire = encode_agent_chunk_for_wire(
            err_chunk,
            response_id=request.request_id,
            sequence=0,
        )
        await ctx.sink.send_wire(wire)
        return

    messages = data.get("messages", [])
    total_pages = data.get("total_pages")
    page = data.get("page_idx")
    if isinstance(messages, list):
        for seq, item in enumerate(messages):
            chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "history.message",
                    "message": item,
                    "session_id": str(session_id or ""),
                    "total_pages": total_pages,
                    "page_idx": page,
                },
                is_complete=False,
            )
            wire = encode_agent_chunk_for_wire(
                chunk,
                response_id=request.request_id,
                sequence=seq,
            )
            sent_original = False
            sent_original = await ctx.sink.send_wire(wire)
            if not sent_original:
                logger.warning(
                    "[AgentWebSocketServer] history 流式响应因单个 chunk 超限而停止: "
                    "request_id=%s seq=%s",
                    request.request_id,
                    seq,
                )
                return

    done_chunk = AgentResponseChunk(
        request_id=request.request_id,
        channel_id=request.channel_id,
        payload={
            "event_type": "history.message",
            "status": "done",
            "session_id": str(session_id or ""),
            "total_pages": total_pages,
            "page_idx": page,
        },
        is_complete=True,
    )
    done_seq = len(messages) if isinstance(messages, list) else 0
    wire_done = encode_agent_chunk_for_wire(
        done_chunk,
        response_id=request.request_id,
        sequence=done_seq,
    )
    await ctx.sink.send_wire(wire_done)
