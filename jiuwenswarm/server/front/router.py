# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Method router: control-plane services vs execution admission."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front.admission import ExecutionAdmission
from jiuwenswarm.server.front.protocol import encode_chunk, encode_response
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.runtime.gateway_adapter.base import build_error_response
from jiuwenswarm.server.ws_send import send_wire_payload

logger = logging.getLogger(__name__)

_CONTROL_METHODS: frozenset[str] | None = None
_CONTROL_SESSION_METHODS: frozenset[str] = frozenset()
_CONTROL_PROJECT_METHODS: frozenset[str] = frozenset()
_CONTROL_CONFIG_METHODS: frozenset[str] = frozenset()
_CONTROL_HISTORY_METHODS: frozenset[str] = frozenset()
_HANDLE_SESSION = None
_HANDLE_PROJECT = None
_HANDLE_CONFIG = None
_HANDLE_HISTORY = None
_LOAD_HISTORY_QUERY = None
_LOAD_HISTORY_TODO_SNAPSHOT = None
_STREAM_HISTORY_RECORDS = None
_INVALID_HISTORY_CURSOR = None
_HISTORY_SNAPSHOT_CHANGED = None


def _load_control_services() -> None:
    """Import Control Services on first control dispatch, not at Front listen."""
    global _CONTROL_METHODS
    global _CONTROL_SESSION_METHODS, _CONTROL_PROJECT_METHODS
    global _CONTROL_CONFIG_METHODS, _CONTROL_HISTORY_METHODS
    global _HANDLE_SESSION, _HANDLE_PROJECT, _HANDLE_CONFIG, _HANDLE_HISTORY
    global _LOAD_HISTORY_QUERY, _LOAD_HISTORY_TODO_SNAPSHOT, _STREAM_HISTORY_RECORDS
    global _INVALID_HISTORY_CURSOR, _HISTORY_SNAPSHOT_CHANGED
    if _CONTROL_METHODS is not None:
        return
    from jiuwenswarm.server.control.config_service import (
        CONTROL_CONFIG_METHODS,
        handle_config_request,
    )
    from jiuwenswarm.server.control.history_service import (
        CONTROL_HISTORY_METHODS,
        HistorySnapshotChanged,
        InvalidHistoryCursor,
        handle_history_request,
        load_history_query,
        load_history_todo_snapshot,
        stream_history_records,
    )
    from jiuwenswarm.server.control.project_service import (
        CONTROL_PROJECT_METHODS,
        handle_project_request,
    )
    from jiuwenswarm.server.control.session_service import (
        CONTROL_SESSION_METHODS,
        handle_session_request,
    )

    _CONTROL_SESSION_METHODS = CONTROL_SESSION_METHODS
    _CONTROL_PROJECT_METHODS = CONTROL_PROJECT_METHODS
    _CONTROL_CONFIG_METHODS = CONTROL_CONFIG_METHODS
    _CONTROL_HISTORY_METHODS = CONTROL_HISTORY_METHODS
    _HANDLE_SESSION = handle_session_request
    _HANDLE_PROJECT = handle_project_request
    _HANDLE_CONFIG = handle_config_request
    _HANDLE_HISTORY = handle_history_request
    _LOAD_HISTORY_QUERY = load_history_query
    _LOAD_HISTORY_TODO_SNAPSHOT = load_history_todo_snapshot
    _STREAM_HISTORY_RECORDS = stream_history_records
    _INVALID_HISTORY_CURSOR = InvalidHistoryCursor
    _HISTORY_SNAPSHOT_CHANGED = HistorySnapshotChanged
    _CONTROL_METHODS = (
        CONTROL_SESSION_METHODS
        | CONTROL_PROJECT_METHODS
        | CONTROL_CONFIG_METHODS
        | CONTROL_HISTORY_METHODS
    )


def _control_methods() -> frozenset[str]:
    _load_control_services()
    methods = _CONTROL_METHODS
    if methods is None:
        raise RuntimeError("control method set is not initialized")
    return methods


def __getattr__(name: str) -> Any:
    if name == "CONTROL_METHODS":
        return _control_methods()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def is_control_method(request: AgentRequest) -> bool:
    method = request.req_method.value if request.req_method is not None else ""
    return method in _control_methods()


class MethodRouter:
    def __init__(self, readiness: Readiness, admission: ExecutionAdmission) -> None:
        self._readiness = readiness
        self._admission = admission

    async def dispatch(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        if is_control_method(request):
            if request.req_method == ReqMethod.HISTORY_GET and request.is_stream:
                await self._dispatch_history_stream(ws, request, send_lock)
                return
            await self._dispatch_control(ws, request, send_lock)
            return
        await self._admission.dispatch(ws, request, send_lock)

    async def _dispatch_control(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        _load_control_services()
        method = request.req_method.value if request.req_method is not None else ""
        try:
            if method in _CONTROL_SESSION_METHODS:
                response = await _HANDLE_SESSION(request)
            elif method in _CONTROL_PROJECT_METHODS:
                response = await _HANDLE_PROJECT(request)
            elif method in _CONTROL_CONFIG_METHODS:
                response = await _HANDLE_CONFIG(request)
            elif method in _CONTROL_HISTORY_METHODS:
                response = await _HANDLE_HISTORY(request)
            else:
                response = build_error_response(
                    request, f"unsupported control method: {method}", code="BAD_REQUEST"
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Front] control dispatch failed: method=%s", method)
            response = build_error_response(request, str(exc), code="INTERNAL_ERROR")
        wire = encode_response(response, response_id=request.request_id)
        async with send_lock:
            await send_wire_payload(ws, wire)

    async def _dispatch_history_stream(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: Any,
    ) -> None:
        _load_control_services()
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
            async with send_lock:
                await send_wire_payload(ws, wire)
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
            async with send_lock:
                await send_wire_payload(ws, wire)
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
                sent = False
                async with send_lock:
                    sent = await send_wire_payload(ws, wire)
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
            sent_todo = False
            async with send_lock:
                sent_todo = await send_wire_payload(ws, wire_todo)
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
        async with send_lock:
            await send_wire_payload(ws, wire)
