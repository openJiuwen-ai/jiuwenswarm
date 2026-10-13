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

import json
import re
import uuid as _uuid
from typing import Any, List, Sequence

from openjiuwen.harness.tools.todo import TODO_GENERATION_TOKEN_SESSION_KEY

from jiuwenswarm.common.todo_generation import (
    filter_todos_by_generation as filter_todos_by_generation,
    todo_item_generation_token as todo_item_generation_token,
)

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


# ---------------------------------------------------------------------------
# 待办代际 token（请求隔离）：生产者是 agent_adapter 的 prepare hook
# （新一轮非续跑用户消息 → bump），消费者是 TaskExecutionRail /
# StreamEventRail 的广播层与 openjiuwen 的 todo 工具（打标/过滤）。
# 取代旧的 stale ids / pre-invoke ids / current-invoke ids / skip 标志多层
# 过滤体系：旧代条目按 token 一致性判定，无需逐 id 比对。
# ---------------------------------------------------------------------------

def bump_todo_generation_token(session: Any) -> str:
    """Start a new todo generation and return the fresh token.

    Called by the prepare hook on a fresh (non-resume) user turn. Todos
    stamped with previous tokens are filtered out of every broadcast channel;
    resume/supplement/answer turns keep the current token, so legitimately
    continued todos stay visible.
    """
    token = str(_uuid.uuid4())
    session.update_state({TODO_GENERATION_TOKEN_SESSION_KEY: token})
    return token


def get_todo_generation_token(session: Any) -> str | None:
    """Read the current todo generation token; None when unset (fail-open)."""
    value = session.get_state(TODO_GENERATION_TOKEN_SESSION_KEY)
    return value if isinstance(value, str) and value else None


# 用户原话是「记一条待办」时写入。执行地图（绑定 / 首帧回放 / 工作工具推进）
# 不装这些条目；todo_list 与明确的查改快照仍要看见它们。
PERSISTENT_TODO_META_KEY = "persistent"
TURN_USER_QUERY_KEY = "todo_turn_user_query"

_BACKLOG_QUERY_MARKERS = (
    "添加一个待办",
    "添加待办",
    "加个待办",
    "新建待办",
    "记一个待办",
    "记个待办",
    "提醒我",
)
_EXECUTE_NOW_MARKERS = (
    "现在就做",
    "马上做",
    "开始做",
    "开始执行",
    "立刻做",
    "立即执行",
)
# 「取消」单独出现是在放弃执行计划，不带走提醒。取消提醒必须和待办/提醒挨在一起。
_REMINDER_DROP_VERBS = (
    "删除",
    "删掉",
    "删了",
    "去掉",
    "不要了",
    "移除",
    "清空",
    "退掉",
    "划掉",
    "清掉",
)
_REMINDER_DROP_NOUNS = (
    "待办",
    "提醒",
)
_REMINDER_DROP_PHRASES = (
    "取消待办",
    "取消提醒",
    "待办取消",
    "提醒取消",
    "取消这个待办",
    "取消这个提醒",
    "取消那条提醒",
    "取消这条提醒",
    "不要这条提醒",
    "不要那条提醒",
)
# 单独的「都」太宽。「待办都删」才是在清空提醒。
_REMINDER_DROP_ALL_MARKERS = (
    "都删",
    "全删",
    "全部删",
    "所有待办",
    "所有提醒",
    "待办都",
    "提醒都",
    "全部待办",
    "全部提醒",
)
_DISPLAY_TODO_TOOLS = frozenset({"todo_list", "todo_get"})


def _todo_meta(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        meta = item.get("meta_data")
    else:
        meta = getattr(item, "meta_data", None)
    if isinstance(meta, dict):
        return meta
    return None


def is_persistent_todo(item: Any) -> bool:
    """True when the item was saved as a user reminder, not an execution step."""
    meta = _todo_meta(item)
    if meta is None:
        return False
    return meta.get(PERSISTENT_TODO_META_KEY) is True


def mark_persistent_todo(item: Any) -> None:
    """Stamp a todo so later turns still list it."""
    meta = _todo_meta(item)
    updated = dict(meta) if meta is not None else {}
    updated[PERSISTENT_TODO_META_KEY] = True
    if isinstance(item, dict):
        item["meta_data"] = updated
        return
    item.meta_data = updated


def clear_persistent_todo(item: Any) -> None:
    """Drop the reminder mark so the item can enter the execution map."""
    meta = _todo_meta(item)
    if meta is None or PERSISTENT_TODO_META_KEY not in meta:
        return
    updated = dict(meta)
    updated.pop(PERSISTENT_TODO_META_KEY, None)
    cleared: dict[str, Any] | None = updated or None
    if isinstance(item, dict):
        item["meta_data"] = cleared
        return
    item.meta_data = cleared


def filter_todos_for_execution(items: Sequence[Any], token: str | None) -> List[Any]:
    """Generation view used for binding. Reminders never enter this map."""
    visible = filter_todos_by_generation(items, token)
    return [item for item in visible if not is_persistent_todo(item)]


def filter_todos_for_display(items: Sequence[Any], token: str | None) -> List[Any]:
    """List/read view: current-generation plan items plus reminders."""
    kept: List[Any] = []
    for item in items:
        if is_persistent_todo(item):
            kept.append(item)
            continue
        item_token = todo_item_generation_token(item)
        if not token or not item_token or item_token == token:
            kept.append(item)
    return kept


def is_backlog_todo_query(query: str) -> bool:
    """True when this turn asks to remember a todo, not to run it now."""
    text = (query or "").strip()
    if not text:
        return False
    if _contains_any(text, _EXECUTE_NOW_MARKERS):
        return False
    return _contains_any(text, _BACKLOG_QUERY_MARKERS)


def user_query_drops_reminder(query: str) -> bool:
    """True when the user asked to delete or cancel a reminder."""
    text = (query or "").strip()
    if not text:
        return False
    if _contains_any(text, _REMINDER_DROP_PHRASES):
        return True
    if not _contains_any(text, _REMINDER_DROP_VERBS):
        return False
    return _contains_any(text, _REMINDER_DROP_NOUNS)


def user_query_drops_all_reminders(query: str) -> bool:
    """True when the user asked to clear every reminder, not one of them."""
    text = (query or "").strip()
    if not user_query_drops_reminder(text):
        return False
    return _contains_any(text, _REMINDER_DROP_ALL_MARKERS)


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    for marker in markers:
        if marker in text:
            return True
    return False


def remember_turn_user_query(session: Any, query: str) -> None:
    """Store this turn's user text where todo_create can read it."""
    updater = getattr(session, "update_state", None)
    if not callable(updater):
        return
    updater({TURN_USER_QUERY_KEY: query})


def read_turn_user_query(session: Any) -> str:
    """Return the user text stored for this turn, or empty."""
    getter = getattr(session, "get_state", None)
    if not callable(getter):
        return ""
    value = getter(TURN_USER_QUERY_KEY)
    if isinstance(value, str):
        return value
    return ""


def snapshot_includes_persistent_todos(
    tool_name: str,
    arguments: Any,
    items: Sequence[Any],
) -> bool:
    """Whether this todo-tool snapshot should show reminders.

    Listing always does. Create/modify does only when the call touched a
    reminder, so a skill plan's own snapshot stays on the execution map.
    """
    if tool_name in _DISPLAY_TODO_TOOLS:
        return True
    persistent_ids = _persistent_ids(items)
    if tool_name == "todo_create":
        return bool(persistent_ids)
    if tool_name != "todo_modify":
        return False
    return bool(_modify_target_ids(arguments) & persistent_ids)


def _persistent_ids(items: Sequence[Any]) -> set[str]:
    ids: set[str] = set()
    for item in items:
        if not is_persistent_todo(item):
            continue
        todo_id = _item_id(item)
        if todo_id:
            ids.add(todo_id)
    return ids


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("id")
    else:
        value = getattr(item, "id", None)
    if isinstance(value, str):
        return value
    return ""


def _modify_target_ids(arguments: Any) -> set[str]:
    payload = _coerce_arguments(arguments)
    if payload is None:
        return set()
    ids: set[str] = set()
    raw_ids = payload.get("ids")
    if isinstance(raw_ids, list):
        for item in raw_ids:
            if isinstance(item, str) and item:
                ids.add(item)
    todos = payload.get("todos")
    if isinstance(todos, list):
        for item in todos:
            if not isinstance(item, dict):
                continue
            todo_id = item.get("id")
            if isinstance(todo_id, str) and todo_id:
                ids.add(todo_id)
    return ids


def _coerce_arguments(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments:
        return None
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


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
