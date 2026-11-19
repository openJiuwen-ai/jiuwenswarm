# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Excel rail that stops a turn from ending on unchecked work.

When the model is about to end the user's turn, the rail looks at the turn's
tool calls: cells written and not read back since, a chart or shape changed
and not looked at since, a tool call that failed and was not retried. Then it
asks for one more model call and puts a reminder before it. Each kind of
reminder comes at most once a turn, so the model may still end the turn by
saying what it did not check.

What the turn did is read from the conversation each time: the pane's tools
pause the turn, and every resume is a new agent invoke, so nothing kept in an
invoke (``ctx.extra``) lasts the turn. A failure is the call's error, which the rail
marks on the result when the call ends, or a result that reports one itself.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.foundation.llm import AssistantMessage, BaseMessage, ToolMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message import OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA as SOURCE
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail

_READ_TOOLS = frozenset({"get_cell_ranges", "get_range_as_csv"})
_WRITE_TOOLS = frozenset({"set_cell_range", "copy_paste_range"})
_CODE_TOOL = "execute_office_js"

# Where code writes and reads, by its text (whether it wrote at all is the task pane's mutation record): a
# property assignment or a mutating call writes; loading values or text, or rendering an image, after the last
# write reads the result back.
_CODE_WRITE = re.compile(
    r"\.\w+\s*=(?!=)|\.(?:add|delete|clear|insert|set|merge|unmerge|freeze|unfreeze|protect|unprotect|copy|move"
    r"|autoFit|refresh|apply|sort|group|ungroup)\w*\s*\(", re.IGNORECASE)
_CODE_IMAGE = re.compile(r"\.getImage\s*\(", re.IGNORECASE)
_CODE_READ = re.compile(
    r"""\.load\(\s*\[?\s*["'][^"']*(?:values|text)|freezePanes\.getLocation|\.getImage\s*\(""", re.IGNORECASE)
_CODE_CHART_WRITE = re.compile(
    r"\.(?:charts|shapes)\.add\w*\s*\(|\w+\.(?:top|left|width|height)\s*=(?!=)|\.setPosition\s*\("
    r"|\.(?:charts|shapes)\b[^\n;]*\.delete\s*\(", re.IGNORECASE)
_CODE_CHARTS = re.compile(r"\.(?:charts|shapes)\b", re.IGNORECASE)

# The fields of a code result that reports its own error, as code that catches one returns it.
_ERROR_FIELDS = frozenset({"error", "message", "stack", "success"})

_FAILED = "office_tool_failed"
_REMINDER = "office_reminder"

_REMINDERS = {
    "read_back": (
        "You changed the workbook this turn and have not read back the cells you changed. The user sees the "
        "displayed text, after number formats and locale, so check what is displayed, not only the value. Read "
        "them back with get_cell_ranges or get_range_as_csv, or load values or text in execute_office_js; read "
        "freeze panes, protection and visibility directly. Call the work done only when what you read matches "
        "the request; otherwise say what you did not check."),
    "look": (
        "You changed a chart or a shape this turn and have not looked at it. Value reads cannot show layout, "
        "overlap or size: render it with getImage in execute_office_js before you describe how it looks, or say "
        "that you did not check it visually."),
}


@dataclass
class _Turn:
    """What the turn's tool calls left unchecked, updated result by result in the order they came back."""

    unread_writes: int = 0
    unviewed_chart: bool = False
    # (tool name, model call that made it) of each failed call no later call of the same tool redid.
    failures: list[tuple[str, int]] = field(default_factory=list)
    reminded: set[str] = field(default_factory=set)

    def record(self, name: str, arguments: dict[str, Any], content: Any, failed: bool, batch: int) -> None:
        if failed:
            self.failures.append((name, batch))
            return
        self.failures = [(n, b) for n, b in self.failures if n != name or b >= batch]
        code = str(arguments.get("code") or "") if name == _CODE_TOOL else ""
        if name in _READ_TOOLS or (code and _reads_back(code)):
            self.unread_writes = 0
        elif _left_unread(name, content):
            self.unread_writes += 1
        if code:
            last_chart_write, last_image = _last(_chart_writes(code)), _last(_CODE_IMAGE.finditer(code))
            if last_chart_write != last_image:
                self.unviewed_chart = last_chart_write > last_image

    def reminder(self) -> tuple[str, str] | None:
        """The reminder the turn should not end without: its kind and text."""
        if self.unread_writes and "read_back" not in self.reminded:
            return "read_back", _REMINDERS["read_back"]
        if self.unviewed_chart and "look" not in self.reminded:
            return "look", _REMINDERS["look"]
        if self.failures and "failure" not in self.reminded:
            counts = Counter(name for name, _batch in self.failures)
            tools = ", ".join(f"{name} (x{count})" if count > 1 else name for name, count in sorted(counts.items()))
            return "failure", (
                f"{len(self.failures)} tool call(s) failed this turn and were not retried (tools: {tools}). Do not "
                "describe those operations as done: retry them, work around the failure, or name it in your reply.")
        return None


def _last(matches: Iterable[re.Match[str]]) -> int:
    return max((match.start() for match in matches), default=-1)


def _reads_back(code: str) -> bool:
    last_write = _last(_CODE_WRITE.finditer(code))
    return any(match.start() > last_write for match in _CODE_READ.finditer(code))


def _chart_writes(code: str) -> Iterator[re.Match[str]]:
    yield from _CODE_CHART_WRITE.finditer(code)
    if _CODE_CHARTS.search(code):
        yield from _CODE_WRITE.finditer(code)


def _left_unread(name: str, content: Any) -> bool:
    """Whether the call wrote cells its own result does not show."""
    if name == _CODE_TOOL:
        return _code_wrote(content)
    return name in _WRITE_TOOLS and not _checked_itself(name, content)


def _checked_itself(name: str, content: Any) -> bool:
    """``set_cell_range`` answered with the values its formulas computed: the write shows its own result."""
    if name != "set_cell_range" or not isinstance(content, str):
        return False
    try:
        result = json.loads(content)
    except ValueError:
        return False
    return isinstance(result, dict) and isinstance(result.get("formula_results"), dict) and bool(
        result["formula_results"]) and not result.get("unverified_copy_destination")


def _code_wrote(content: Any) -> bool:
    """Whether code changed the workbook, as the task pane's mutation record on the result says."""
    try:
        answer = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return True
    mutation = answer.get("mutation") if isinstance(answer, dict) else None
    return not isinstance(mutation, dict) or mutation.get("state") != "not_applied"


def _reports_failure(content: Any) -> bool:
    """A call that ran but whose own result says it failed: code that returned ``{error}`` or ``{success: false}``.

    Only a result shaped as an error counts: data that has an ``error`` among other fields (a cell's ``#N/A``) does
    not.
    """
    try:
        answer = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return False
    result = answer.get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict):
        return False
    return result.get("success") is False or (bool(result.get("error")) and set(result) <= _ERROR_FIELDS)


def _arguments(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _turn_so_far(messages: list[BaseMessage]) -> _Turn:
    """What the turn the latest user message started has left unchecked, and which reminders it has had."""
    start = max((i for i, m in enumerate(messages)
                 if isinstance(m, UserMessage) and (m.metadata or {}).get(SOURCE) in (None, "query")), default=-1)
    turn, calls, batch = _Turn(), {}, 0
    for message in messages[start + 1:]:
        metadata = message.metadata or {}
        if isinstance(message, AssistantMessage) and message.tool_calls:
            batch += 1
            calls.update({call.id: (call.name, _arguments(call.arguments), batch) for call in message.tool_calls})
        elif isinstance(message, ToolMessage) and message.tool_call_id in calls:
            name, arguments, made_by = calls[message.tool_call_id]
            failed = bool(metadata.get(_FAILED)) or _reports_failure(message.content)
            turn.record(name, arguments, message.content, failed, made_by)
        elif isinstance(message, UserMessage) and metadata.get(SOURCE) == _REMINDER:
            turn.reminded.add(metadata[_REMINDER])
    return turn


class ExcelReminderRail(AgentRail):
    """Keep the model going, with a reminder, when it would end the turn on unchecked work."""

    _PENDING = "_office_reminder"

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        output, message = ctx.inputs.tool_result, ctx.inputs.tool_msg
        if message is not None and not ctx.is_paused and not getattr(output, "success", True):
            message.metadata = {**(message.metadata or {}), _FAILED: True}

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        response = ctx.inputs.response
        if not isinstance(response, AssistantMessage) or response.tool_calls:
            return
        reminder = _turn_so_far(ctx.context.get_messages()).reminder()
        if reminder is not None:
            ctx.extra[self._PENDING] = reminder
            ctx.request_model_continue()

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """The reminder goes after the answer the model wanted to end with, which is in the context by now."""
        reminder = ctx.extra.pop(self._PENDING, None)
        if reminder is not None:
            kind, text = reminder
            await ctx.context.add_messages(UserMessage(content=f"<system-reminder>\n{text}\n</system-reminder>",
                                                       metadata={SOURCE: _REMINDER, _REMINDER: kind}))
