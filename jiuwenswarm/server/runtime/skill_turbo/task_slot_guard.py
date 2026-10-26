# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""taskProgress 槽位泄漏登记簿（跨请求，模块级单例）。

前端只有唯一的 taskProgress 槽位，由 ``task.update`` 全量快照 last-wins 覆盖。
skill_turbo 入口 gate 误判"无外层 todo"（同轮并行 todo_create +
skill_acceleration_exec 竞态，外层 rail 的 ``_ACTIVE_TASK_ID`` 尚未设置）时，
turbo 的 task.* 事件被整流转发，内部 Stage 快照顶掉外层 todo 列表；若该轮
又在终态前被打断（HITL），续跑入口 gate 判 skip，终态快照被转发层抑制——
槽位永驻最后转发的 in_progress 帧，用户视角"Stage 永远卡住"
（完整定界见 docs/bugfix/B_03_leaked-task-slot-settlement.md）。

本登记簿给这个"无 owner 的真空"补一个 owner：

- ``claim_task_slot``：转发分支放行 task.update 时登记（槽位被该 turbo 任务集占用）；
- ``release_task_slot``：两类注销——① 转发分支自己跑到终态快照（任务集自然
  闭合，见 skill_turbo_tools 转发循环）；② 外层 rail 发自身 task.update
  （槽位被外层 todo 夺回，见 task_execution_rail._emit_task_update_event）；
- ``peek_leaked_task_slot``：skip 入口终止性出口查询——有登记即槽位仍被本
  引擎的陈旧任务集占用，调用方放行终态快照关账后自行 release。

键 = 外层父会话 session_id（turbo 的 parent_session 与外层 rail 的
ctx.session 是同一 session 对象，两侧 ``get_session_id()`` 同值）。值 =
占用任务集的 task_id 集，仅用于日志观测与事后诊断；清算判定**不要求** id
交集——槽位已被污染时"最新终态覆盖陈旧 in_progress"优于永驻陈旧（严格
交集匹配会放走"首任务集被弃、次任务集完成"的治愈路径，B_03 记录为拒绝方案）。

⛔ 语义红线：
  - 状态必须模块级跨请求存续——不得改成请求级 ContextVar（泄漏恰恰跨请求：
    登记在首入口、清算在续跑入口）；
  - 只登记"转发分支曾放行 task.update"的事实，不预测 gate 判定、不读 todo.json；
  - 唯一注销时机是"终态快照已实际进流"（FIFO 写成功）或"外层快照已进流"——
    预测式注销会让健康槽位被二次顶替。
"""

from __future__ import annotations

import logging
from collections.abc import Collection

logger = logging.getLogger(__name__)

__all__ = [
    "claim_task_slot",
    "peek_leaked_task_slot",
    "release_task_slot",
    "reset_task_slot_registry",
]

# 单进程多会话上限：超过后按插入序淘汰最旧条目。条目极小（会话 id → 数十个
# task id），上限只为防长生命周期进程无界增长（先例：类扫描缓存 _scan_cache
# 亦为模块级共享；hitl_dedup 序号表无上限——本处从紧）。
_MAX_TRACKED_SESSIONS = 4096

_leaked_task_slots: dict[str, frozenset[str]] = {}


def claim_task_slot(session_id: str, task_ids: Collection[str]) -> None:
    """登记槽位被该 turbo 任务集占用（转发分支放行 task.update 时调用）。

    同会话重复 claim 为覆盖语义（后写胜）：并发两条 turbo 流同会话转发时，
    登记跟随后进快照——该场景本身已属退化态（两套 Stage 快照交错顶替），
    登记簿不额外仲裁，见 B_03 边界 case。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return
    ids = frozenset(
        str(t).strip()
        for t in task_ids
        if str(t or "").strip()
    )
    if not ids:
        return
    if (
        sid not in _leaked_task_slots
        and len(_leaked_task_slots) >= _MAX_TRACKED_SESSIONS
    ):
        _leaked_task_slots.pop(next(iter(_leaked_task_slots)))
    _leaked_task_slots[sid] = ids


def release_task_slot(session_id: str) -> None:
    """注销登记：终态快照已进流（自然闭合）或外层 rail 快照夺回槽位。

    幂等：无登记时静默（外层 rail 每次发 todo 快照都会调，无泄漏会话是常态）。
    """
    _leaked_task_slots.pop(str(session_id or "").strip(), None)


def peek_leaked_task_slot(session_id: str) -> frozenset[str] | None:
    """查询未闭合的槽位占用；无登记返回 None。

    只读不删——清算方在终态快照**实际进流后**再调 release_task_slot
    （FIFO 写失败保留登记，下次终止性出口重试）。
    """
    return _leaked_task_slots.get(str(session_id or "").strip())


def reset_task_slot_registry() -> None:
    """清空登记簿（测试隔离用；生产零调用）。"""
    _leaked_task_slots.clear()
