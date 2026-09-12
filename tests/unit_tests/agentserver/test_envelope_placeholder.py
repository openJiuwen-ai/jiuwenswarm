"""A resumed sub-agent's answer is discarded.

Scenario:
1. User asks the parent to delegate.
2. Parent assistant emits a tool_call (id "tc1", name "task").
3. The delegation tool returns an interrupt *envelope* dict (not an
   exception). The dict {"result_type": "interrupt", "state": [...],
   "interrupt_ids": [...]} is serialized into a ToolMessage.
4. User answers; the sub-agent resumes, finishes, and the real result is
   appended as another ToolMessage for the same tool_call_id.

_fix_incomplete_tool_context runs before every model call and dedupes
ToolMessages per tool_call_id first-wins, exempting "interrupt
placeholders". If the serialized envelope is not classified as a
placeholder, the envelope wins, the real answer is dropped, and the
model re-asks the question inside the envelope's state.
"""

import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


def _rail() -> JiuSwarmStreamEventRail:
    rail = JiuSwarmStreamEventRail.__new__(JiuSwarmStreamEventRail)
    rail._deep_agent = None
    return rail


def _envelope_tool_message(tool_call_id: str = "tc1") -> ToolMessage:
    """Serialize an interrupt envelope the way the delegation tool returns it."""
    envelope = {
        "result_type": "interrupt",
        "state": [
            {
                "type": "interaction",
                "payload": {
                    "id": "inner-1",
                    "value": {
                        "tool_name": "ask_user",
                        "message": "Which naming style do you want?",
                    },
                },
            }
        ],
        "interrupt_ids": ["inner-1"],
    }
    return ToolMessage(content=json.dumps(envelope), tool_call_id=tool_call_id)


def _ctx(messages):
    return SimpleNamespace(
        context=_ModelContext(messages),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )


@pytest.mark.asyncio
async def test_envelope_then_real_result_keeps_real_result():
    rail = _rail()
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "type": "tool_call", "name": "task", "arguments": "{}"}
            ],
        ),
        _envelope_tool_message("tc1"),
        ToolMessage(content="Final naming: snake_case everywhere.", tool_call_id="tc1"),
    ]
    ctx = _ctx(messages)
    await rail._fix_incomplete_tool_context(ctx)

    tool_texts = [
        m.content for m in ctx.context.messages if isinstance(m, ToolMessage)
    ]
    assert len(tool_texts) == 1, f"expected exactly one ToolMessage, got {tool_texts}"
    assert "snake_case" in tool_texts[0], (
        "real sub-agent answer was discarded; the interrupt envelope survived: "
        f"{tool_texts[0]}"
    )


@pytest.mark.asyncio
async def test_placeholder_text_then_real_result_keeps_real_result():
    """Control case: literal placeholder text is handled fine."""
    rail = _rail()
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "type": "tool_call", "name": "task", "arguments": "{}"}
            ],
        ),
        ToolMessage(
            content=(
                "[Tool interrupted] Tool task was interrupted by the user "
                "and has no result."
            ),
            tool_call_id="tc1",
        ),
        ToolMessage(content="Final naming: snake_case everywhere.", tool_call_id="tc1"),
    ]
    ctx = _ctx(messages)
    await rail._fix_incomplete_tool_context(ctx)

    tool_texts = [
        m.content for m in ctx.context.messages if isinstance(m, ToolMessage)
    ]
    assert len(tool_texts) == 1
    assert "snake_case" in tool_texts[0]
