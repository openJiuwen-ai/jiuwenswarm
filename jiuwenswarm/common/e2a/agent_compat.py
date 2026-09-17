# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer：E2AEnvelope → 现有 AgentRequest（第一阶段）；不得与 normalize_failed 兜底同时使用。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from jiuwenswarm.common.e2a.gateway_normalize import E2A_INTERNAL_CONTEXT_KEY
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)


def _e2a_timestamp_to_float(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def e2a_to_agent_request(env: E2AEnvelope) -> AgentRequest:
    """
    将规范化成功的 E2A 转为 AgentRequest。

    若 envelope 含 Gateway 兜底标记，须由调用方先分支处理 legacy，勿调用本函数。
    """
    ctx = dict(env.channel_context or {})
    internal = ctx.pop(E2A_INTERNAL_CONTEXT_KEY, None)
    if isinstance(internal, dict) and internal.get("normalize_failed"):
        raise RuntimeError("e2a_to_agent_request called on fallback envelope; use legacy path")

    metadata = ctx if ctx else None
    method_str = env.method
    req_method: ReqMethod | None = None
    if method_str:
        try:
            req_method = ReqMethod(method_str)
        except ValueError:
            logger.error(
                "[E2A][compat] unknown E2A method=%r request_id=%s",
                method_str,
                env.request_id,
            )
            raise

    return AgentRequest(
        request_id=env.request_id or "",
        channel_id=env.channel or "web",
        session_id=env.session_id,
        chat_id=env.chat_id,
        req_method=req_method,
        params=dict(env.params or {}),
        is_stream=bool(env.is_stream),
        timestamp=_e2a_timestamp_to_float(env.timestamp),
        metadata=metadata,
        # V2: 透传 agent_ref 到 AgentServer，供响应侧 chunk/response 回带（设计 §6.3）。
        agent_ref=env.agent_ref,
    )
