# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The wire of tools a connected client executes.

agent-core runs a client tool (``ToolCard(execution="client")``) by pausing the
call with a ``client_tool`` interrupt. The stream carries one
``chat.client_tool_request`` event per paused call; the client runs it and
answers with an ordinary ``chat.send``::

    {"source": "client_tool_interrupt",
     "answers": [{"interrupt_id": ..., "request_id": <tool call id>,
                  "result": {"status": ..., "content": [...], "is_error": ...}}, ...]}

One resume may answer any subset of the paused calls; agent-core keeps the
others paused, still waiting for their client. A client that binds the
conversation again (a reloaded task pane) gets, with its bind, the requests the
paused agent still waits on (``AgentRuntime.pending_interactions``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.tool import ClientToolResult
from openjiuwen.core.single_agent.interrupt import InterruptKind
from pydantic import ValidationError

from agent_gateway.common.interrupt_resume import CLIENT_TOOL_INTERRUPT_SOURCE
from jiuwenswarm.common.schema.message import EventType


def client_tool_request_event(interaction: Any) -> dict[str, Any] | None:
    """The ``chat.client_tool_request`` event for a paused client tool call, else None."""
    if isinstance(interaction, Mapping):
        request_id, value = str(interaction.get("id") or ""), interaction.get("value")
    else:
        request_id, value = str(getattr(interaction, "id", "") or ""), getattr(interaction, "value", None)
    read = value.get if isinstance(value, Mapping) else lambda name: getattr(value, name, None)
    if not request_id or read("interrupt_kind") != InterruptKind.CLIENT_TOOL:
        return None
    return {
        "event_type": EventType.CHAT_CLIENT_TOOL_REQUEST.value,
        "request_id": request_id,
        "interrupt_id": str(read("interrupt_id") or ""),
        "source": CLIENT_TOOL_INTERRUPT_SOURCE,
        "tool_name": str(read("tool_name") or ""),
        "arguments": dict(read("arguments") or {}),
        "client_tool": dict(read("client_request") or {}),
    }


class ClientToolAnswerMismatch(ValueError):
    """A client tool resume no paused call waits on: the client drops its results."""

    code = "CLIENT_TOOL_ANSWER_MISMATCH"


@dataclass(frozen=True)
class ClientToolAnswer:
    """One call's result from a ``client_tool_interrupt`` resume, for the interrupt it names."""

    request_id: str
    interrupt_id: str
    result: ClientToolResult


def client_tool_answers(answers: Iterable[Any]) -> list[ClientToolAnswer]:
    """The results a ``client_tool_interrupt`` resume carries.

    An entry answers its call only with the call's ``request_id``, the
    ``interrupt_id`` of the request it answers and a ``result`` that
    ``ClientToolResult.coerce`` accepts; any other entry answers nothing.
    """
    parsed: list[ClientToolAnswer] = []
    for answer in answers:
        if not isinstance(answer, Mapping) or "result" not in answer:
            continue
        request_id, interrupt_id = answer.get("request_id"), answer.get("interrupt_id")
        if not (isinstance(request_id, str) and request_id and isinstance(interrupt_id, str) and interrupt_id):
            continue
        try:
            result = ClientToolResult.coerce(answer["result"])
        except ValidationError:
            continue
        parsed.append(ClientToolAnswer(request_id=request_id, interrupt_id=interrupt_id, result=result))
    return parsed


__all__ = [
    "ClientToolAnswer",
    "ClientToolAnswerMismatch",
    "client_tool_answers",
    "client_tool_request_event",
]
