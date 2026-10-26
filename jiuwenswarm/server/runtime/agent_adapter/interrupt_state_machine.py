# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HITL 中断状态机：显式建模中断生命周期与终态。

背景：cancel/supplement 之前只终止 round（流转发任务），不清理持久化中断
状态（``INTERRUPTION_KEY`` 残留、resume_ctx 残留），导致下一条纯文本被当
resume 输入 → re-interrupt → 死循环；任务结束后残留卡片可点击且行为不可
预期（"点一张生一张"的反馈循环）。

本模块给每个 session 的 HITL 中断一个显式相位：

::

    idle ──(卡片发出)──► paused ──(应答受理)──► resumed ──┐
                          │  │                            │
                          │  └──(cancel/supplement)──► 终态 │(重放中再次中断)
                          │                            │    │
                          └────────────────────────────┴────┘
                                       │
                     cancelled / supplemented（终态，可被新卡片重开）

终态语义：

- 进入 ``cancelled`` / ``supplemented`` 后，同 session 的旧卡片应答一律拒绝
  （guard_stale_interrupt_response），不会进入 runtime 重放；
- 新的中断（新卡片发出）会把相位重开为 ``paused``——终态只表示"上一代中断
  已死"，不阻断新一代；
- 非法转换只记日志不抛错（fail-open）：状态机是守卫，不是运行时依赖。

所有读写接受 ``SessionFlagProxy``（鸭子类型），由调用方保证落盘态与内存态
双写。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

INTERRUPT_PHASE_SESSION_KEY = "jiuwenclaw_interrupt_phase"

PHASE_IDLE = "idle"
PHASE_PAUSED = "paused"
PHASE_RESUMED = "resumed"
PHASE_CANCELLED = "cancelled"
PHASE_SUPPLEMENTED = "supplemented"

TERMINAL_PHASES = frozenset({PHASE_CANCELLED, PHASE_SUPPLEMENTED})

_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    PHASE_IDLE: frozenset({PHASE_PAUSED}),
    PHASE_PAUSED: frozenset({PHASE_RESUMED, PHASE_CANCELLED, PHASE_SUPPLEMENTED}),
    PHASE_RESUMED: frozenset({
        PHASE_PAUSED,  # 重放中再次中断（新卡片）
        PHASE_IDLE,  # 轮次正常完成，回到空闲
        PHASE_CANCELLED,
        PHASE_SUPPLEMENTED,
    }),
    # 终态可被新卡片重开（新中断属于新一代，不算复活旧中断）
    PHASE_CANCELLED: frozenset({PHASE_PAUSED, PHASE_IDLE}),
    PHASE_SUPPLEMENTED: frozenset({PHASE_PAUSED, PHASE_IDLE}),
}


def get_interrupt_phase(session: Any) -> dict[str, Any] | None:
    """读取当前中断相位记录；无记录返回 None。"""
    if session is None:
        return None
    try:
        value = session.get_state(INTERRUPT_PHASE_SESSION_KEY)
    except Exception:  # noqa: BLE001 — 守卫不因读取失败阻断主流程
        logger.warning("[InterruptStateMachine] get phase failed", exc_info=True)
        return None
    if isinstance(value, dict) and value.get("phase"):
        return value
    return None


def is_interrupt_terminal(session: Any) -> bool:
    """当前相位是否处于终态（cancelled / supplemented）。"""
    phase = get_interrupt_phase(session)
    return bool(phase) and phase.get("phase") in TERMINAL_PHASES


def _transition_phase(
    session: Any,
    phase: str,
    *,
    card_id: str = "",
    source: str = "",
    reason: str = "",
) -> None:
    """写入新相位（fail-open：非法转换记日志但仍写入）。"""
    if session is None:
        return
    current = get_interrupt_phase(session)
    current_phase = current.get("phase") if current else PHASE_IDLE
    if phase not in _VALID_TRANSITIONS.get(current_phase, frozenset()):
        logger.warning(
            "[InterruptStateMachine] unexpected transition %s -> %s "
            "(card_id=%s reason=%s) — applying anyway (fail-open)",
            current_phase,
            phase,
            card_id,
            reason,
        )
    record: dict[str, Any] = {
        "phase": phase,
        "ts": time.time(),
        "prev": current_phase,
    }
    if card_id:
        record["card_id"] = card_id
    if source:
        record["source"] = source
    if reason:
        record["reason"] = reason
    try:
        session.update_state({INTERRUPT_PHASE_SESSION_KEY: record})
    except Exception:  # noqa: BLE001
        logger.warning(
            "[InterruptStateMachine] write phase %s failed (card_id=%s)",
            phase,
            card_id,
            exc_info=True,
        )


def mark_interrupt_paused(session: Any, *, card_id: str = "", source: str = "") -> None:
    """卡片发出（或重放中再次中断）：相位 → paused。"""
    _transition_phase(session, PHASE_PAUSED, card_id=card_id, source=source)


def mark_interrupt_resumed(session: Any, *, card_id: str = "", source: str = "") -> None:
    """应答已受理并进入重放：相位 → resumed。"""
    _transition_phase(session, PHASE_RESUMED, card_id=card_id, source=source)


def mark_interrupt_idle(session: Any) -> None:
    """轮次正常完成：相位 → idle。"""
    _transition_phase(session, PHASE_IDLE)


def mark_interrupt_cancelled(session: Any, *, reason: str = "") -> None:
    """用户取消：相位 → cancelled（终态）。"""
    _transition_phase(session, PHASE_CANCELLED, reason=reason)


def mark_interrupt_supplemented(session: Any, *, reason: str = "") -> None:
    """用户补充/切换任务：相位 → supplemented（终态）。"""
    _transition_phase(session, PHASE_SUPPLEMENTED, reason=reason)
