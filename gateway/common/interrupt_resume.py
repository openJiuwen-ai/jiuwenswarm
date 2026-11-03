# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``chat.send`` sources that answer a paused tool call."""

from typing import Any

CLIENT_TOOL_INTERRUPT_SOURCE = "client_tool_interrupt"
"""``source`` of a ``chat.send`` in which a client answers the tool calls it ran."""

INTERRUPT_RESUME_SOURCES: frozenset[str] = frozenset(
    {
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
        CLIENT_TOOL_INTERRUPT_SOURCE,
    }
)
"""``source`` values of a ``chat.send`` that answers a paused tool call.

Such a send resumes the suspended turn instead of starting one: the
Gateway keeps the running stream, the Runtime routes it to the paused
execution, and the adapter turns ``answers`` into an ``InteractiveInput``.
The chat frontends send the ``source`` of the ``chat.ask_user_question``
event they answer.
"""


def is_client_tool_resume(params: Any) -> bool:
    """Whether a ``chat.send`` carries a client's results for the tool calls it ran."""
    return isinstance(params, dict) and params.get("source") == CLIENT_TOOL_INTERRUPT_SOURCE


def answers_paused_calls(params: Any) -> bool:
    """Whether a ``chat.send`` answers a paused turn: at least one answer, and the question it names.

    A client tool resume names no question; each of its answers names the call it ran.
    """
    if not isinstance(params, dict):
        return False
    source = str(params.get("source") or "").strip()
    answers = params.get("answers")
    if source not in INTERRUPT_RESUME_SOURCES or not isinstance(answers, list) or not answers:
        return False
    return source == CLIENT_TOOL_INTERRUPT_SOURCE or bool(str(params.get("request_id") or "").strip())
