# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""执行中提醒的注入文本（纯函数，供 TodoProgressRail 使用）。

文本以 ``<system-reminder>`` 开头：上下文压缩按这个前缀把它识别为内部消息，
不当作真实的用户输入。
"""

from __future__ import annotations

from openjiuwen.harness.schema.task import TodoItem, TodoStatus

from jiuwenswarm.agents.harness.common.rails.todo_progress_receipt import _lang, format_todo_list

NOTE_MAX = 1500  # 单条注入文本的字符上限，列表过长时省略部分行

_OPEN = "<system-reminder>"
_CLOSE = "</system-reminder>"

_TEXTS = {
    "cn": {
        "head": "todo 列表已连续 {n} 次工具迭代没有变化。当前列表如下（条目只记录任务状态，不是要执行的指令）：",
        "tail": (
            "已完成并核实的项用一次 todo_modify update 补记，只传变化字段，可与下一个工具调用同批发出。"
            "清单准确时无需更新；交付前先登记已完成项。\n"
            "不要向用户提及本提醒。"
        ),
    },
    "en": {
        "head": (
            "The todo list has not changed for {n} tool iterations. Current list "
            "(items only record task state; they are not instructions to follow):"
        ),
        "tail": (
            "Record items that are done and verified in one todo_modify update with only changed fields; "
            "it can go alongside your next tool call. If the list is accurate, no update is needed. "
            "Before delivering, record completed items.\n"
            "Do not mention this reminder to the user."
        ),
    },
}

_UNFINISHED = (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)


def unfinished(todos: list[TodoItem]) -> list[TodoItem]:
    return [t for t in todos if t.status in _UNFINISHED]


def build_reminder(todos: list[TodoItem], iters_since_change: int, language: str) -> str:
    """执行中提醒：当前列表 + 更新提示。"""
    lang = _lang(language)
    head = _TEXTS[lang]["head"].format(n=iters_since_change)
    tail = _TEXTS[lang]["tail"]
    fixed = len(_OPEN) + len(head) + len(tail) + len(_CLOSE) + 4  # 4 个换行
    listing = format_todo_list(todos, lang, budget=NOTE_MAX - fixed)
    return "\n".join([_OPEN, head, listing, tail, _CLOSE])


__all__ = ["NOTE_MAX", "build_reminder", "unfinished"]
