# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ask_user options/answers validation (#2330, #2331)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult, RejectResult

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail


def _make_tool_call(arguments: dict) -> ToolCall:
    return ToolCall(
        id="tc_ask",
        type="function",
        name="ask_user",
        arguments=json.dumps(arguments),
    )


@pytest.mark.asyncio
async def test_options_string_a_b_is_rejected():
    """Issue #2331: options='a,b' must reject instead of silent no-UI."""
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {
                    "question": "Which option?",
                    "header": "Choice",
                    "options": "a,b",
                }
            ],
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, None)

    assert isinstance(decision, RejectResult)
    assert "questions[0].options must be an array" in decision.tool_result


@pytest.mark.asyncio
async def test_valid_options_still_interrupt():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {
                    "question": "Which option?",
                    "header": "Choice",
                    "options": [
                        {"label": "A", "description": "a"},
                        {"label": "B", "description": "b"},
                    ],
                }
            ],
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, None)

    assert isinstance(decision, InterruptResult)


@pytest.mark.asyncio
async def test_empty_structured_answers_are_rejected():
    """Issue #2330: empty resume (bare Other) must not resolve as a blank answer."""
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {
                    "question": "Which option?",
                    "header": "Choice",
                    "options": [
                        {"label": "A", "description": "a"},
                        {"label": "B", "description": "b"},
                    ],
                }
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {"answers": {}},
    )

    assert isinstance(decision, RejectResult)
    assert "answers must include at least one non-empty response" in decision.tool_result


# Every rejection the model can reach while writing an ask_user call. A
# rejection naming only the fault is retried unchanged -- the model has nothing
# new to try -- and repeated identical tool calls end the run at the loop
# detector, whose abort text is delivered to the user in place of an answer. So
# each message must also name what to send instead, which in practice means
# quoting a shape.
_REJECTION_CASES = [
    ("questions_not_array", {"query": "Q", "questions": "a,b"}),
    (
        "too_many_questions",
        {"query": "Q", "questions": [{"question": f"Q{i}"} for i in range(5)]},
    ),
    ("question_not_object", {"query": "Q", "questions": ["just a string"]}),
    ("question_text_missing", {"query": "Q", "questions": [{"header": "H"}]}),
    (
        "header_not_string",
        {"query": "Q", "questions": [{"question": "Q1", "header": 123}]},
    ),
    (
        "options_not_array",
        {"query": "Q", "questions": [{"question": "Q1", "options": "a,b"}]},
    ),
    (
        "option_without_label",
        {"query": "Q", "questions": [{"question": "Q1", "options": [{}, {}]}]},
    ),
    (
        "one_option",
        {"query": "Q", "questions": [{"question": "Q1", "options": [{"label": "A"}]}]},
    ),
]

# An instruction to do something, as opposed to a restatement of the rule that
# was broken. Deliberately a word list rather than a shape check: what matters
# is that the model is told to act, and the wording of any one message is free
# to change.
_REMEDY_WORDS = (
    "Send",
    "send",
    "Add",
    "add",
    "Replace",
    "replace",
    "Call",
    "call",
    "Give",
    "give",
    "Merge",
    "merge",
    "omit",
    "drop",
    "continue",
)


@pytest.mark.parametrize(
    "case_name,arguments",
    _REJECTION_CASES,
    ids=[name for name, _ in _REJECTION_CASES],
)
@pytest.mark.asyncio
async def test_every_rejection_states_a_remedy(case_name, arguments):
    rail = StructuredAskUserRail()

    decision = await rail.resolve_interrupt(
        MagicMock(), _make_tool_call(arguments), None
    )

    assert isinstance(decision, RejectResult), case_name
    message = decision.tool_result
    assert message.startswith("[INVALID_ARGUMENT]"), case_name
    assert any(word in message for word in _REMEDY_WORDS), message


@pytest.mark.asyncio
async def test_empty_answers_rejection_states_a_remedy():
    """The resume-path rejection is on the same loop: it too must say what to do.

    Reached when the user submits nothing at all. Without the closing clause an
    empty response has been read as agreement, so the message says explicitly
    that it is not an answer and gives both ways forward.
    """
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {
                    "question": "Which option?",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ],
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, {"answers": {}})

    assert isinstance(decision, RejectResult)
    assert any(word in decision.tool_result for word in _REMEDY_WORDS)
    assert "call ask_user again" in decision.tool_result
    assert "continue without it" in decision.tool_result


@pytest.mark.asyncio
async def test_a_valid_call_still_raises_an_interrupt():
    """The rejections are all this changes: a well-formed call is untouched."""
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {
                    "question": "Which option?",
                    "header": "Choice",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ],
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, None)

    assert isinstance(decision, InterruptResult)
