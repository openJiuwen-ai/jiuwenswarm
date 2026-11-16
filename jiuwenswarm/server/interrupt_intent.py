# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.cancel / chat.interrupt 的 intent 契约。

WS dispatch 与中立适配层 handler 共用这一份白名单，保证同一协议字段在
所有通道（WebSocket / HTTP / Gateway）上的校验语义一致。
"""

from __future__ import annotations

from typing import Any

# chat.cancel（chat.interrupt）合法 intent 集合：
#   cancel/supplement → 终止 session 流式任务；pause/resume → 任务保持运行
# 未知值直接拒绝，不做任何会话/任务操作。历史教训：适配层曾发送
# "user_cancel"，旧版静默只做应答记账，停止失效且联调期无从发现。
KNOWN_INTERRUPT_INTENTS = ("cancel", "supplement", "pause", "resume")


def normalize_interrupt_intent(params: Any) -> str:
    """从请求参数取出 intent；缺省、null、空串按既有惯例回落 "cancel"。"""
    if not isinstance(params, dict):
        return "cancel"
    return str(params.get("intent") or "cancel")


def unknown_interrupt_intent_error(intent: str) -> str | None:
    """校验 intent 合法性：合法返回 None，未知值返回错误消息。"""
    if intent in KNOWN_INTERRUPT_INTENTS:
        return None
    return (
        f"unknown interrupt intent: {intent!r} "
        f"(expected one of: {', '.join(KNOWN_INTERRUPT_INTENTS)})"
    )