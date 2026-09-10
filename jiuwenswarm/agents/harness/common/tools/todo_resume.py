# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared helpers for interrupt/resume todo flows.

Pure vocabulary layer for the interrupt-recovery todo flows: resume-query
heuristics, CN/EN resume prompts, todo_create-guard messages, and the
session-state keys/markers shared between the agent_adapter prepare hooks
(producers) and TaskExecutionRail (consumer). No I/O, no adapter/session
lifecycle dependencies.
"""

from __future__ import annotations

import re
from typing import Any, List, Sequence

_ACTIVE_TODO_STATUS_VALUES = frozenset({"pending", "in_progress"})

_RESUME_QUERY_PATTERNS = (
    re.compile(r"^(继续|接着(做|来|干)?|往下(做|走)?|接着吧|继续吧|继续执行|继续任务|继续完成)[。!！?？…]*$", re.I),
    # Relay reconnect / interrupt-continue default phrase.
    re.compile(r"^请?继续(刚才的)?任务[。!！?？…]*$", re.I),
    re.compile(r"^(重试|再试(一次|一下)?|请重试|重新尝试)[。!！?？…]*$", re.I),
    re.compile(r"^(continue|resume|go on|carry on|keep going)[.!?…]*$", re.I),
    re.compile(r"^(retry|try again)[.!?…]*$", re.I),
    # 宽松匹配：继续 + 可选的任意内容（如任务名/技能名） + 任务/工作/执行/完成等动词
    # 覆盖 "继续excel表格任务"、"继续写PPT的工作"、"继续之前的分析任务" 等场景
    re.compile(r"^继续.{0,20}(任务|工作|执行|完成|做|跑|吧)[。!！?？…]*$", re.I),
)

TODO_CREATE_BLOCKED_CN = (
    "Error: 当前会话已有未完成任务（pending/in_progress）。"
    "请先 todo_list，再用 todo_modify 续跑；勿 todo_create 覆盖。"
    "仅当用户明确要求重来/重规划时，todo_create（ force=true）。"
)

TODO_CREATE_BLOCKED_EN = (
    "Error: This session has unfinished tasks (pending/in_progress). "
    "Call todo_list first, then todo_modify to resume; do not todo_create over them. "
    "Call todo_create (force=true) only when the user explicitly asks to replan or restart planning."
)

TODO_CREATE_READ_FAILED_CN = (
    "Error: 无法读取当前 todo 计划，尝试 todo_list 或重试，勿直接 todo_create 覆盖。"
)

TODO_CREATE_READ_FAILED_EN = (
    "Error: Cannot read the current todo plan. Try todo_list or retry; "
    "do not todo_create over it directly."
)

INTERRUPT_RESUME_DECISION_PROMPT_CN = """【中断续跑】用户要继续/重试当前任务（非整单重来）。

**本 session 有活跃计划（pending/in_progress），请续跑而非重建：**todo_list → todo_modify 续跑

**续跑时**
- 从 in_progress 项接着做；completed 默认跳过
- 「重试」= 重做当前 in_progress 步骤，不是从 Stage 1 重来
- 已 skill_complete 的 skill 勿重载、勿拆成新 todo

**主会话快照（供续跑对照）**
{snapshot}"""

INTERRUPT_RESUME_DECISION_PROMPT_EN = (
    """[Interrupt resume] The user wants to continue/retry the current task (not a full restart).

**This session has an active plan (pending/in_progress) — resume, do not rebuild:**
todo_list → todo_modify to resume

**When resuming**
- Continue from the in_progress item; skip completed by default
- "Retry" = redo the current in_progress step, not restart from Stage 1
- Do not reload skills that already ended with skill_complete

**Main session snapshot (for resume reference)**
{snapshot}"""
)

INTERRUPT_RESUME_TODO_REMINDER_CN = """【续跑】本 session 已有计划，按状态选工具：有活跃项 → todo_list + todo_modify；无活跃项 → todo_create。

{tasks}

当前 in_progress：{in_progress_task}
从该项续跑；completed 勿重做。"""

INTERRUPT_RESUME_TODO_REMINDER_EN = (
    """[Resume] This session already has a plan.
Choose tools by status: active items → todo_list + todo_modify; no active items → todo_create.

{tasks}

Current in_progress: {in_progress_task}
Resume from this item; do not redo completed items."""
)


TODO_RESUME_SNAPSHOT_PENDING_KEY = "jiuwenclaw_todo_resume_snapshot_pending"
SKIP_INVOKE_TASK_UPDATE_SYNC_KEY = "jiuwenclaw_skip_invoke_task_update_sync"
# before_invoke 捕获的旧 todo id 集合（JSON list）。TaskExecutionRail 写入，
# StreamEventRail._emit_todo_updated 读取——todo.updated 旁路（todo 工具
# after_tool_call 全量推 todo.json）也要过滤这些跨请求残留，否则旧任务的
# completed 条目会经该通道重新弹回前端（task.update 通道的 _stale_todo_ids
# 过滤管不到这条旁路）。
STALE_TODO_IDS_SESSION_KEY = "jiuwenclaw_stale_todo_ids"
# 本轮 invoke 中实际存在的 todo id 集合（JSON list）。TaskExecutionRail 在
# _sync_todo_and_emit_transitions 完成后写入，StreamEventRail._emit_todo_updated
# 读取——过滤 stale ids 时排除本轮新建的同 ID 项，防止 todo_create 创建的
# 新 todo 被 stale 过滤器误杀。
CURRENT_INVOKE_TODO_IDS_SESSION_KEY = "jiuwenclaw_current_invoke_todo_ids"
# before_invoke 中 _init_task_tracking 加载磁盘 todo 后的 id 快照。
# 用于区分「磁盘旧残留」与「本轮 LLM 新建」：过滤 stale ids 时，
# 若 id 既在 stale 集又在此快照中 → 真旧残留，过滤；否则 → 本轮新建，保留。
PRE_INVOKE_TODO_IDS_SESSION_KEY = "jiuwenclaw_pre_invoke_todo_ids"


def set_current_invoke_todo_ids(session: Any, ids: list[str] | set[str]) -> None:
    """Record todo ids that exist in _todo_map during this invoke."""
    session.update_state({CURRENT_INVOKE_TODO_IDS_SESSION_KEY: sorted(ids)})


def get_current_invoke_todo_ids(session: Any) -> set[str]:
    """Read the current invoke's todo ids; empty set when unset."""
    value = session.get_state(CURRENT_INVOKE_TODO_IDS_SESSION_KEY)
    if isinstance(value, list):
        return {
            str(item)
            for item in value
            if item is not None and not isinstance(item, (dict, list, tuple)) and str(item).strip()
        }
    return set()


def clear_current_invoke_todo_ids(session: Any) -> None:
    session.update_state({CURRENT_INVOKE_TODO_IDS_SESSION_KEY: None})


def set_pre_invoke_todo_ids(session: Any, ids: list[str] | set[str]) -> None:
    """Snapshot of todo ids loaded by _init_task_tracking (disk state)."""
    session.update_state({PRE_INVOKE_TODO_IDS_SESSION_KEY: sorted(ids)})


def get_pre_invoke_todo_ids(session: Any) -> set[str]:
    """Read the pre-invoke todo ids snapshot; empty set when unset."""
    value = session.get_state(PRE_INVOKE_TODO_IDS_SESSION_KEY)
    if isinstance(value, list):
        return {
            str(item)
            for item in value
            if item is not None and not isinstance(item, (dict, list, tuple)) and str(item).strip()
        }
    return set()


def clear_pre_invoke_todo_ids(session: Any) -> None:
    session.update_state({PRE_INVOKE_TODO_IDS_SESSION_KEY: None})


def set_stale_todo_ids(session: Any, ids: list[str] | set[str]) -> None:
    """Record the stale todo ids captured by before_invoke for this turn."""
    session.update_state({STALE_TODO_IDS_SESSION_KEY: sorted(ids)})


def get_stale_todo_ids(session: Any) -> set[str]:
    """Read the stale todo ids for this turn; empty set when unset."""
    value = session.get_state(STALE_TODO_IDS_SESSION_KEY)
    if isinstance(value, list):
        return {
            str(item)
            for item in value
            if item is not None and not isinstance(item, (dict, list, tuple)) and str(item).strip()
        }
    return set()


def clear_stale_todo_ids(session: Any) -> None:
    session.update_state({STALE_TODO_IDS_SESSION_KEY: None})


def _todo_status_value(item: Any) -> str:
    status = getattr(item, "status", "")
    if hasattr(status, "value"):
        return str(status.value).lower()
    return str(status).lower()


def has_active_todo_items(todos: Sequence[Any]) -> bool:
    """Return True if any todo is pending or in_progress."""
    return any(_todo_status_value(item) in _ACTIVE_TODO_STATUS_VALUES for item in todos)


def is_resume_user_query(query: str) -> bool:
    """Heuristic: short user message meaning continue/resume/retry the same task.

    Does not match explicit replan phrases (e.g. 从头开始); those are full restarts.
    """
    text = (query or "").strip()
    if not text or len(text) > 32:
        return False
    normalized = text.rstrip("。!！?？….")
    for pattern in _RESUME_QUERY_PATTERNS:
        if pattern.match(normalized):
            return True
    return False


def format_todo_snapshot_lines(todos: Sequence[Any]) -> str:
    lines: List[str] = []
    for item in todos:
        status = _todo_status_value(item)
        content = getattr(item, "content", "")
        item_id = getattr(item, "id", "")
        lines.append(f"- [{status}] {content} (id={item_id})")
    return "\n".join(lines)


def build_interrupt_resume_decision_prompt(
    language: str,
    *,
    snapshot: str,
) -> str:
    template = (
        INTERRUPT_RESUME_DECISION_PROMPT_EN
        if language in ("en", "english")
        else INTERRUPT_RESUME_DECISION_PROMPT_CN
    )
    return template.format(snapshot=snapshot.strip() or "(empty)")


def build_interrupt_resume_todo_reminder(
    language: str,
    *,
    tasks: str,
    in_progress_task: str,
) -> str:
    template = (
        INTERRUPT_RESUME_TODO_REMINDER_EN
        if language in ("en", "english")
        else INTERRUPT_RESUME_TODO_REMINDER_CN
    )
    return template.format(
        tasks=tasks.strip() or "(none)",
        in_progress_task=in_progress_task.strip() or "(none)",
    )


def todo_create_blocked_message(language: str = "cn") -> str:
    if language in ("en", "english"):
        return TODO_CREATE_BLOCKED_EN
    return TODO_CREATE_BLOCKED_CN


def todo_create_read_failed_message(language: str = "cn") -> str:
    if language in ("en", "english"):
        return TODO_CREATE_READ_FAILED_EN
    return TODO_CREATE_READ_FAILED_CN


# ---------------------------------------------------------------------------
# Skip-invoke markers: producers are the agent_adapter prepare hooks
# (stale_todo_cleanup), the consumer is TaskExecutionRail.before_invoke.
# Set on the runtime session (_interaction_session) — a flag on a throwaway
# session object is invisible to the rail.
# ---------------------------------------------------------------------------

def mark_skip_invoke_task_update_sync(session: Any) -> None:
    """Mark that before_invoke should not broadcast a stale todo snapshot this turn."""
    session.update_state({SKIP_INVOKE_TASK_UPDATE_SYNC_KEY: True})


def is_skip_invoke_task_update_sync(session: Any) -> bool:
    return session.get_state(SKIP_INVOKE_TASK_UPDATE_SYNC_KEY) is True


def clear_skip_invoke_task_update_sync(session: Any) -> None:
    session.update_state({SKIP_INVOKE_TASK_UPDATE_SYNC_KEY: None})
