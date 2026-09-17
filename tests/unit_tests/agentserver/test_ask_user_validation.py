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
