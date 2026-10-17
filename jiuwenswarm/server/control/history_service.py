# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""History query Control Service. Reads session history files only."""

from __future__ import annotations

import math
from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.control.responses import build_error_response
from jiuwenswarm.server.runtime.session.session_history import (
    HistorySnapshotChanged,
    InvalidHistoryCursor,
    history_exists,
    load_history_records,
    read_history_cursor_page,
)
from jiuwenswarm.server.wire_truncate import (
    _HISTORY_PAGE_SIZE,
    _HISTORY_RESTORABLE_ASSISTANT_EVENT_TYPES,
    _sanitize_history_record_for_wire,
    split_history_record_for_stream,
)


def _is_restorable_history_record(record: Any) -> bool:
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


def load_history_page(
    session_id: str,
    page_idx: int,
    *,
    subagent_id: str | None = None,
) -> dict[str, Any] | None:
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
    restorable = [item for item in raw if _is_restorable_history_record(item)]
    total = len(restorable)
    total_pages = max(1, math.ceil(total / _HISTORY_PAGE_SIZE))
    if page_idx > total_pages:
        return None
    ordered = list(reversed(restorable))
    start = (page_idx - 1) * _HISTORY_PAGE_SIZE
    end = start + _HISTORY_PAGE_SIZE
    result: dict[str, Any] = {
        "messages": list(ordered[start:end]),
        "total_pages": total_pages,
        "page_idx": page_idx,
    }
    if normalized_subagent_id:
        result["subagent_id"] = normalized_subagent_id
    return result


def load_history_cursor_page(
    session_id: str,
    cursor: str | None,
    *,
    limit: int = _HISTORY_PAGE_SIZE,
    subagent_id: str | None = None,
) -> dict[str, Any]:
    """Newest-to-oldest JSONL batch used by the Web restore protocol."""
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


def load_history_todo_snapshot(session_id: str) -> list[dict[str, Any]]:
    """Workspace todo.json snapshot for the first history restore batch."""
    from jiuwenswarm.common.todo_snapshot import load_todo_snapshot_for_frontend
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    try:
        metadata = get_session_metadata(session_id, enable_writeback=False) or {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    project_dir = str(metadata.get("project_dir") or "").strip() or None
    work_mode = metadata.get("work_mode")
    mode = metadata.get("mode")
    return load_todo_snapshot_for_frontend(
        session_id,
        project_dir=project_dir,
        work_mode=work_mode if isinstance(work_mode, str) else None,
        mode=mode if isinstance(mode, str) else None,
    )


def sanitize_history_messages(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        _sanitize_history_record_for_wire(record)
        for record in messages
        if isinstance(record, dict)
    ]


def stream_history_records(messages: list[Any], *, use_split: bool) -> list[Any]:
    chunks: list[Any] = []
    for item in messages:
        if use_split:
            chunks.extend(split_history_record_for_stream(item))
        else:
            chunks.append(_sanitize_history_record_for_wire(item))
    return chunks


def _normalized_subagent_id(params: dict[str, Any]) -> str | None:
    subagent_id = params.get("subagent_id")
    return subagent_id if isinstance(subagent_id, str) else None


def load_history_query(params: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve ``history.get`` params for page_idx or cursor/limit.

    Raises ``InvalidHistoryCursor`` / ``HistorySnapshotChanged`` for the
    cursor protocol. Returns ``None`` when page_idx paging cannot locate
    a page (same contract as Runtime ``get_conversation_history``).
    """
    session_id = params.get("session_id")
    subagent_id = _normalized_subagent_id(params)
    if "cursor" in params:
        request_cursor = params.get("cursor")
        if request_cursor is not None and not isinstance(request_cursor, str):
            raise InvalidHistoryCursor("history cursor must be null or a string")
        return load_history_cursor_page(
            str(session_id or ""),
            request_cursor,
            limit=params.get("limit", _HISTORY_PAGE_SIZE),
            subagent_id=subagent_id,
        )
    page_idx = params.get("page_idx")
    return load_history_page(
        session_id=str(session_id or ""),
        page_idx=page_idx if isinstance(page_idx, int) else -1,
        subagent_id=subagent_id,
    )


async def handle_history_request(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    try:
        data = load_history_query(params)
    except (InvalidHistoryCursor, HistorySnapshotChanged) as exc:
        error_code = (
            "HISTORY_SNAPSHOT_CHANGED"
            if isinstance(exc, HistorySnapshotChanged)
            else "INVALID_HISTORY_CURSOR"
        )
        return build_error_response(request, str(exc), code=error_code)
    if data is None:
        return build_error_response(
            request,
            "invalid page_idx or session history not found",
            code="NOT_FOUND",
        )
    messages = data.get("messages")
    if isinstance(messages, list):
        data = dict(data)
        data["messages"] = sanitize_history_messages(messages)
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=data,
        metadata=request.metadata,
    )
