# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.cancel / chat.interrupt 的 intent 契约。

WS dispatch 与中立适配层 handler 共用这一份白名单，保证同一协议字段在
所有通道（WebSocket / HTTP / Gateway）上的校验语义一致。
"""

from __future__ import annotations

from dataclasses import replace as dataclasses_replace
from typing import Any

# chat.cancel（chat.interrupt）合法 intent 集合：
#   cancel/supplement/stop → 终止 session 流式任务；pause/resume → 任务保持运行
# "stop" 是 cancel 的等价别名：runtime/service.py 的 Team 分支按
# intent in {"cancel", "stop"} 处理，入口不能比下游更严，否则 stop 语义失效。
# 未知值直接拒绝，不做任何会话/任务操作。历史教训：适配层曾发送
# "user_cancel"，旧版静默只做应答记账，停止失效且联调期无从发现。
KNOWN_INTERRUPT_INTENTS = ("cancel", "supplement", "pause", "resume", "stop")

_INTERRUPT_INTENT_ALIASES = {"stop": "cancel"}


def normalize_interrupt_intent(params: Any) -> str:
    """从请求参数取出 intent；缺省、null、空串按既有惯例回落 "cancel"。

    与 runtime/service.py 的 str(... or "cancel").strip().lower() 保持一致，
    并把等价别名（stop）归一为规范取值，下游只看到规范集合。
    """
    if not isinstance(params, dict):
        return "cancel"
    intent = str(params.get("intent") or "cancel").strip().lower()
    return _INTERRUPT_INTENT_ALIASES.get(intent, intent)


def unknown_interrupt_intent_error(intent: str) -> str | None:
    """校验 intent 合法性：合法返回 None，未知值返回错误消息。"""
    if intent in KNOWN_INTERRUPT_INTENTS:
        return None
    return (
        f"unknown interrupt intent: {intent!r} "
        f"(expected one of: {', '.join(KNOWN_INTERRUPT_INTENTS)})"
    )


def reject_interrupt_result_payload(intent: str, error: str) -> dict:
    """未知 intent 的拒绝应答 payload。

    沿用既有 chat.interrupt_result 字段契约（interface_deep.py /
    external_harness_helpers.py 的 intent+success+message），另附 error
    便于适配层按原因排查。
    """
    return {
        "event_type": "chat.interrupt_result",
        "intent": intent,
        "success": False,
        "message": error,
        "error": error,
    }


def normalized_intent_request(request):
    """归一化 CHAT_CANCEL 的 intent 并回写 request.params。

    保证入口判定与下游（runtime / 定向 expert / organization）读到的
    intent 一致（大小写、空白、stop 别名）。request 是 AgentRequest；
    params 非 dict 时不回写，原样返回。
    """
    intent = normalize_interrupt_intent(request.params)
    params = request.params
    if isinstance(params, dict) and params.get("intent") != intent:
        request = dataclasses_replace(request, params={**params, "intent": intent})
    return request, intent
