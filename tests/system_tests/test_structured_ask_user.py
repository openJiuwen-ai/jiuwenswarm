# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System tests for StructuredAskUserRail and interrupt_helpers questions extraction.

Tests the integration of:
1. StructuredAskUserRail — init/uninit lifecycle, tool card schema, resolve_interrupt
2. interrupt_helpers._extract_questions_from_value — extraction from tool_args
3. interrupt_helpers.convert_interactions_to_ask_user_question — full conversion pipeline
4. init.prompts.ts prompt text — structured ask_user instructions present
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import (
    ToolCallInterruptRequest,
)
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserPayload

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
    EXTENDED_INPUT_PARAMS_CN,
    EXTENDED_INPUT_PARAMS_EN,
    MAX_STRUCTURED_QUESTIONS,
    StructuredAskUserRail,
    StructuredAskUserTool,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    _extract_questions_from_value,
    convert_interactions_to_ask_user_question,
    extract_question_from_interaction,
)
from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
    build_confirm_interrupt_message,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]


# =====================================================================
# Helpers
# =====================================================================

def _make_tool_call(
    tool_call_id: str = "tc_001",
    arguments: dict | str | None = None,
) -> ToolCall:
    """Create a ToolCall with given arguments."""
    if arguments is None:
        arguments = {"query": "Update?"}
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return ToolCall(id=tool_call_id, type="function", name="ask_user", arguments=arguments)


def _make_tcir(
    message: str = "Update?",
    tool_args: dict | str | None = None,
) -> ToolCallInterruptRequest:
    """Create a ToolCallInterruptRequest for testing."""
    if tool_args is None:
        tool_args = {"query": message}
    return ToolCallInterruptRequest(
        message=message,
        payload_schema={},
        tool_name="ask_user",
        tool_call_id="tc_001",
        tool_args=tool_args,
    )


def _make_mock_agent() -> MagicMock:
    """Create a mock agent with ability_manager and card."""
    agent = MagicMock()
    agent.ability_manager = MagicMock()
    agent.card = MagicMock()
    agent.card.id = "test_agent_001"
    return agent


# =====================================================================
# 1. StructuredAskUserTool Schema Tests
# =====================================================================

class TestStructuredAskUserToolSchema:
    """Verify the extended tool card schema for ask_user."""

    @staticmethod
    def test_en_schema_has_query_and_questions():
        """English schema must include both `query` and `questions` properties."""
        props = EXTENDED_INPUT_PARAMS_EN["properties"]
        assert "query" in props
        assert "questions" in props
        assert props["query"]["type"] == "string"
        assert props["questions"]["type"] == "array"

    @staticmethod
    def test_cn_schema_has_query_and_questions():
        """Chinese schema must include both `query` and `questions` properties."""
        props = EXTENDED_INPUT_PARAMS_CN["properties"]
        assert "query" in props
        assert "questions" in props

    @staticmethod
    def test_required_fields_only_query():
        """Only `query` is required; `questions` is optional."""
        assert EXTENDED_INPUT_PARAMS_EN["required"] == ["query"]
        assert EXTENDED_INPUT_PARAMS_CN["required"] == ["query"]

    @staticmethod
    def test_questions_schema_limits_each_call_to_four():
        """English and Chinese schemas must enforce the same question limit."""
        assert MAX_STRUCTURED_QUESTIONS == 4
        assert (
            EXTENDED_INPUT_PARAMS_EN["properties"]["questions"]["maxItems"]
            == MAX_STRUCTURED_QUESTIONS
        )
        assert (
            EXTENDED_INPUT_PARAMS_CN["properties"]["questions"]["maxItems"]
            == MAX_STRUCTURED_QUESTIONS
        )

    @staticmethod
    def test_questions_item_schema_structure():
        """Each question item must have `question` (required) and optional
        `header`, `options`, `multi_select`.
        """

        from jiuwenswarm.agents.harness.common.rails.ask_user_rail import (
            _QUESTIONS_ITEM_SCHEMA,
        )
        props = _QUESTIONS_ITEM_SCHEMA["properties"]
        assert "question" in props
        assert "header" in props
        assert "options" in props
        assert "multi_select" in props
        assert _QUESTIONS_ITEM_SCHEMA["required"] == ["question"]
        assert props["question"]["minLength"] == 1
        options_schema = props["options"]
        assert options_schema["maxItems"] == 4
        assert options_schema["anyOf"] == [{"maxItems": 0}, {"minItems": 2}]
        option_schema = options_schema["items"]
        assert option_schema["required"] == ["label"]
        assert option_schema["properties"]["label"]["minLength"] == 1

    @staticmethod
    def test_tool_card_name_is_ask_user():
        """Tool card name must be `ask_user` (same as original for compat)."""
        tool = StructuredAskUserTool(language="en")
        assert tool.card.name == "ask_user"

    @staticmethod
    def test_tool_card_input_params_match_en():
        """Tool card input_params should match EXTENDED_INPUT_PARAMS_EN."""
        tool = StructuredAskUserTool(language="en")
        assert tool.card.input_params == EXTENDED_INPUT_PARAMS_EN

    @staticmethod
    def test_tool_card_input_params_match_cn():
        """Tool card input_params should match EXTENDED_INPUT_PARAMS_CN."""
        tool = StructuredAskUserTool(language="cn")
        assert tool.card.input_params == EXTENDED_INPUT_PARAMS_CN


# =====================================================================
# 2. StructuredAskUserRail Lifecycle Tests
# =====================================================================

class TestStructuredAskUserRailLifecycle:
    """Verify rail init/uninit lifecycle with mock agent."""

    @staticmethod
    def test_init_registers_tool_in_ability_manager():
        """init() must register the tool card in agent.ability_manager."""
        rail = StructuredAskUserRail()
        agent = _make_mock_agent()

        with patch("openjiuwen.harness.rails.interrupt.ask_user_rail.resolve_language", return_value="en"):
            rail.init(agent)

        agent.ability_manager.add_ability.assert_called_once()
        added_card = agent.ability_manager.add_ability.call_args[0][0]
        assert added_card.name == "ask_user"

    @staticmethod
    def test_uninit_removes_tool_from_ability_manager():
        """uninit() must remove the tool from agent.ability_manager."""
        rail = StructuredAskUserRail()
        agent = _make_mock_agent()

        with patch("openjiuwen.harness.rails.interrupt.ask_user_rail.resolve_language", return_value="en"):
            rail.init(agent)
            rail.uninit(agent)

        agent.ability_manager.remove_ability.assert_called_once_with("ask_user")

    @staticmethod
    def test_init_uninit_clears_structured_tools():
        """uninit() must clear the internal _structured_tools list."""
        rail = StructuredAskUserRail()
        agent = _make_mock_agent()

        with patch("openjiuwen.harness.rails.interrupt.ask_user_rail.resolve_language", return_value="en"):
            rail.init(agent)
            assert len(rail.get_structured_tools()) == 1

            rail.uninit(agent)
            assert len(rail.get_structured_tools()) == 0

    @staticmethod
    def test_tool_names_default_is_ask_user():
        """Default tool_names should be {'ask_user'}."""
        rail = StructuredAskUserRail()
        assert rail.get_tools() == {"ask_user"}


# =====================================================================
# 3. StructuredAskUserRail _extract_questions Tests
# =====================================================================

class TestStructuredAskUserRailExtractQuestions:
    """Verify _extract_questions method parses tool call arguments correctly."""

    @staticmethod
    def test_extract_questions_from_dict_args():
        """Should extract questions from dict arguments."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Update?",
            "questions": [
                {"question": "Apply update?", "header": "Update",
                 "options": [{"label": "Apply", "description": "apply"}]},
            ],
        })
        result = rail.extract_questions(tc)
        assert result is not None
        assert len(result) == 1
        assert result[0]["question"] == "Apply update?"

    @staticmethod
    def test_extract_questions_from_string_args():
        """Should extract questions from JSON string arguments."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments=json.dumps({
            "query": "Update?",
            "questions": [{"question": "Q1", "header": "H1"}],
        }))
        result = rail.extract_questions(tc)
        assert result is not None
        assert len(result) == 1

    @staticmethod
    def test_extract_questions_returns_none_for_plain_query():
        """Should return None for a plain query (no questions)."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={"query": "What is your role?"})
        result = rail.extract_questions(tc)
        assert result is None

    @staticmethod
    def test_extract_questions_returns_none_for_none_tool_call():
        """Should return None when tool_call is None."""
        rail = StructuredAskUserRail()
        result = rail.extract_questions(None)
        assert result is None


# =====================================================================
# 4. interrupt_helpers._extract_questions_from_value Tests
# =====================================================================

class TestExtractQuestionsFromValue:
    """Verify _extract_questions_from_value handles all extraction paths."""

    @staticmethod
    def test_dict_value_obj_with_questions():
        """Should extract questions from a dict value_obj."""
        result = _extract_questions_from_value({
            "questions": [{"question": "Q1", "header": "H1"}],
        })
        assert result is not None
        assert len(result) == 1

    @staticmethod
    def test_tcir_with_questions_in_tool_args_dict():
        """Should extract questions from ToolCallInterruptRequest.tool_args (dict)."""
        tcir = _make_tcir(tool_args={
            "query": "Update?",
            "questions": [
                {"question": "Apply?", "header": "Update",
                 "options": [{"label": "Apply", "description": "apply"}]},
            ],
        })
        result = _extract_questions_from_value(tcir)
        assert result is not None
        assert result[0]["question"] == "Apply?"

    @staticmethod
    def test_tcir_with_questions_in_tool_args_json_string():
        """Should extract questions from ToolCallInterruptRequest.tool_args (JSON string)."""
        tcir = _make_tcir(tool_args=json.dumps({
            "query": "Update?",
            "questions": [{"question": "Apply?", "header": "Update"}],
        }))
        result = _extract_questions_from_value(tcir)
        assert result is not None
        assert result[0]["question"] == "Apply?"

    @staticmethod
    def test_tcir_plain_query_returns_none():
        """Should return None for plain query (no questions in tool_args)."""
        tcir = _make_tcir(tool_args={"query": "What is your role?"})
        result = _extract_questions_from_value(tcir)
        assert result is None

    @staticmethod
    def test_tcir_invalid_json_string_returns_none():
        """Should return None for tool_args that is invalid JSON."""
        tcir = _make_tcir(tool_args="not valid json{{{")
        result = _extract_questions_from_value(tcir)
        assert result is None

    @staticmethod
    def test_tcir_json_string_without_questions_returns_none():
        """Should return None for JSON string tool_args without questions field."""
        tcir = _make_tcir(tool_args=json.dumps({"query": "role?"}))
        result = _extract_questions_from_value(tcir)
        assert result is None

    @staticmethod
    def test_empty_questions_list_returns_none():
        """Should return None for an empty questions list."""
        tcir = _make_tcir(tool_args={"query": "Q?", "questions": []})
        result = _extract_questions_from_value(tcir)
        assert result is None

    @staticmethod
    def test_direct_questions_attribute_on_object():
        """Should extract questions from hasattr path (questions attribute)."""
        obj = MagicMock()
        obj.questions = [{"question": "Q1", "header": "H1"}]
        # Remove tool_args to ensure it goes through the hasattr path
        del obj.tool_args
        result = _extract_questions_from_value(obj)
        assert result is not None
        assert len(result) == 1

    @staticmethod
    def test_tool_args_takes_priority_over_direct_attribute():
        """If both direct questions and tool_args.questions exist, direct path wins."""
        tcir = _make_tcir(tool_args={
            "query": "Q?",
            "questions": [{"question": "from_tool_args", "header": "TA"}],
        })
        # ToolCallInterruptRequest does NOT have .questions attribute
        # so only tool_args path will be hit
        result = _extract_questions_from_value(tcir)
        assert result is not None
        assert result[0]["question"] == "from_tool_args"


# =====================================================================
# 5. convert_interactions_to_ask_user_question Full Pipeline
# =====================================================================

class TestConvertInteractionsToAskUserQuestion:
    """Verify the full conversion pipeline from TCIR to frontend event."""

    @staticmethod
    def test_structured_questions_produce_ask_user_interrupt():
        """Structured questions in tool_args should produce source=ask_user_interrupt."""
        tcir = _make_tcir(tool_args={
            "query": "Update?",
            "questions": [
                {"question": "Apply update?", "header": "Update",
                 "options": [{"label": "Apply", "description": "apply"},
                             {"label": "Skip", "description": "skip"}],
                 "multi_select": False},
            ],
        })

        # Wrap in InteractionOutput-like structure
        interaction = MagicMock()
        interaction.id = "req_001"
        interaction.value = tcir

        result = convert_interactions_to_ask_user_question([interaction])
        assert result is not None
        assert result["event_type"] == "chat.ask_user_question"
        assert result["source"] == "ask_user_interrupt"
        assert len(result["questions"]) == 1
        q = result["questions"][0]
        assert q["question"] == "Apply update?"
        assert q["header"] == "Update"
        # Options should include original 2 + "Other" appended by _build_multi_questions
        assert len(q["options"]) == 3
        assert q["options"][0]["label"] == "Apply"
        assert q["options"][1]["label"] == "Skip"
        assert q["options"][2]["label"] == "Other"

    @staticmethod
    def test_plain_query_produce_ask_user_interrupt():
        """Plain query (no questions) should produce source=ask_user_interrupt."""
        tcir = _make_tcir(tool_args={"query": "What is your role?"})

        interaction = MagicMock()
        interaction.id = "req_002"
        interaction.value = tcir

        result = convert_interactions_to_ask_user_question([interaction])
        assert result is not None
        assert result["source"] == "ask_user_interrupt"
        assert result["questions"][0]["question"] == "What is your role?"
        assert result["questions"][0]["options"] == []

    @staticmethod
    def test_empty_state_outputs_returns_none():
        """Empty state_outputs should return None."""
        result = convert_interactions_to_ask_user_question([])
        assert result is None

    @staticmethod
    def test_ask_user_request_without_tool_args_uses_query_from_tool_args():
        """AskUserRequest shells should still resolve as ask_user_interrupt."""
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_004",
                "value": {
                    "tool_name": "ask_user",
                    "tool_args": {"query": "Choose a language"},
                    "message": "Choose a language",
                    "questions": [],
                    "payload_schema": {},
                },
            }
        ])
        assert result is not None
        assert result["source"] == "ask_user_interrupt"
        assert result["questions"][0]["question"] == "Choose a language"

    @staticmethod
    def test_dict_interaction_with_questions_in_value():
        """Dict-format interaction should also work."""
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_003",
                "value": {
                    "query": "Update?",
                    "questions": [{"question": "Apply?", "header": "Upd",
                                   "options": [{"label": "Yes"}]}],
                },
            }
        ])
        assert result is not None
        assert result["source"] == "ask_user_interrupt"


# =====================================================================
# 5b. Confirm vs permission interrupt classification
# =====================================================================

class TestConfirmAndPermissionInterrupts:
    @staticmethod
    def test_confirm_interrupt_message_is_classified():
        message = build_confirm_interrupt_message("switch_mode", {"mode": "plan"})
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_confirm",
                "value": {
                    "tool_name": "switch_mode",
                    "message": message,
                    "tool_args": {"mode": "plan"},
                },
            }
        ])
        assert result is not None
        assert result["source"] == "confirm_interrupt"
        assert "switch_mode" in result["questions"][0]["question"]
        assert result["questions"][0]["header"].startswith("操作确认")

    @staticmethod
    def test_permission_interrupt_message_is_classified():
        message = "**工具 `write_file` 需要授权才能执行**\n\n请确认是否允许该操作。"
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_perm",
                "value": {
                    "tool_name": "write_file",
                    "message": message,
                    "tool_args": {"file_path": "foo.py"},
                },
            }
        ])
        assert result is not None
        assert result["source"] == "permission_interrupt"
        assert "write_file" in result["questions"][0]["question"]
        assert result["questions"][0]["header"].startswith("权限审批")

    @staticmethod
    def test_query_tool_permission_interrupt_is_not_ask_user():
        """A query argument does not make a non-ask_user tool an ask-user interrupt."""
        message = "**工具 `memory_search` 需要授权才能执行**\n\n请确认是否允许该操作。"
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_memory_search",
                "value": {
                    "tool_name": "memory_search",
                    "message": message,
                    "tool_args": {"query": "篮球"},
                },
            }
        ])
        assert result is not None
        assert result["source"] == "permission_interrupt"
        assert "memory_search" in result["questions"][0]["question"]
        assert result["questions"][0]["header"].startswith("权限审批")

    @staticmethod
    def test_extract_question_falls_back_for_generic_confirm_copy():
        question = extract_question_from_interaction({
            "id": "req_generic",
            "value": {
                "tool_name": "switch_mode",
                "message": "Please approve or reject?",
                "tool_args": {"mode": "normal"},
            },
        })
        assert question is not None
        assert "switch_mode" in question["question"]
        assert question["header"] == "操作确认: switch_mode"

    @staticmethod
    def test_exit_plan_mode_interrupt_uses_confirm_interrupt():
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_plan_exit",
                "value": {
                    "tool_name": "exit_plan_mode",
                    "message": "Please approve or reject?",
                    "tool_args": {},
                },
            }
        ])
        assert result is not None
        assert result["source"] == "confirm_interrupt"

    @staticmethod
    def test_plan_approval_message_falls_back_to_approve_reject_options():
        result = convert_interactions_to_ask_user_question([
            {
                "id": "req_plan_approval",
                "value": {
                    "tool_name": "exit_plan_mode",
                    "message": "**计划审批**\n\nAgent 已完成计划制定，等待你审批：\n\n计划内容",
                    "tool_args": {},
                },
            }
        ])

        assert result is not None
        assert result["source"] == "confirm_interrupt"
        assert result["plan_approval_kind"] == "plan_approval"
        assert result["plan_content"] == "计划内容"
        assert result["plan_language"] == "cn"
        assert result["questions"][0]["header"] == "Exit Plan and Execute"
        assert "请选择：" not in result["questions"][0]["question"]
        assert "- **批准**" not in result["questions"][0]["question"]
        assert result["questions"][0]["question"].endswith("计划内容")
        assert [option["label"] for option in result["questions"][0]["options"]] == [
            "批准",
            "拒绝",
        ]


# =====================================================================
# 6. StructuredAskUserRail resolve_interrupt Tests
# =====================================================================

class TestStructuredAskUserRailResolveInterrupt:
    """Verify resolve_interrupt handles structured and plain answers."""

    @staticmethod
    @pytest.mark.asyncio
    async def test_none_user_input_returns_interrupt():
        """When user_input is None, should return interrupt (first-time call)."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Update?",
            "questions": [{"question": "Apply?", "header": "Upd",
                          "options": [{"label": "Apply"}, {"label": "Skip"}]}],
        })
        ctx = MagicMock()

        decision = await rail.resolve_interrupt(ctx, tc, None)

        # Should be an InterruptResult
        from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
        assert isinstance(decision, InterruptResult)

    @staticmethod
    @pytest.mark.asyncio
    async def test_four_questions_are_allowed():
        """The maximum supported batch should still produce an interrupt."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Setup info",
            "questions": [
                {"question": f"Question {index}?", "header": f"Q{index}"}
                for index in range(1, MAX_STRUCTURED_QUESTIONS + 1)
            ],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
        assert isinstance(decision, InterruptResult)

    @staticmethod
    @pytest.mark.asyncio
    async def test_more_than_four_questions_are_rejected():
        """An oversized batch should return an argument error without prompting."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Setup info",
            "questions": [
                {"question": f"Question {index}?", "header": f"Q{index}"}
                for index in range(1, MAX_STRUCTURED_QUESTIONS + 2)
            ],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "at most 4 questions" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize(
        "option",
        [
            {"description": "missing label"},
            {"label": ""},
            {"label": "   "},
            {"label": 123},
            {"value": "must not replace label"},
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_option_labels_are_rejected(option):
        """Every selectable option must provide a non-empty string label."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{
                "question": "Which option?",
                "header": "Choice",
                "options": [option],
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions[0].options[0].label" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize("question", [None, "not an object", 123, []])
    @pytest.mark.asyncio
    async def test_non_object_questions_are_rejected(question):
        """Every questions item must be an object."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [question],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions[0] must be an object" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize("questions", [None, {}, "not an array", 123])
    @pytest.mark.asyncio
    async def test_non_array_questions_are_rejected(questions):
        """An explicitly provided questions value must be an array."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": questions,
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions must be an array" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize(
        "question",
        [
            {"header": "Choice"},
            {"question": "", "header": "Choice"},
            {"question": "   ", "header": "Choice"},
            {"question": 123, "header": "Choice"},
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_question_text_is_rejected(question):
        """Question text must be a non-empty string."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [question],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions[0].question" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize("header", [None, {}, 123])
    @pytest.mark.asyncio
    async def test_non_string_header_is_rejected(header):
        """An explicitly provided header must be a string."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{
                "question": "Which option?",
                "header": header,
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions[0].header must be a string" in decision.tool_result

    @staticmethod
    @pytest.mark.asyncio
    async def test_missing_header_uses_default():
        """An omitted header should be normalized to the frontend default."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{"question": "Which option?"}],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
        assert isinstance(decision, InterruptResult)

        interaction = MagicMock()
        interaction.id = "req_missing_header"
        interaction.value = decision.request
        payload = convert_interactions_to_ask_user_question([interaction])
        assert payload is not None
        assert payload["questions"][0]["header"] == "Question"

    @staticmethod
    @pytest.mark.parametrize("options", [None, {}, "not an array", "a,b", 123])
    @pytest.mark.asyncio
    async def test_non_array_options_are_rejected(options):
        """An explicitly provided options value must be an array (#2331)."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{
                "question": "Which option?",
                "header": "Choice",
                "options": options,
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "questions[0].options must be an array" in decision.tool_result

    @staticmethod
    @pytest.mark.asyncio
    async def test_empty_options_array_is_allowed():
        """An empty options array represents a free-input question."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Describe your preference",
            "questions": [{
                "question": "What do you prefer?",
                "header": "Preference",
                "options": [],
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
        assert isinstance(decision, InterruptResult)

    @staticmethod
    @pytest.mark.parametrize("option_count", [1, 5])
    @pytest.mark.asyncio
    async def test_invalid_option_counts_are_rejected(option_count):
        """Non-empty options arrays must contain between two and four items."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{
                "question": "Which option?",
                "header": "Choice",
                "options": [
                    {"label": f"Option {index}"}
                    for index in range(option_count)
                ],
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "must contain either 0 or 2-4 items" in decision.tool_result

    @staticmethod
    @pytest.mark.parametrize("option_count", [2, 4])
    @pytest.mark.asyncio
    async def test_valid_option_counts_are_allowed(option_count):
        """Two and four options should both remain valid."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Choose",
            "questions": [{
                "question": "Which option?",
                "header": "Choice",
                "options": [
                    {"label": f"Option {index}"}
                    for index in range(option_count)
                ],
            }],
        })

        decision = await rail.resolve_interrupt(MagicMock(), tc, None)

        from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult
        assert isinstance(decision, InterruptResult)

    @staticmethod
    @pytest.mark.asyncio
    async def test_structured_answer_dict_returns_reject():
        """Structured answer as dict should return RejectResult with formatted text."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Update?",
            "questions": [{"question": "Apply update?", "header": "Update",
                          "options": [{"label": "Apply update"}, {"label": "Skip"}]}],
        })
        ctx = MagicMock()

        # Simulate user selecting "Apply update"
        user_input = {"answers": {"Apply update?": "Apply update"}}
        decision = await rail.resolve_interrupt(ctx, tc, user_input)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "Apply update" in decision.tool_result

    @staticmethod
    @pytest.mark.asyncio
    async def test_structured_answer_string_fallback():
        """String answer for a structured question should be handled as free-text."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Update?",
            "questions": [{"question": "Apply?", "header": "Upd"}],
        })
        ctx = MagicMock()

        decision = await rail.resolve_interrupt(ctx, tc, "I want to customize")

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "I want to customize" in decision.tool_result

    @staticmethod
    @pytest.mark.asyncio
    async def test_plain_query_delegates_to_parent():
        """Plain query (no questions) should delegate to parent AskUserRail."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={"query": "What is your role?"})
        ctx = MagicMock()

        # AskUserPayload changed: answer (str) → answers (dict)
        # Construct payload compatible with both old and new upstream versions
        if "answer" in AskUserPayload.model_fields:
            user_input = AskUserPayload(answer="I am a developer")
        else:
            user_input = AskUserPayload(answers={"What is your role?": "I am a developer"})
        decision = await rail.resolve_interrupt(ctx, tc, user_input)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "I am a developer" in decision.tool_result

    @staticmethod
    @pytest.mark.asyncio
    async def test_structured_answer_with_multiple_questions():
        """Multiple structured questions answered should format all answers."""
        rail = StructuredAskUserRail()
        tc = _make_tool_call(arguments={
            "query": "Setup info",
            "questions": [
                {"question": "Branch naming?", "header": "Branch"},
                {"question": "Test runner?", "header": "Test"},
            ],
        })
        ctx = MagicMock()

        user_input = {
            "answers": {
                "Branch naming?": "feature/*",
                "Test runner?": "pytest",
            },
        }
        decision = await rail.resolve_interrupt(ctx, tc, user_input)

        from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
        assert isinstance(decision, RejectResult)
        assert "Branch naming?" in decision.tool_result
        assert "feature/*" in decision.tool_result
        assert "Test runner?" in decision.tool_result
        assert "pytest" in decision.tool_result


# =====================================================================
# 7. init.prompts.ts Prompt Text Tests (read source file directly)
# =====================================================================

_INIT_PROMPTS_TS_PATH = (
    Path(__file__).parent.parent.parent
    / "jiuwenswarm"
    / "cli"
    / "src"
    / "core"
    / "commands"
    / "builtins"
    / "init.prompts.ts"
)


class TestInitPromptStructuredAskUser:
    """Verify the /init prompt text instructs structured ask_user usage.

    These tests read the TypeScript source file directly rather than importing,
    since init.prompts.ts is a TypeScript module not importable by Python.
    """

    @staticmethod
    def _read_prompts_ts() -> str:
        """Read the init.prompts.ts source file."""
        if not _INIT_PROMPTS_TS_PATH.exists():
            pytest.skip("init.prompts.ts not found at expected path")
        return _INIT_PROMPTS_TS_PATH.read_text(encoding="utf-8")

    def test_en_prompt_contains_ask_user_questions_parameter(self):
        """EN prompt must instruct LLM to use `ask_user` with `questions`."""
        content = self._read_prompts_ts()
        assert "questions" in content
        assert "ask_user" in content
        # Must NOT contain the old conditional language
        assert "If `ask_user` supports" not in content

    def test_zh_prompt_contains_ask_user_questions_parameter(self):
        """ZH prompt must instruct LLM to use `ask_user` with `questions`."""
        content = self._read_prompts_ts()
        assert "questions" in content
        assert "ask_user" in content
        # Must NOT contain the old conditional language
        assert "若 `ask_user` 支持" not in content

    def test_en_prompt_contains_apply_update_skip_options(self):
        """EN prompt must mention 'Apply update' / 'Skip' as concrete options."""
        content = self._read_prompts_ts()
        assert "Apply update" in content
        assert "Skip (keep current)" in content

    def test_zh_prompt_contains_apply_update_skip_options(self):
        """ZH prompt must mention '应用更新' / '跳过' as concrete options."""
        content = self._read_prompts_ts()
        assert "应用更新" in content
        assert "跳过" in content

    def test_en_step3_has_questions_usage_example(self):
        """EN Step 3 must include ask_user questions usage example."""
        content = self._read_prompts_ts()
        assert "multi_select" in content
        # The example should show the questions parameter structure
        assert "header" in content

    def test_zh_step3_has_questions_usage_example(self):
        """ZH Step 3 must include ask_user questions usage example."""
        content = self._read_prompts_ts()
        assert "multi_select" in content
        assert "header" in content
