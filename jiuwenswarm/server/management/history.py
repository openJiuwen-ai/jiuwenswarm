# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""History management; resources remain owned by its injected host."""

from __future__ import annotations

from jiuwenswarm.server.runtime.session.session_history import list_session_subagent_ids

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from typing import Any
from jiuwenswarm.server.runtime.session.session_history import HistorySnapshotChanged
from jiuwenswarm.server.runtime.session.session_history import InvalidHistoryCursor
from jiuwenswarm.server.wire_truncate import _HISTORY_PAGE_SIZE
from jiuwenswarm.server.wire_truncate import _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES
from jiuwenswarm.server.wire_truncate import _TEAM_HISTORY_DEFAULT_LIMIT
from jiuwenswarm.server.wire_truncate import _TEAM_HISTORY_DEFAULT_MAX_BYTES
from jiuwenswarm.server.wire_truncate import _TEAM_HISTORY_MAX_LIMIT
from jiuwenswarm.server.wire_truncate import _TEAM_HISTORY_MAX_MAX_BYTES
from jiuwenswarm.server.wire_truncate import _TEAM_HISTORY_MIN_MAX_BYTES
from jiuwenswarm.server.wire_truncate import _coerce_int
import datetime as _dt
from jiuwenswarm.server.wire_truncate import _sanitize_history_record_for_wire
from jiuwenswarm.server.wire_truncate import _select_history_record_page
from jiuwenswarm.server.runtime.session.session_history import append_history_record
import asyncio
from jiuwenswarm.server.runtime.session.session_history import (
    enqueue_history_request_completion,
)
from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
from jiuwenswarm.server.runtime.session.session_history import history_exists
from gateway.common.mode_matrix import is_team_mode
from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id
from jiuwenswarm.server.runtime.session.session_history import load_history_records
from jiuwenswarm.common.todo_snapshot import load_todo_snapshot_for_frontend
import math
from jiuwenswarm.server.runtime.session.session_history import read_history_cursor_page
from jiuwenswarm.server.runtime.session.session_history import (
    read_member_history_records,
)
from jiuwenswarm.server.runtime.session.session_history import read_team_history_records
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.wire_truncate import split_history_record_for_stream
from jiuwenswarm.server.runtime.session.session_history import wait_for_history_receipt

logger = logging.getLogger(__name__)


def _is_restorable_history_record(record: Any) -> bool:
    """Coarsely filter records that the web history UI cannot use for pagination."""
    if not isinstance(record, dict):
        return False
    role = record.get("role")
    content = record.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    has_media = (
        isinstance(record.get("media_items"), list)
        and bool(record["media_items"])
        or (isinstance(record.get("mediaItems"), list) and bool(record["mediaItems"]))
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


class HistoryManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_history_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        subagent_id = params.get("subagent_id")
        data = host.get_conversation_history(
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_history_append_record(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Append a supplied history record without creating an Agent turn.

        Cron uses this for terminal failures.  Falling through to the normal
        request path would treat the failure text as chat input and can invoke
        the unavailable model a second time, leaving the frontend with no
        durable record when it reloads the execution session.
        """
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = str(params.get("session_id") or request.session_id or "").strip()
        content = params.get("content")
        if not session_id or not is_valid_session_id(session_id) or content is None:
            wire = host._send_error_response(
                ws, request, send_lock, "session_id and content required", "BAD_REQUEST"
            )
            async with send_lock:
                await host.services.send(ws, wire)
            return
        request_id = str(params.get("request_id") or request.request_id or "").strip()
        channel_id = str(params.get("channel_id") or request.channel_id or "").strip()
        role = (
            "assistant"
            if str(params.get("role") or "assistant") == "assistant"
            else "user"
        )
        event_type = str(params.get("event_type") or "").strip() or None
        mode = str(params.get("mode") or "").strip() or None
        try:
            timestamp = float(params.get("timestamp") or request.timestamp or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp <= 0:
            timestamp = _dt.datetime.now().timestamp()
        try:
            await run_history_io(
                append_history_record,
                session_id=session_id,
                request_id=request_id,
                channel_id=channel_id,
                role=role,
                content=content,
                timestamp=timestamp,
                event_type=event_type,
                mode=mode,
            )
            title = str(params.get("title") or "").strip()
            if title:
                try:
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
                except Exception as title_exc:
                    logger.warning(
                        "[AgentWebSocketServer] history.append_record title backfill failed: session_id=%s error=%s",
                        session_id,
                        title_exc,
                    )
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
        except Exception as exc:
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
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_team_history_get(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """返回 team 模式历史记录的分页，避免与 history.get 并发竞争。

        支持可选 member_name 参数：传入时仅返回与该 member 相关的记录
        （p2p 消息 / @all 广播 / teammate 输出）。
        """
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        member_name = params.get("member_name")
        channel_id = request.channel_id or "web"
        if not isinstance(session_id, str) or not session_id.strip():
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=channel_id,
                ok=False,
                payload={"error": "session_id is required"},
            )
            wire = host.services.encode_response(resp, response_id=request.request_id)
            async with send_lock:
                await host.services.send(ws, wire)
            return
        session_id = session_id.strip()
        try:
            if member_name and isinstance(member_name, str) and member_name.strip():
                records = await asyncio.to_thread(
                    read_member_history_records, session_id, str(member_name).strip()
                )
            else:
                records = await asyncio.to_thread(read_team_history_records, session_id)
        except Exception as exc:
            logger.warning(
                "[team.history.get] read failed: session_id=%s error=%s",
                session_id,
                exc,
            )
            records = []
        sanitized_records = [
            _sanitize_history_record_for_wire(record)
            for record in records
            if isinstance(record, dict)
        ]
        total = len(sanitized_records)
        cursor = _coerce_int(
            params.get("cursor", params.get("offset", 0)),
            default=0,
            minimum=0,
            maximum=max(0, total),
        )
        limit = _coerce_int(
            params.get("limit"),
            default=_TEAM_HISTORY_DEFAULT_LIMIT,
            minimum=1,
            maximum=_TEAM_HISTORY_MAX_LIMIT,
        )
        max_bytes = _coerce_int(
            params.get("max_bytes"),
            default=_TEAM_HISTORY_DEFAULT_MAX_BYTES,
            minimum=_TEAM_HISTORY_MIN_MAX_BYTES,
            maximum=_TEAM_HISTORY_MAX_MAX_BYTES,
        )
        page_records, next_cursor = _select_history_record_page(
            sanitized_records,
            cursor=cursor,
            limit=limit,
            max_bytes=max_bytes,
            session_id=session_id,
        )
        logger.debug(
            "[team.history.get] session_id=%s member=%s total=%d cursor=%d returned=%d next_cursor=%d max_bytes=%d",
            session_id,
            str(member_name or ""),
            total,
            cursor,
            len(page_records),
            next_cursor,
            max_bytes,
        )
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=channel_id,
            ok=True,
            payload={
                "records": page_records,
                "session_id": session_id,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor < total,
                "total": total,
            },
        )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_history_get_stream(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        host = self.host
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        cursor_protocol = "cursor" in params
        request_cursor = params.get("cursor")
        subagent_id = params.get("subagent_id")
        try:
            if cursor_protocol:
                if request_cursor is not None and (not isinstance(request_cursor, str)):
                    raise InvalidHistoryCursor(
                        "history cursor must be null or a string"
                    )
                data = await asyncio.to_thread(
                    host.get_conversation_history_cursor,
                    session_id,
                    request_cursor,
                    limit=params.get("limit", _HISTORY_PAGE_SIZE),
                    subagent_id=subagent_id if isinstance(subagent_id, str) else None,
                )
            else:
                data = await asyncio.to_thread(
                    host.get_conversation_history,
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
            wire = host.services.encode_chunk(
                error_chunk, response_id=request.request_id, sequence=0
            )
            async with send_lock:
                await host.services.send(ws, wire)
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
            wire = host.services.encode_chunk(
                err_chunk, response_id=request.request_id, sequence=0
            )
            async with send_lock:
                await host.services.send(ws, wire)
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
                            "subagent_id": str(
                                response_subagent_id or subagent_id or ""
                            ),
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
                    wire = host.services.encode_chunk(
                        chunk, response_id=request.request_id, sequence=sequence
                    )
                    sequence += 1
                    sent_chunk = False
                    async with send_lock:
                        sent_chunk = await host.services.send(ws, wire)
                    if not sent_chunk:
                        logger.warning(
                            "[AgentWebSocketServer] history 流式响应因 chunk 超限而停止: request_id=%s sequence=%s",
                            request.request_id,
                            sequence,
                        )
                        return
        next_seq = sequence
        is_initial_history_batch = (
            cursor_protocol
            and request_cursor is None
            or (not cursor_protocol and page_idx == 1)
        )
        if (
            is_initial_history_batch
            and isinstance(session_id, str)
            and session_id.strip()
        ):
            todos = load_todo_snapshot_for_frontend(
                session_id.strip(), **_todo_snapshot_session_fields(session_id.strip())
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
            wire_todo = host.services.encode_chunk(
                todo_chunk, response_id=request.request_id, sequence=next_seq
            )
            sent_todo = False
            async with send_lock:
                sent_todo = await host.services.send(ws, wire_todo)
            if not sent_todo:
                logger.warning(
                    "[AgentWebSocketServer] history todo.updated snapshot send failed (oversized or replaced): request_id=%s session_id=%s seq=%s todo_count=%s",
                    request.request_id,
                    session_id.strip(),
                    next_seq,
                    len(todos),
                )
            next_seq += 1

        session_usage = None
        if not subagent_id and isinstance(session_id, str) and session_id.strip():
            from jiuwenswarm.server.control.history_service import load_history_session_usage

            session_usage = await asyncio.to_thread(
                load_history_session_usage, session_id.strip()
            )

        done_payload: dict[str, Any] = {
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
        }
        usage_revision = session_usage.get("revision") if isinstance(session_usage, dict) else None
        if isinstance(usage_revision, int) and not isinstance(usage_revision, bool) and usage_revision > 0:
            done_payload["session_usage"] = session_usage
        # Parent history cold-start: advertise on-disk subagent buckets so the
        # frontend can fetch per-subagent transcripts even when parent history
        # never persisted chat.subtask_update roster frames (GitCode #4887).
        # Keep the if-condition to <=3 boolean expressions (G.CTL.03).
        parent_session_id = session_id.strip() if isinstance(session_id, str) else ""
        is_parent_history_request = not response_subagent_id and not subagent_id
        if is_initial_history_batch and is_parent_history_request and parent_session_id:
            try:
                discovered_ids = list_session_subagent_ids(parent_session_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[history.get] list_session_subagent_ids failed: session_id=%s",
                    session_id,
                    exc_info=True,
                )
                discovered_ids = []
            if discovered_ids:
                done_payload["subagent_ids"] = discovered_ids
        done_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=done_payload,
            is_complete=True,
        )
        wire_done = host.services.encode_chunk(
            done_chunk, response_id=request.request_id, sequence=next_seq
        )
        async with send_lock:
            await host.services.send(ws, wire_done)

    @staticmethod
    def get_conversation_history(
        session_id: str, page_idx: int, *, subagent_id: str | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        if not isinstance(page_idx, int) or page_idx <= 0:
            return None
        normalized_session_id = session_id.strip()
        normalized_subagent_id = (
            subagent_id.strip()
            if isinstance(subagent_id, str) and subagent_id.strip()
            else None
        )
        if normalized_subagent_id:
            if not history_exists(
                normalized_session_id, subagent_id=normalized_subagent_id
            ):
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
                normalized_session_id, subagent_id=normalized_subagent_id
            )
        except Exception:
            return None
        if not isinstance(raw, list):
            return None
        page_size = _HISTORY_PAGE_SIZE
        restorable = [item for item in raw if _is_restorable_history_record(item)]
        total = len(restorable)
        total_pages = max(1, math.ceil(total / page_size))
        if page_idx > total_pages:
            return None
        ordered = list(reversed(restorable))
        start = (page_idx - 1) * page_size
        end = start + page_size
        page_messages = list(ordered[start:end])
        logger.debug(
            "[history.get] session_id=%s subagent_id=%s page_idx=%s raw_total=%s restorable_total=%s total_pages=%s returned=%s",
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

    @staticmethod
    def get_conversation_history_cursor(
        session_id: str,
        cursor: str | None,
        *,
        limit: int = _HISTORY_PAGE_SIZE,
        subagent_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidHistoryCursor("session_id is required")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or (not 1 <= limit <= _HISTORY_PAGE_SIZE)
        ):
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
