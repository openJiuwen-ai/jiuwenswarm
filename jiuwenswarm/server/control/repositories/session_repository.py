# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session metadata store used by Front Control.

Reads and writes session files through ``session_metadata`` / history.
Does not create, switch, or delete sessions — those stay on the execution plane.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.methods import CONTROL_SESSION_METHODS
from jiuwenswarm.server.control.responses import (
    build_error_response,
    parse_int_param,
)
from jiuwenswarm.server.runtime.session.session_history import load_history_records
from jiuwenswarm.server.runtime.session.session_info import to_session_info
from jiuwenswarm.server.runtime.session.session_metadata import (
    _read_metadata,
    _write_metadata_sync,
    get_all_sessions_metadata,
    get_session_metadata,
    set_session_pinned,
)

logger = logging.getLogger(__name__)

_SESSION_LIST_LIMIT_DEFAULT: Final[int] = 20
_SESSION_LIST_LIMIT_MAX: Final[int] = 200
_VALID_ACCENT_COLORS: Final[frozenset[str]] = frozenset(
    {"default", "blue", "green", "pink", "purple", "red", "yellow"}
)
_PREVIEW_CHAT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"chat.final", "team.message"}
)
_PREVIEW_COUNT_DEFAULT: Final[int] = 30
_PREVIEW_COUNT_MAX: Final[int] = 100


def _is_previewable(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    content = item.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    if role == "user":
        return has_content
    return item.get("event_type") in _PREVIEW_CHAT_EVENT_TYPES and has_content


def _build_preview_messages(raw: object, count: int) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if not isinstance(raw, list):
        return messages
    previewable = [item for item in raw if _is_previewable(item)]
    for msg in previewable[-count:]:
        messages.append(
            {
                "role": msg.get("role", "unknown"),
                "content": msg.get("content", "") if isinstance(msg.get("content"), str) else "",
                "event_type": msg.get("event_type", ""),
            }
        )
    return messages


def _ok(request: AgentRequest, payload: dict) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


async def handle_list(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    limit = _SESSION_LIST_LIMIT_DEFAULT
    offset = 0
    try:
        limit = parse_int_param(
            params,
            "limit",
            _SESSION_LIST_LIMIT_DEFAULT,
            minimum=1,
            maximum=_SESSION_LIST_LIMIT_MAX,
        )
        offset = parse_int_param(params, "offset", 0, minimum=0, maximum=10**9)
        sessions, total = get_all_sessions_metadata(limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_repository] session.list failed: %s", exc)
        channel_id = str(request.channel_id or "").strip().lower()
        if channel_id == "tui":
            return _ok(
                request,
                {"sessions": [], "total": 0, "limit": limit, "offset": offset},
            )
        return build_error_response(request, str(exc), code="INTERNAL_ERROR")
    channel_id = str(request.channel_id or "").strip().lower()
    session_infos = sessions if channel_id == "tui" else [to_session_info(item) for item in sessions]
    return _ok(
        request,
        {"sessions": session_infos, "total": total, "limit": limit, "offset": offset},
    )


async def handle_get_metadata(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    sid = params.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return build_error_response(request, "session_id is required", code="BAD_REQUEST")
    sid = sid.strip()
    try:
        meta = get_session_metadata(sid, cache_bust=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_repository] session.get_metadata failed: %s", exc)
        return build_error_response(request, str(exc), code="INTERNAL_ERROR")
    if not meta:
        return build_error_response(request, "session not found", code="NOT_FOUND")
    return _ok(request, meta)


async def handle_pin(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    sid = params.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        return build_error_response(request, "session_id is required", code="BAD_REQUEST")
    sid = sid.strip()
    raw_pinned = params.get("pinned")
    if not isinstance(raw_pinned, bool):
        return build_error_response(request, "pinned must be boolean", code="BAD_REQUEST")
    try:
        result = await asyncio.to_thread(set_session_pinned, sid, raw_pinned)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_repository] session.pin failed: %s", exc)
        return build_error_response(request, str(exc), code="INTERNAL_ERROR")
    if result is None:
        return build_error_response(request, "session not found", code="NOT_FOUND")
    new_pinned, new_order = result
    return _ok(request, {"pinned": new_pinned, "pin_order": new_order})


async def handle_color_set(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    target = str(params.get("session_id") or request.session_id or "").strip()
    if not target:
        return build_error_response(request, "session_id is required", code="BAD_REQUEST")
    color = params.get("color")
    if color is None:
        try:
            metadata = get_session_metadata(target, cache_bust=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[session_repository] session.color_set query failed: %s", exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        accent_color = metadata.get("accent_color", "default") if metadata else "default"
        return _ok(request, {"session_id": target, "accent_color": accent_color})
    if str(color) not in _VALID_ACCENT_COLORS:
        return build_error_response(request, f"invalid color: {color}", code="BAD_REQUEST")
    try:
        metadata = _read_metadata(target)
        metadata["accent_color"] = str(color)
        _write_metadata_sync(target, metadata)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_repository] session.color_set failed: %s", exc)
        return build_error_response(request, str(exc), code="INTERNAL_ERROR")
    return _ok(request, {"session_id": target, "accent_color": str(color)})


async def handle_preview(request: AgentRequest) -> AgentResponse:
    params = request.params if isinstance(request.params, dict) else {}
    target = str(params.get("session_id") or request.session_id or "").strip()
    if not target:
        return build_error_response(request, "session_id is required", code="BAD_REQUEST")
    preview_count = parse_int_param(
        params,
        "count",
        _PREVIEW_COUNT_DEFAULT,
        minimum=1,
        maximum=_PREVIEW_COUNT_MAX,
    )
    try:
        raw = load_history_records(target)
        preview_messages = _build_preview_messages(raw, preview_count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_repository] session.preview read failed: %s", exc)
        preview_messages = []
    return _ok(request, {"session_id": target, "preview_messages": preview_messages})


async def handle_session_request(request: AgentRequest) -> AgentResponse:
    method = request.req_method.value if request.req_method is not None else ""
    if method not in CONTROL_SESSION_METHODS:
        return build_error_response(
            request, f"unsupported control session method: {method}", code="BAD_REQUEST"
        )
    if request.req_method != ReqMethod.SESSION_LIST:
        from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError, guard

        params = request.params if isinstance(request.params, dict) else {}
        try:
            guard(str(params.get("session_id") or request.session_id or ""))
        except LifecycleError as exc:
            return build_error_response(request, str(exc), code=exc.code)
    if request.req_method == ReqMethod.SESSION_GET_METADATA:
        return await handle_get_metadata(request)
    if request.req_method == ReqMethod.SESSION_PIN:
        return await handle_pin(request)
    if request.req_method == ReqMethod.SESSION_COLOR_SET:
        return await handle_color_set(request)
    if request.req_method == ReqMethod.SESSION_PREVIEW:
        return await handle_preview(request)
    return await handle_list(request)
