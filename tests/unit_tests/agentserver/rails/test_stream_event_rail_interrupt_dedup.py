# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for interrupt-envelope handling in _fix_incomplete_tool_context.

A delegating tool (task/sub-agent) does not raise when its sub-agent interrupts;
it returns the interrupt envelope as an ordinary result, which is stringified
into a ToolMessage on the parent context. After the sub-agent resumes, a second
ToolMessage arrives with the same tool_call_id carrying the genuine answer.

_fix_incomplete_tool_context de-duplicates ToolMessages first-wins, so unless the
envelope is classified as a placeholder the genuine answer is dropped and the
parent keeps replying with the sub-agent's own pending question.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
import jiuwenswarm.agents.harness.common.rails.stream_event_rail as rail_module


# A faithful rendering of str({"result_type": "interrupt", "state": [...],
# "interrupt_ids": [...]}) as produced when the envelope reaches a ToolMessage.
ENVELOPE = (
    "{'result_type': 'interrupt', 'state': [OutputSchema(type='__interaction__', "
    "index=0, payload=InteractionOutput(id='chatcmpl-tool-fake0001', "
    "value=ToolCallInterruptRequest(message='Which naming style do you want?', "
    "payload_schema={'description': 'Payload'})))], "
    "'interrupt_ids': ['chatcmpl-tool-fake0001']}"
)
ENVELOPE_JSON = (
    '{"result_type": "interrupt", "state": [], '
    '"interrupt_ids": ["chatcmpl-tool-fake0001"]}'
)
REAL = (
    "success=True data={'output': 'Three candidates: retry_budget, "
    "retry_ceiling, retry_allowance.'}"
)


class FakeContext:
    """Minimal ModelContext stand-in with the three methods the rail uses."""

    def __init__(self, messages: list[Any]) -> None:
        self._messages = list(messages)

    def get_messages(self) -> list[Any]:
        return list(self._messages)

    def pop_messages(self, size: int) -> None:
        if size:
            self._messages = self._messages[:-size]

    async def add_messages(self, message: Any) -> None:
        self._messages.append(message)


class FakeInputs:
    tools: list[Any] = []


class FakeCallbackContext:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.inputs = FakeInputs()
        self.extra: dict[str, Any] = {}


class LogCapture(logging.Handler):
    """Capture records straight off the module logger.

    jiuwenswarm loggers do not propagate, so pytest's caplog fixture sees
    nothing; attach to the emitting logger instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def repair_log():
    capture = LogCapture()
    logger = rail_module.logger
    previous_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)


def _tool_call(tool_call_id: str = "tc1", name: str = "task") -> ToolCall:
    return ToolCall(
        id=tool_call_id,
        type="function",
        name=name,
        arguments='{"subagent_type": "general-purpose"}',
    )


async def _repair(messages: list[Any]) -> list[Any]:
    ctx = FakeCallbackContext(FakeContext(messages))
    rail = JiuSwarmStreamEventRail()
    await rail._fix_incomplete_tool_context(ctx)
    return ctx.context.get_messages()


def _tool_messages(messages: list[Any]) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [ENVELOPE, ENVELOPE_JSON])
async def test_envelope_then_real_keeps_the_real_result(envelope, repair_log):
    """The genuine post-resume result must survive, not the interrupt envelope."""
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=envelope, tool_call_id="tc1"),
        ToolMessage(content=REAL, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == REAL
    # The pending question must not survive into the parent context.
    assert "Which naming style" not in str(tool_messages[0].content)

    # The repair is reported as a placeholder replacement. The trailing real
    # message is additionally tallied as a removed duplicate because it was
    # hoisted into the placeholder's slot; that accounting is shared with the
    # pre-existing "[Tool interrupted]" path and is not what this test guards.
    repair_lines = [m for m in repair_log.messages if "Repaired tool message context" in m]
    assert repair_lines, repair_log.messages
    assert "placeholder_replaced=1" in repair_lines[-1]


@pytest.mark.asyncio
async def test_envelope_alone_is_left_in_place():
    """While the sub-agent is still interrupted there is nothing to swap in."""
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=ENVELOPE, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == ENVELOPE


@pytest.mark.asyncio
async def test_real_result_only_is_unchanged():
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=REAL, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == REAL


@pytest.mark.asyncio
async def test_legacy_interrupt_placeholder_is_still_replaced(repair_log):
    """The pre-existing '[Tool interrupted]' path keeps working."""
    placeholder = (
        "[Tool interrupted] Tool task was interrupted by the user and has no result."
    )
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=placeholder, tool_call_id="tc1"),
        ToolMessage(content=REAL, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == REAL

    repair_lines = [m for m in repair_log.messages if "Repaired tool message context" in m]
    assert repair_lines, repair_log.messages
    assert "placeholder_replaced=1" in repair_lines[-1]


@pytest.mark.asyncio
async def test_two_genuine_results_still_deduplicate_first_wins():
    """Non-placeholder duplicates keep the existing first-wins behaviour."""
    first = "success=True data={'output': 'first'}"
    second = "success=True data={'output': 'second'}"
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=first, tool_call_id="tc1"),
        ToolMessage(content=second, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == first


# --- false-positive guards -------------------------------------------------

NOT_ENVELOPES = [
    pytest.param(
        "The sub-agent returned result_type interrupt with interrupt_ids set; "
        "that is the envelope shape to look for.",
        id="prose-mentioning-the-keys",
    ),
    pytest.param(
        "success=True data={'output': \"grep for 'result_type': 'interrupt' and "
        "'interrupt_ids' in handler.py\"}",
        id="quoted-snippet-inside-a-real-result",
    ),
    pytest.param(
        "{'output': \"the envelope is {'result_type': 'interrupt', "
        "'interrupt_ids': []}\", 'result_type': 'answer'}",
        id="envelope-nested-as-a-value",
    ),
    pytest.param(
        "{'result_type': 'answer', 'interrupt_ids': [], 'output': 'done'}",
        id="answer-envelope-carrying-interrupt_ids",
    ),
    pytest.param(
        "{'result_type': 'interrupt', 'state': []}",
        id="interrupt-without-interrupt_ids",
    ),
    pytest.param(
        "{'result_type': 'interrupted', 'interrupt_ids': ['x']}",
        id="near-miss-result_type-value",
    ),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("content", NOT_ENVELOPES)
def test_non_envelope_text_is_not_classified_as_an_envelope(content):
    assert not JiuSwarmStreamEventRail._is_serialized_interrupt_envelope_text(content)


@pytest.mark.parametrize("content", [ENVELOPE, ENVELOPE_JSON])
def test_envelope_text_is_classified_as_an_envelope(content):
    assert JiuSwarmStreamEventRail._is_serialized_interrupt_envelope_text(content)


@pytest.mark.asyncio
async def test_real_result_mentioning_the_keys_is_not_dropped():
    """A genuine answer that merely talks about interrupts must win the dedup."""
    chatty = (
        "success=True data={'output': \"The check is result_type == 'interrupt' "
        "plus 'interrupt_ids' in the dict.\"}"
    )
    messages = [
        UserMessage(content="delegate please"),
        AssistantMessage(content="", tool_calls=[_tool_call()]),
        ToolMessage(content=chatty, tool_call_id="tc1"),
    ]

    result = await _repair(messages)

    tool_messages = _tool_messages(result)
    assert len(tool_messages) == 1
    assert tool_messages[0].content == chatty
