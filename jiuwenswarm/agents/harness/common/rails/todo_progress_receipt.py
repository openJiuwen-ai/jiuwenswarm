# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""todo 写入回执的追加文本（纯函数，供 TodoProgressRail 使用）。

todo_modify 原回执只有 ``Successfully updated N task(s)``，模型看不到当前列表、
也得不到下一步提示；这里按写入后的列表状态给出一条提示，todo_modify 另附当前列表。
"""

from __future__ import annotations

from typing import Literal

from openjiuwen.harness.schema.task import TodoItem, TodoStatus

from jiuwenswarm.agents.harness.common.rails.todo_progress_snapshot import TodoReceiptChange

_CONTENT_MAX = 80
_SUFFIX_MAX = 600  # 整条追加文本的字符上限，条目多时省略部分列表行
_MISSING_CONTENT_MAX = 30  # 遗漏条目全文放不下时每项内容的截断长度

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
        "last_item": "仅剩当前一项。做完并核实后先改为 completed 再交付；尚未完成或等待必要验证时保持原状态，说明实际缺项及原因。",
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
            "Only the current item is left. Once done and verified, set it to completed before delivery; "
            "if unfinished or awaiting required checks, keep its status and explain actual missing work and reasons."
        ),
    },
}

_LABELS = {
    "cn": {"list": "当前 todo 列表：", "hint": "提示：",
           "closed": "（已结束 {n} 项，未列出）", "more": "（另有 {n} 项未列出）", "more_sep": "",
           "regressed_head": "已完成项退回未完成：",
           "regressed_tail": "。执行这些条目前请先核实。确需重做才继续，误填则用 todo_modify 修正。",
           "missing_head": "重建后的清单缺少原已完成项：",
           "missing_item": "{id}（{content}）",
           "missing_tail": ("。这些工作仍属于当前任务时，下一次 todo 调用用 {action} 按原 id、原内容以 completed 状态补回"
                            "{truncated}；用户要求删除的、已经以新 id 保留在清单中的不补。"),
           "missing_truncated": "（以“…”结尾的内容显示不全，按原条目全文补回）",
           "missing_insert": "todo_modify insert_before（target_id 为 {target}）",
           "missing_append": "todo_modify append"},
    "en": {"list": "Current todo list:", "hint": "Hint: ",
           "closed": "({n} closed items not shown)", "more": "({n} more items not shown)", "more_sep": " ",
           "regressed_head": "Completed items became unfinished: ",
           "regressed_tail": (". Verify these items before working on them. "
                              "Continue only if rework is needed, or correct the status via todo_modify."),
           "missing_head": "Completed items missing from the rebuilt list: ",
           "missing_item": "{id} ({content})",
           "missing_tail": (". If this work still belongs to the current task, restore it in your next todo call "
                            "via {action}, keeping the original ids and content with status completed{truncated}. "
                            "Skip items the user asked to remove and items already kept under a new id."),
           "missing_truncated": " (\"…\" marks cut-short content)",
           "missing_insert": "todo_modify insert_before with target_id {target}",
           "missing_append": "todo_modify append"},
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


def _missing_hint_parts(todos: list[TodoItem], language: str, *, truncated: bool) -> tuple[str, str]:
    """补回位置：插到第一个未完成项之前，使已完成项连在一起、排在当前工作之前；没有未完成项时追加到末尾。"""
    labels = _LABELS[language]
    target = next((t.id for t in todos if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)), None)
    action = labels["missing_insert"].format(target=target) if target else labels["missing_append"]
    note = labels["missing_truncated"] if truncated else ""
    return labels["missing_head"], labels["missing_tail"].format(action=action, truncated=note)


def _missing_entry(item_id: str, content: str, language: str, *, truncate: bool) -> str:
    if truncate and len(content) > _MISSING_CONTENT_MAX:
        content = content[: _MISSING_CONTENT_MAX - 1] + "…"
    return _LABELS[language]["missing_item"].format(id=item_id, content=content)


def _id_hint_min_len(head: str, tail: str, count: int, language: str) -> int:
    """一项都不列、只注明省略数量时的长度。"""
    labels = _LABELS[language]
    return len(head + (labels["more_sep"] + labels["more"]).format(n=count) + tail)


def _id_hint(head: str, tail: str, entries: list[str], language: str, budget: int) -> str:
    """列出条目的提示，总长不超过 budget，放不下的注明省略数量。"""
    full_hint = head + ", ".join(entries) + tail
    if len(full_hint) <= budget:
        return full_hint

    labels = _LABELS[language]
    omitted = labels["more_sep"] + labels["more"]
    kept: list[str] = []
    for entry in entries:
        candidate = kept + [entry]
        reserve = omitted.format(n=len(entries) - len(candidate)) if len(candidate) < len(entries) else ""
        if len(head + ", ".join(candidate) + reserve + tail) > budget:
            break
        kept = candidate
    more = omitted.format(n=len(entries) - len(kept)) if len(kept) < len(entries) else ""
    return head + ", ".join(kept) + more + tail


def receipt_suffix(tool_name: str, todos: list[TodoItem], *, noop: bool, language: str,
                   change: TodoReceiptChange | None = None) -> str:
    """回执追加文本，总长不超过 _SUFFIX_MAX。todo_create 原回执已含完整列表，只追加提示。

    change 中的原 id 与原内容只对 todo_create 生效：整体重建把已完成项退回未完成时提醒核实，
    漏掉已完成项时提醒按原 id 补回；todo_modify 逐项修改，重新打开或删除某项是明确意图，不提示。
    """
    lang = _lang(language)
    create = tool_name == "todo_create"
    regressed = change.regressed_ids if create and change else None
    missing = change.missing_items if create and change else None
    kind = pick_hint(todos, noop=noop)
    hints = []
    # 正在执行的条目刚被退回时先核实再继续；有遗漏时先补回。这两种情况都不给"继续执行"类提示，
    # 否则模型会照通用提示接着做下一项，略过后面的补回要求
    regressed_active = bool(regressed) and any(
        todo.status == TodoStatus.IN_PROGRESS and todo.id in regressed for todo in todos)
    if not missing and not regressed_active and (not create or kind in ("noop", "advance", "last_item")):
        hints.append(_LABELS[lang]["hint"] + _HINTS[lang][kind])
    # 每条提示：(开头, 结尾, 条目, 放不下时改用的 (结尾, 条目))
    parts: list[tuple[str, str, list[str], tuple[str, list[str]] | None]] = []
    if missing:
        head, tail = _missing_hint_parts(todos, lang, truncated=False)
        fallback = None
        if any(len(content) > _MISSING_CONTENT_MAX for _, content in missing):
            fallback = (_missing_hint_parts(todos, lang, truncated=True)[1],
                        [_missing_entry(i, c, lang, truncate=True) for i, c in missing])
        parts.append((head, tail, [_missing_entry(i, c, lang, truncate=False) for i, c in missing], fallback))
    if regressed:
        parts.append((_LABELS[lang]["regressed_head"], _LABELS[lang]["regressed_tail"], regressed, None))
    full_len = sum(len(head + ", ".join(entries) + tail) + 1 for head, tail, entries, _ in parts)
    if parts and 2 + _joined_len(hints) + full_len > _SUFFIX_MAX:
        hints = []  # 退回和遗漏条目加上通用提示后列不全时，省略通用的下一步提示，空间留给条目
    for index, (head, tail, entries, fallback) in enumerate(parts):
        # 为后面的提示预留至少"只注明省略数量"所需的长度，保证每条提示都能出现
        reserve = sum(_id_hint_min_len(h, t, len(e), lang) + 1 for h, t, e, _ in parts[index + 1:])
        budget = _SUFFIX_MAX - 2 - _joined_len(hints) - (1 if hints else 0) - reserve  # 2 = 两个前导换行
        if fallback and len(head + ", ".join(entries) + tail) > budget:
            # 遗漏条目全文放不下时截断内容，并注明截断：原条目已从清单删除，模型会照抄看到的文字补回
            tail, entries = fallback
        hints.append(_id_hint(head, tail, entries, lang, budget))
    hint = "\n".join(hints)
    if tool_name == "todo_modify":
        listing = format_todo_list(todos, lang, budget=_SUFFIX_MAX - len(hint) - 3)  # 3 = 两个前导换行 + 列表与提示间换行
        return f"\n\n{listing}\n{hint}"
    return f"\n\n{hint}" if hint else ""


__all__ = ["format_todo_list", "pick_hint", "receipt_suffix"]
