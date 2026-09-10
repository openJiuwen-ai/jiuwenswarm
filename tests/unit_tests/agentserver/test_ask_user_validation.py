# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ask_user options/answers validation (#2330, #2331)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult, RejectResult

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
    StructuredAskUserRail,
    StructuredAskUserTool,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)


def _make_tool_call(arguments: dict) -> ToolCall:
    return ToolCall(
        id="tc_ask",
        type="function",
        name="ask_user",
        arguments=json.dumps(arguments),
    )


def test_structured_ask_user_schema_declares_question_preview():
    tool = StructuredAskUserTool(language="cn", agent_id="test")

    preview_schema = tool.card.input_params["properties"]["questions"]["items"][
        "properties"
    ]["preview"]

    assert preview_schema["type"] == "object"
    assert preview_schema["required"] == ["text"]
    assert preview_schema["properties"]["text"]["type"] == "string"


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
async def test_json_encoded_questions_array_still_interrupts_with_structured_payload():
    """A provider may serialize the valid questions array once more."""
    questions = [
        {
            "question": "Which option?",
            "header": "Choice",
            "options": [
                {"label": "A", "description": "a"},
                {"label": "B", "description": "b"},
            ],
        }
    ]
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": json.dumps(questions),
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, None)

    assert isinstance(decision, InterruptResult)
    assert decision.request.questions == questions

    interaction = MagicMock()
    interaction.id = "req_ask"
    interaction.value = decision.request
    payload = convert_interactions_to_ask_user_question([interaction])
    assert payload is not None
    assert payload["source"] == "ask_user_interrupt"
    assert payload["questions"][0]["question"] == questions[0]["question"]


@pytest.mark.asyncio
@pytest.mark.parametrize("questions", ["[not json]", json.dumps({"question": "not an array"})])
async def test_json_encoded_non_array_questions_remain_rejected(questions):
    rail = StructuredAskUserRail()
    tc = _make_tool_call({"query": "Choose", "questions": questions})

    decision = await rail.resolve_interrupt(MagicMock(), tc, None)

    assert isinstance(decision, RejectResult)
    assert "questions must be an array" in decision.tool_result


@pytest.mark.asyncio
async def test_empty_structured_answers_preserve_answered_machine_state():
    """The shared AskUser contract preserves answered even when answers are empty."""
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "return_json": True,
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
        {"status": "answered", "answers": []},
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == '{"status":"answered","answers":[]}'


@pytest.mark.asyncio
async def test_explicit_skipped_keeps_readable_default():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {"status": "skipped", "answers": []},
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == "用户已跳过本次问答，未提供回答。"


@pytest.mark.asyncio
async def test_explicit_skipped_can_opt_in_to_compact_machine_state():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "return_json": True,
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {"status": "skipped", "answers": []},
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == '{"status":"skipped","answers":[]}'


@pytest.mark.asyncio
async def test_explicit_skipped_accepts_empty_frontend_answer_shells():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "return_json": True,
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {
            "status": "skipped",
            "answers": [
                {"question": "First?", "selected_options": []},
                {
                    "question": "Second?",
                    "selected_options": [],
                    "custom_input": None,
                },
                {
                    "question": "Third?",
                    "selected_options": [],
                    "custom_input": "   ",
                },
            ],
        },
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == '{"status":"skipped","answers":[]}'


@pytest.mark.asyncio
async def test_empty_answered_uses_non_empty_readable_fallback():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {"status": "answered", "answers": []},
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == "用户未提供回答。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_input",
    [
        {"status": "skipped", "answers": {"Which?": "A"}},
        {"status": "skipped", "answers": []},
        {
            "status": "skipped",
            "answers": [
                {"question": "Which?", "selected_options": ["A"]}
            ],
        },
        {
            "status": "skipped",
            "answers": [
                {
                    "question": "Which?",
                    "selected_options": [],
                    "custom_input": "A custom answer",
                }
            ],
        },
    ],
)
async def test_non_exact_skipped_shapes_are_rejected(user_input):
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )
    if user_input == {"status": "skipped", "answers": []}:
        user_input = {"status": "skipped"}

    decision = await rail.resolve_interrupt(MagicMock(), tc, user_input)

    assert isinstance(decision, RejectResult)
    # Non-exact / malformed shapes are treated as skipped (not [INVALID_ARGUMENT])
    # so the LLM does not mistake the tool as broken and trigger fallback cascades.
    assert "未提供回答" in decision.tool_result


@pytest.mark.asyncio
async def test_answered_structured_payload_keeps_readable_text_semantics():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        {
            "status": "answered",
            "answers": [
                {
                    "question": "Which?",
                    "selected_options": ["A"],
                    "custom_input": None,
                }
            ],
        },
    )

    assert isinstance(decision, RejectResult)
    assert decision.tool_result == "Which?: A"
@pytest.mark.asyncio
async def test_answered_structured_payload_can_opt_in_to_machine_state():
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "return_json": True,
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )
    answer_envelope = {
        "status": "answered",
        "answers": [
            {
                "question": "Which?",
                "selected_options": ["A"],
                "custom_input": None,
            }
        ],
    }

    decision = await rail.resolve_interrupt(
        MagicMock(),
        tc,
        answer_envelope,
    )

    assert isinstance(decision, RejectResult)
    assert json.loads(decision.tool_result) == answer_envelope

    from jiuwenswarm.agents.harness.common.tools.deepresearch.tools import (
        _normalize_feedback_handler_resume_feedback,
    )

    assert (
        _normalize_feedback_handler_resume_feedback(
            "问题: Which?\n回答: A",
            decision.tool_result,
        )
        == "问题: Which?\n回答: A"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_input",
    [
        {"answers": {"Which?": "A"}},
        {
            "answers": {"Which?": "A"},
            "_structured_response": {
                "status": "answered",
                "answers": [
                    {
                        "question": "Which?",
                        "selected_options": ["A"],
                        "custom_input": None,
                    }
                ],
            },
        },
        "A",
    ],
)
async def test_legacy_answer_representations_are_rejected(legacy_input):
    rail = StructuredAskUserRail()
    tc = _make_tool_call(
        {
            "query": "Choose",
            "questions": [
                {"question": "Which?", "header": "Choice", "options": []}
            ],
        }
    )

    decision = await rail.resolve_interrupt(MagicMock(), tc, legacy_input)

    assert isinstance(decision, RejectResult)
    # Legacy / unparseable representations are treated as skipped (not
    # [INVALID_ARGUMENT]) to avoid misleading the LLM into fallback cascades.
    assert "未提供回答" in decision.tool_result
