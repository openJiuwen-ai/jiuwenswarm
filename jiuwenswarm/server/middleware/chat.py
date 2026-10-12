# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""聊天请求进业务函数之前的三步。

这三步不回包。WebSocket 的分发函数会调用，以后 HTTP 的聊天路由也调用同一份。
顺序是：先让扩展看一眼输入，再抄已有的团队名字，再记隐式反馈。
不是聊天请求时，调用了也会直接返回。
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import AgentServerChatHookContext

logger = logging.getLogger(__name__)

_CHAT_HOOK_METHODS = (
    ReqMethod.CHAT_SEND,
    ReqMethod.CHAT_RESUME,
    ReqMethod.CHAT_ANSWER,
)


def request_query_text(request: AgentRequest) -> str:
    """Return text chat query only; structured events are handled downstream."""
    if not isinstance(request.params, dict):
        return ""
    query = request.params.get("query")
    if not isinstance(query, str):
        return ""
    return query.strip()


def should_trigger_before_chat_request(request: AgentRequest) -> bool:
    return request.req_method in _CHAT_HOOK_METHODS


async def trigger_before_chat_request(request: AgentRequest) -> None:
    """让扩展插件先看一眼聊天输入。插件可以改参数，这里不回包。"""
    if not should_trigger_before_chat_request(request):
        return
    from jiuwenswarm.extensions.registry import ExtensionRegistry

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
    """Best-effort 采集隐式反馈：用户在收到推荐后的文本回复。

    主动推荐送达后，用户若直接用文本回复（"简洁点""不需要"…）而不是点卡片
    上的赞/踩按钮，这条回复就是隐式反馈。把它关联到该会话最近一条推荐，
    交由 ``record_implicit_feedback`` 做情感分类后入 buffer，供下次 tick 梯度更新。

    与 Runtime activity participant 同款 best-effort：任何异常只 log debug，
    绝不阻断主 chat 流。只在 ``chat.send`` 且来源不是 proactive 自己触发的
    推荐指令时才介入（``source=proactive_recommendation`` 是系统主动塞给主
    agent 的指令，不是用户说的话，见 proactive_adapter 触发处）。
    """
    if request.req_method != ReqMethod.CHAT_SEND:
        return
    try:
        params = request.params if isinstance(request.params, dict) else {}
        # proactive 自己触发主 agent 的指令带 source=proactive_recommendation，
        # 那不是用户输入，跳过
        if str(params.get("source") or "").strip() == "proactive_recommendation":
            return
        session_id = str(request.session_id or params.get("session_id") or "").strip()
        if not session_id:
            return
        query = request_query_text(request)
        if not query:
            return

        from jiuwenswarm.agents.harness.common.recommendation.feedback_collector import (
            find_latest_recommendation,
            record_implicit_feedback,
        )

        # max_age_seconds=0 砍掉时间窗：不靠时间硬挡"无关反馈"——是否相关、是否
        # 产生梯度交给梯度更新器的模型判断（看 rec_content + user_reply 语义）。
        # 配合 record_feedback 的"同 rec_id 只采紧跟第一条、后续不覆盖"逻辑，
        # 每条推荐只关联它之后紧跟的第一条用户回复。
        latest = find_latest_recommendation(session_id, max_age_seconds=0)
        if latest is None:
            return
        rec_id = latest.get("id")
        if not rec_id:
            return

        # 已对该 rec_id 给过显式反馈（赞/踩）的话，record_feedback 的去重逻辑
        # 会自动丢弃隐式补充——无需在此预判。
        record_implicit_feedback(rec_id, query)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[AgentWebSocketServer] implicit feedback hook failed; preserving chat: "
            "session_id=%s error=%s",
            request.session_id,
            exc,
        )
