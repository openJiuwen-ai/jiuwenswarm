# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""todo 写入回执的追加文本（纯函数，供 TodoProgressRail 使用）。

todo_modify 原回执只有 ``Successfully updated N task(s)``，模型看不到当前列表、
也得不到下一步提示；这里按写入后的列表状态给出一条提示，todo_modify 另附当前列表。
"""

from __future__ import annotations

from typing import Literal

from openjiuwen.harness.schema.task import TodoItem, TodoStatus

_CONTENT_MAX = 80
_SUFFIX_MAX = 600  # 整条追加文本的字符上限，条目多时省略部分列表行

Hint = Literal["noop", "empty", "all_done", "start_next", "advance", "last_item"]

_HINTS: dict[str, dict[Hint, str]] = {
    "cn": {
        "noop": "本次调用后列表没有变化。如果本意是更新状态，请核对 id 和参数。",
        "empty": "列表已清空。",
        "all_done": "所有项均已结束。列表状态不代表任务结果，最终答复按实际情况写。",
        "start_next": "没有进行中的项。开始下一项前，先把它改为 in_progress。",
        "advance": (
            "做完并核实的项改为 completed；还有 pending 项时，把要做的下一项改为 in_progress。"
            "状态更新可与下一个工具调用同批发出。"
        ),
        "last_item": "仅剩当前一项。做完后先改为 completed 再作答；未做完则保持原状态，在答复中写明原因。",
    },
    "en": {
        "noop": "The list did not change. If you meant to update a status, check the ids and arguments.",
        "empty": "The list is now empty.",
        "all_done": "Every item is closed. List status is not the task outcome; base the final answer on actual results.",
        "start_next": "Nothing is in progress. Set the next item to in_progress before working on it.",
        "advance": (
            "Set items that are done and verified to completed; if pending items remain, set the one you "
            "work on next to in_progress. Status updates can go in the same batch as the next tool call."
        ),
        "last_item": (
            "Only the current item is left. When done, set it to completed before answering; "
            "if not done, keep its status and state the reason in the answer."
        ),
    },
}

_LABELS = {
    "cn": {"list": "当前 todo 列表：", "hint": "提示：",
           "closed": "（已结束 {n} 项，未列出）", "more": "（另有 {n} 项未列出）"},
    "en": {"list": "Current todo list:", "hint": "Hint: ",
           "closed": "({n} closed items not shown)", "more": "({n} more items not shown)"},
}

_CLOSED = (TodoStatus.COMPLETED, TodoStatus.CANCELLED)


def _lang(language: str) -> str:
    return "en" if str(language).lower().startswith("en") else "cn"


def pick_hint(todos: list[TodoItem], *, noop: bool) -> Hint:
    """按写入后的状态选一条提示；noop 只在确定"列表没有变化"时传 True。"""
    if noop:
        return "noop"
    if not todos:
        return "empty"
    pending = sum(1 for t in todos if t.status == TodoStatus.PENDING)
    in_progress = sum(1 for t in todos if t.status == TodoStatus.IN_PROGRESS)
    if pending == 0 and in_progress == 0:
        return "all_done"
    if in_progress == 0:
        return "start_next"
    if pending == 0 and in_progress == 1:
        return "last_item"
    return "advance"


def _item_line(t: TodoItem) -> str:
    content = t.content if len(t.content) <= _CONTENT_MAX else t.content[: _CONTENT_MAX - 1] + "…"
    return f"- {t.id} [{t.status.value}] {content}"


def _joined_len(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(len(lines) - 1, 0)


def format_todo_list(todos: list[TodoItem], language: str, budget: int = _SUFFIX_MAX) -> str:
    """列出当前列表，总长不超过 budget：先省略已结束的项，仍超出再截掉末尾的项。"""
    labels = _LABELS[_lang(language)]
    lines = [labels["list"], *(_item_line(t) for t in todos)]
    if _joined_len(lines) <= budget:
        return "\n".join(lines)
    closed = sum(1 for t in todos if t.status in _CLOSED)
    head = [labels["list"]] + ([labels["closed"].format(n=closed)] if closed else [])
    open_lines = [_item_line(t) for t in todos if t.status not in _CLOSED]
    if _joined_len(head + open_lines) <= budget:
        return "\n".join(head + open_lines)
    # 预留"另有 N 项未列出"一行，N 取最大可能值，保证这一行的长度够用
    room = budget - _joined_len(head + [labels["more"].format(n=len(open_lines))])
    kept: list[str] = []
    for line in open_lines:
        if len(line) + 1 > room:
            break
        kept.append(line)
        room -= len(line) + 1
    return "\n".join(head + kept + [labels["more"].format(n=len(open_lines) - len(kept))])


def receipt_suffix(tool_name: str, todos: list[TodoItem], *, noop: bool, language: str) -> str:
    """回执追加文本，总长不超过 _SUFFIX_MAX。todo_create 原回执已含完整列表，只追加提示。"""
    lang = _lang(language)
    hint = _LABELS[lang]["hint"] + _HINTS[lang][pick_hint(todos, noop=noop)]
    if tool_name == "todo_modify":
        listing = format_todo_list(todos, lang, budget=_SUFFIX_MAX - len(hint) - 3)  # 3 = 两个前导换行 + 列表与提示间换行
        return f"\n\n{listing}\n{hint}"
    return f"\n\n{hint}"


__all__ = ["format_todo_list", "pick_hint", "receipt_suffix"]
