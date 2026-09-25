# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Method router: control-plane services vs execution admission."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.health_service import handle_readiness
from jiuwenswarm.server.control.methods import (
    ALL_CONTROL_METHODS,
    CONTROL_CONFIG_METHODS,
    CONTROL_HEALTH_METHODS,
    CONTROL_HISTORY_METHODS,
    CONTROL_PROJECT_METHODS,
    CONTROL_SESSION_METHODS,
)
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.front.event_forwarder import EventForwarder
from jiuwenswarm.server.front.protocol import encode_chunk, encode_response
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.control.responses import build_error_response
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)

CONTROL_METHODS = ALL_CONTROL_METHODS

_HANDLE_SESSION = None
_HANDLE_PROJECT = None
_HANDLE_CONFIG = None
_HANDLE_HISTORY = None
_LOAD_HISTORY_QUERY = None
_LOAD_HISTORY_TODO_SNAPSHOT = None
_STREAM_HISTORY_RECORDS = None
_INVALID_HISTORY_CURSOR = None
_HISTORY_SNAPSHOT_CHANGED = None


def _load_session_service() -> None:
    global _HANDLE_SESSION
    if _HANDLE_SESSION is not None:
        return
    from jiuwenswarm.server.control.session_service import handle_session_request

    _HANDLE_SESSION = handle_session_request


def _load_project_service() -> None:
    global _HANDLE_PROJECT
    if _HANDLE_PROJECT is not None:
        return
    from jiuwenswarm.server.control.project_service import handle_project_request

    _HANDLE_PROJECT = handle_project_request


def _load_config_service() -> None:
    global _HANDLE_CONFIG
    if _HANDLE_CONFIG is not None:
        return
    from jiuwenswarm.server.control.config_service import handle_config_request

    _HANDLE_CONFIG = handle_config_request


def _load_history_service() -> None:
    global _HANDLE_HISTORY, _LOAD_HISTORY_QUERY, _LOAD_HISTORY_TODO_SNAPSHOT
    global _STREAM_HISTORY_RECORDS, _INVALID_HISTORY_CURSOR, _HISTORY_SNAPSHOT_CHANGED
    if _HANDLE_HISTORY is not None:
        return
    from jiuwenswarm.server.control.history_service import (
        HistorySnapshotChanged,
        InvalidHistoryCursor,
        handle_history_request,
        load_history_query,
        load_history_todo_snapshot,
        stream_history_records,
    )

    _HANDLE_HISTORY = handle_history_request
    _LOAD_HISTORY_QUERY = load_history_query
    _LOAD_HISTORY_TODO_SNAPSHOT = load_history_todo_snapshot
    _STREAM_HISTORY_RECORDS = stream_history_records
    _INVALID_HISTORY_CURSOR = InvalidHistoryCursor
    _HISTORY_SNAPSHOT_CHANGED = HistorySnapshotChanged


def _load_control_services() -> None:
    """Load all Control Service handlers. Prefer per-type loaders at dispatch."""
    _load_session_service()
    _load_project_service()
    _load_config_service()
    _load_history_service()


def is_control_method(request: AgentRequest) -> bool:
    method = request.req_method.value if request.req_method is not None else ""
    return method in ALL_CONTROL_METHODS


class MethodRouter:
    def __init__(
        self,
        readiness: Readiness,
        admission: ExecutionAdmission,
        *,
        forwarder: EventForwarder | None = None,
    ) -> None:
        self._readiness = readiness
        self._admission = admission
        self._forwarder = forwarder

    async def dispatch(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        if is_control_method(request):
            if request.req_method == ReqMethod.HEALTH_CHECK_GET_CONF:
                await self._dispatch_health(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_GET and request.is_stream:
                await self._dispatch_history_stream(ws, request, send_lock)
                return
            await self._dispatch_control(ws, request, send_lock)
            return
        await self._admission.dispatch(ws, request, send_lock)

    async def _dispatch_health(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        response = handle_readiness(request, self._readiness)
        wire = encode_response(response, response_id=request.request_id)
        await self._send(ws, send_lock, wire)

    async def _dispatch_control(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        method = request.req_method.value if request.req_method is not None else ""
        try:
            if method in CONTROL_SESSION_METHODS:
                _load_session_service()
                response = await _HANDLE_SESSION(request)
            elif method in CONTROL_PROJECT_METHODS:
                _load_project_service()
                response = await _HANDLE_PROJECT(request)
            elif method in CONTROL_CONFIG_METHODS:
                _load_config_service()
                response = await _HANDLE_CONFIG(request)
            elif method in CONTROL_HISTORY_METHODS:
                _load_history_service()
                response = await _HANDLE_HISTORY(request)
            elif method in CONTROL_HEALTH_METHODS:
                response = handle_readiness(request, self._readiness)
            else:
                response = build_error_response(
                    request, f"unsupported control method: {method}", code="BAD_REQUEST"
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Front] control dispatch failed: method=%s", method)
            response = build_error_response(request, str(exc), code="INTERNAL_ERROR")
        wire = encode_response(response, response_id=request.request_id)
        await self._send(ws, send_lock, wire)

    async def _dispatch_history_stream(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        _load_history_service()
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        cursor_protocol = "cursor" in params
        request_cursor = params.get("cursor")
        subagent_id = params.get("subagent_id")
        try:
            data = await asyncio.to_thread(_LOAD_HISTORY_QUERY, params)
        except (_INVALID_HISTORY_CURSOR, _HISTORY_SNAPSHOT_CHANGED) as exc:
            error_code = (
                "HISTORY_SNAPSHOT_CHANGED"
                if isinstance(exc, _HISTORY_SNAPSHOT_CHANGED)
                else "INVALID_HISTORY_CURSOR"
            )
            err = AgentResponseChunk(
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
            wire = encode_chunk(err, response_id=request.request_id, sequence=0)
            await self._send(ws, send_lock, wire)
            return
        if data is None:
            err = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "invalid page_idx or session history not found",
                },
                is_complete=True,
            )
            wire = encode_chunk(err, response_id=request.request_id, sequence=0)
            await self._send(ws, send_lock, wire)
            return
        messages = data.get("messages", [])
        total_pages = data.get("total_pages")
        page = data.get("page_idx")
        next_cursor = data.get("next_cursor")
        has_more = data.get("has_more")
        snapshot_id = data.get("snapshot_id")
        snapshot_end = data.get("snapshot_end")
        response_subagent_id = data.get("subagent_id")
        use_split = request.channel_id == "web"
        sequence = 0
        if isinstance(messages, list):
            for chunk_record in _STREAM_HISTORY_RECORDS(messages, use_split=use_split):
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
                wire = encode_chunk(
                    chunk, response_id=request.request_id, sequence=sequence
                )
                sequence += 1
                sent = await self._send(ws, send_lock, wire)
                if not sent:
                    logger.warning(
                        "[Front] history stream stopped after oversized chunk: "
                        "request_id=%s sequence=%s",
                        request.request_id,
                        sequence,
                    )
                    return
        next_seq = sequence
        is_initial_history_batch = (
            cursor_protocol and request_cursor is None
        ) or (not cursor_protocol and page_idx == 1)
        if is_initial_history_batch and isinstance(session_id, str) and session_id.strip():
            todos = await asyncio.to_thread(
                _LOAD_HISTORY_TODO_SNAPSHOT, session_id.strip()
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
            wire_todo = encode_chunk(
                todo_chunk, response_id=request.request_id, sequence=next_seq
            )
            sent_todo = await self._send(ws, send_lock, wire_todo)
            if not sent_todo:
                logger.warning(
                    "[Front] history todo.updated snapshot send failed: "
                    "request_id=%s session_id=%s seq=%s todo_count=%s",
                    request.request_id,
                    session_id.strip(),
                    next_seq,
                    len(todos),
                )
            next_seq += 1
        done = AgentResponseChunk(
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
        wire = encode_chunk(done, response_id=request.request_id, sequence=next_seq)
        await self._send(ws, send_lock, wire)

    async def _send(self, ws: Any, send_lock: Any, payload: dict[str, Any]) -> bool:
        if self._forwarder is not None:
            return await self._forwarder.send(payload, ws=ws, send_lock=send_lock)
        async with send_lock:
            return await send_wire_payload(ws, payload)
