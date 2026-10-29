# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sessions management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from jiuwenswarm.server.runtime.session.session_history import append_history_record
import asyncio
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
)
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
from jiuwenswarm.server.runtime.session.history_io import run_history_io

logger = logging.getLogger(__name__)


class SessionsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_session_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.list 请求：返回历史会话基础信息列表。

        响应格式与 Web fallback ``_session_list`` 保持一致:
        ``{"sessions": [...], "total": int, "limit": int, "offset": int}``,
        确保按新接口接入分页的 Web 前端能拿到分页元信息。
        """
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        limit = 20
        offset = 0
        raw_limit = params.get("limit")
        if isinstance(raw_limit, int) and (not isinstance(raw_limit, bool)):
            limit = raw_limit
        elif isinstance(raw_limit, float) and raw_limit.is_integer():
            limit = int(raw_limit)
        elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
            limit = int(raw_limit.strip())
        raw_offset = params.get("offset")
        if isinstance(raw_offset, int) and (not isinstance(raw_offset, bool)):
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
            sessions, total = ([], 0)
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_session_rename(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.rename：与 CLI Gateway 本地回退共用 apply_session_rename。"""
        host = self.host
        from jiuwenswarm.server.runtime.session.session_rename import (
            apply_session_rename,
        )

        sid = request.session_id or ""
        ch = (request.channel_id or "").strip() or "tui"
        ok, payload, err, code = apply_session_rename(
            request.params, sid, init_channel_id=ch
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_session_rebind_project(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.rebind_project：TUI /workspace set 切换工作目录时重绑当前会话。

        AgentServer 拥有会话运行态与 metadata 写入权，因此重绑必须在 AgentServer
        完成：分离部署 / user_id 隔离目录时，Gateway 本地写不会落到 AgentServer
        所在的会话目录。本 handler 复用 ``find_or_create_code_project_for_tui_params``
        解析/创建新目录对应的 code 项目，再 ``rebind_session_project`` 强制更新
        metadata（含 channel_metadata.project_dir/cwd，保证 /resume current-dir 过滤
        正确归位）。下一轮 chat.send 会读到新 project_dir 并据此重选 agent 实例。
        """
        host = self.host
        from jiuwenswarm.server.runtime.session.project_store import (
            find_or_create_code_project_for_tui_params,
        )
        from jiuwenswarm.server.runtime.session.session_metadata import (
            rebind_session_project,
        )

        sid = (request.session_id or "").strip()
        params = request.params if isinstance(request.params, dict) else {}
        candidate_dir = str(
            params.get("project_dir") or params.get("cwd") or ""
        ).strip()
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
            except Exception as exc:
                logger.warning(
                    "[AgentServer] session.rebind_project resolve project failed: %s",
                    exc,
                )
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": str(exc), "code": "PROJECT_RESOLVE_FAILED"},
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
                    previous_project_id = str(
                        (get_session_metadata(
                            sid, cache_bust=True, enable_writeback=False,
                        ) or {}).get("project_id") or ""
                    )
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
                        from jiuwenswarm.server.runtime.recommendation_host import (
                            active_recommendation_host,
                        )

                        try:
                            recommendation = active_recommendation_host()
                            if (
                                recommendation is not None
                                and previous_project_id
                                and previous_project_id != project.project_id
                            ):
                                await recommendation.session_left_project(previous_project_id, sid)
                        except Exception:  # noqa: BLE001 - rebind already persisted
                            logger.warning(
                                "[AgentWebSocketServer] session rebind recommendation "
                                "hook failed: session_id=%s",
                                sid,
                                exc_info=True,
                            )
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _resolve_rewind_agent(
        self, channel_id: str, session_id: str | None = None
    ) -> tuple[Any, Any] | None:
        """Return (deep_agent, react_agent) for rewind context rebuild.

        Prefer the live **session-scoped** DeepAgent used by chat.send.
        Root ``agent.get_instance()`` is a separate DeepAgent whose
        context_engine / ``_interaction_session`` are not the ones the next
        user turn will read — updating them leaves the model still seeing
        rewound turns.
        """
        host = self.host
        sid = str(session_id or "").strip()
        agent = (
            host._agent_manager.get_agent_for_session_nowait(
                channel_id=channel_id or "default", session_id=sid
            )
            if sid
            else None
        )
        if agent is None:
            agent = host._agent_manager.get_agent_nowait(
                channel_id=channel_id or "default"
            )
        if agent is None:
            return None
        deep_agent = None
        if sid:
            adapter = host._resolve_adapter(agent)
            if adapter is not None:
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
                                    "[AgentWS] rewind: cached session adapter has no instance for session_id=%s",
                                    sid,
                                )
        if deep_agent is None:
            deep_agent = await agent.ensure_instance()
            if deep_agent is not None and sid:
                logger.info(
                    "[AgentWS] rewind: no session-scoped DeepAgent for %s; falling back to root instance",
                    sid,
                )
        if deep_agent is None:
            return None
        react_agent = deep_agent.react_agent
        if react_agent is None:
            return None
        return (deep_agent, react_agent)

    async def _handle_session_rewind_full(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
        restore_files: bool = False,
        compact: bool = False,
    ) -> None:
        """Full rewind: truncate history.json + context_engine + update checkpointer."""
        host = self.host
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")
        compact_summary = params.get("compact_summary") if compact else None
        direction = (
            str(params.get("direction") or "from").strip() if compact else "from"
        )
        summarized_count = int(params.get("summarized_count", 0) or 0) if compact else 0
        if not target_sid or turn_index is None:
            wire = host._send_error_response(
                ws,
                request,
                send_lock,
                "session_id and turn_index required",
                "BAD_REQUEST",
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = host._send_error_response(
                ws, request, send_lock, "turn_index must be integer", "BAD_REQUEST"
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            restore_result: dict[str, Any] = {}
            if restore_files:
                from jiuwenswarm.agents.harness.common.session_ops_service import (
                    restore_session_files,
                )

                restore_result = restore_session_files(
                    session_id=target_sid, turn_index=turn_index
                )
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
                rewind_result = rewind_session(
                    session_id=target_sid, turn_index=turn_index
                )
            context_ok = False
            pair = await host._resolve_rewind_agent(
                request.channel_id or "default", session_id=target_sid
            )
            if pair is None:
                logger.warning(
                    "[AgentWS] session.rewind: no agent for context rebuild (session_id=%s channel=%s); history truncated but model context may still contain rewound turns",
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
                        "[AgentWS] session.rewind context truncation failed: %s", exc
                    )
                if not context_ok:
                    logger.warning(
                        "[AgentWS] session.rewind: history truncated but rewind_context=false (session_id=%s)",
                        target_sid,
                    )
            payload = {**rewind_result, "rewind_context": context_ok}
            if restore_files:
                payload["restored_files"] = restore_result.get("restored_files", [])
                payload["deleted_files"] = restore_result.get("deleted_files", [])
                payload["restore_errors"] = restore_result.get("errors", [])
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
                await run_history_io(
                    append_history_record,
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
                        }
                    },
                )
                await run_history_io(
                    append_history_record,
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
                    await run_history_io(
                        append_history_record,
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_session_rewind_context(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Truncate history.json + in-memory context_engine for a session."""
        host = self.host
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session,
            rewind_session_context,
        )

        params = request.params if isinstance(request.params, dict) else {}
        target_sid = str(params.get("session_id") or request.session_id or "").strip()
        turn_index = params.get("turn_index")
        if not target_sid or turn_index is None:
            wire = host._send_error_response(
                ws,
                request,
                send_lock,
                "session_id and turn_index required",
                "BAD_REQUEST",
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        try:
            turn_index = int(turn_index)
        except (ValueError, TypeError):
            wire = host._send_error_response(
                ws, request, send_lock, "turn_index must be integer", "BAD_REQUEST"
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        pair = await host._resolve_rewind_agent(
            request.channel_id or "default", session_id=target_sid
        )
        if pair is None:
            wire = host._send_error_response(
                ws, request, send_lock, "no agent instance available"
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        deep_agent, _react_agent = pair
        try:
            rewind_result = rewind_session(session_id=target_sid, turn_index=turn_index)
            context_ok = await rewind_session_context(
                deep_agent=deep_agent, session_id=target_sid, turn_index=turn_index
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
