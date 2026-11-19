# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""聊天请求进业务函数之前的三步。

这三步不回包。WebSocket 的分发函数会调用，以后 HTTP 的聊天路由也调用同一份。
顺序是：先让扩展看一眼输入，再抄已有的团队名字，再记隐式反馈。
不是聊天请求时，调用了也会直接返回。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_gateway.common.mode_matrix import is_team_mode
from jiuwenswarm.common.hook_event import AgentServerHookEvents
from jiuwenswarm.common.hooks_context import AgentServerChatHookContext
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import CHAT_TURN_METHODS

logger = logging.getLogger(__name__)


def request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


def should_trigger_before_chat_request(request: AgentRequest) -> bool:
    return request.req_method in CHAT_TURN_METHODS


async def trigger_before_chat_request(request: AgentRequest) -> None:
    """让扩展插件先看一眼聊天输入。插件可以改参数，这里不回包。"""
    if not should_trigger_before_chat_request(request):
        return
    from agent_gateway.extensions.registry import ExtensionRegistry

    params = request.params if isinstance(request.params, dict) else {}
    if not isinstance(request.params, dict):
        request.params = params

    ctx = AgentServerChatHookContext(
        request_id=request.request_id,
        channel_id=request.channel_id,
        session_id=request.session_id,
        req_method=request.req_method.value if request.req_method is not None else None,
        params=params,
    )

    await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)


async def ensure_auto_team_binding(request: AgentRequest) -> Any | None:
    """Forward an existing team binding without creating one from the query."""
    if request.req_method != ReqMethod.CHAT_SEND:
        return None

    params = request.params if isinstance(request.params, dict) else {}
    if not isinstance(request.params, dict):
        request.params = params
    session_id = str(request.session_id or params.get("session_id") or "").strip()
    if not session_id:
        return None

    from jiuwenswarm.runtime.request import resolve_agent_request_mode
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    metadata = get_session_metadata(session_id, cache_bust=True)
    raw_mode = params.get("mode")
    effective_mode = (
        raw_mode
        if isinstance(raw_mode, str) and raw_mode.strip()
        else metadata.get("mode")
    )
    _, _, canonical_mode = resolve_agent_request_mode(effective_mode)
    if not is_team_mode(canonical_mode):
        return None

    existing_team_name = str(metadata.get("team_name") or "").strip()
    if existing_team_name:
        params.setdefault("team_name", existing_team_name)
        template_id = str(metadata.get("team_template_id") or "").strip()
        if template_id:
            params.setdefault("team_template_id", template_id)
        return existing_team_name
    return None


async def try_record_implicit_feedback(request: AgentRequest) -> None:
    """No-op: proactive recommendation feedback path removed."""
    _ = request
