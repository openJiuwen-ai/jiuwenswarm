# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extended AskUserRail that supports structured questions with options.

The upstream AskUserRail (from openjiuwen) only accepts a plain `query` string.
This subclass extends the `ask_user` tool schema with an optional `questions`
parameter, allowing the LLM to present multi-choice options to the user.

When `questions` is provided, the interrupt payload includes structured
question data that the frontend TUI renders as clickable options instead
of a free-text input box.

The key mechanism: ToolCallInterruptRequest.tool_args preserves the original
tool call arguments (including `questions`). The interrupt_helpers pipeline
extracts questions from tool_args so the frontend can render them.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.core.single_agent.interrupt import InterruptRequest
from openjiuwen.core.single_agent.rail import AgentCallbackContext
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails.interrupt.ask_user_rail import (
    AskUserPayload,
    AskUserRail,
)
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    InterruptDecision,
)

logger = logging.getLogger(__name__)

MAX_STRUCTURED_QUESTIONS = 4

# ---------------------------------------------------------------------------
# Extended input schema
# ---------------------------------------------------------------------------

_QUESTIONS_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "minLength": 1,
            "description": "The question to present to the user.",
        },
        "header": {
            "type": "string",
            "description": "A short label displayed as a chip/tag.",
        },
        "options": {
            "type": "array",
            "description": "Available choices for this question (2-4 items).",
            "maxItems": 4,
            "anyOf": [{"maxItems": 0}, {"minItems": 2}],
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Display text for this option (1-5 words).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Explanation of what this option means.",
                    },
                    "preview": {
                        "type": "string",
                        "description": (
                            "Optional preview content rendered beside this option when "
                            "comparing concrete artifacts the user should visually compare "
                            "(e.g. ASCII mockups, code snippets). Markdown is supported; "
                            "use fenced code blocks for monospace mockups so alignment is "
                            "preserved. Only rendered for single-select questions; ignored "
                            "for multi-select."
                        ),
                    },
                },
                "required": ["label"],
            },
        },
        "multi_select": {
            "type": "boolean",
            "default": False,
            "description": "Allow multiple selections instead of just one.",
        },
    },
    "required": ["question"],
}

EXTENDED_INPUT_PARAMS_EN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The question to present to the user (required).",
        },
        "questions": {
            "type": "array",
            "description": (
                "Structured questions with selectable options. "
                "Use this when you want the user to choose from predefined options "
                "instead of typing free text. Ask at most 4 questions per call. "
                "Omit options for free-text input; otherwise provide 2-4 options. "
                "The user can always select 'Other' for custom input."
            ),
            "items": _QUESTIONS_ITEM_SCHEMA,
            "maxItems": MAX_STRUCTURED_QUESTIONS,
        },
    },
    "required": ["query"],
}

EXTENDED_INPUT_PARAMS_CN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "向用户展示的问题（必填）。",
        },
        "questions": {
            "type": "array",
            "description": (
                "带选项的结构化问题。当希望用户从预定义选项中选择而非自由输入时使用。"
                "每次调用最多询问 4 个问题。"
                "自由输入题不提供选项；否则必须提供 2-4 个选项。"
                "用户始终可以选择「其他」进行自定义输入。"
            ),
            "items": _QUESTIONS_ITEM_SCHEMA,
            "maxItems": MAX_STRUCTURED_QUESTIONS,
        },
    },
    "required": ["query"],
}

_EXTENDED_DESCRIPTION_EN: str = (
    "Interrupts execution and requests input from the user. "
    "Supports two modes:\n"
    "1. Plain query (free-text): pass only `query` — the user types their answer.\n"
    "2. Structured questions (multi-choice): pass `query` + `questions` — "
    "the user selects from predefined options. "
    "Use `questions` when you want the user to choose between specific options "
    "(e.g., 'Apply update' vs 'Skip'). Ask at most 4 questions per call. "
    "Omit options for free-text input; otherwise provide 2-4 options. "
    "For single-select questions, an option may carry a `preview` (markdown, "
    "e.g. fenced code block ASCII mockup) shown beside it to compare concrete "
    "artifacts; use it only when a visual comparison helps the user decide."
)

_EXTENDED_DESCRIPTION_CN: str = (
    "中断执行并向用户请求输入。支持两种模式：\n"
    "1. 纯文本查询：只传 `query` —— 用户自由输入回答。\n"
    "2. 结构化选项：传 `query` + `questions` —— 用户从预定义选项中选择。"
    "当你希望用户在特定选项间做选择时（如「应用更新」vs「跳过」）使用 `questions`。"
    "每次调用最多询问 4 个问题。自由输入题不提供选项；否则必须提供 2-4 个选项。"
    "对于单选问题，选项可携带 `preview`（markdown，"
    "如带围栏代码块的 ASCII mockup）展示在选项旁，用于对比具体产物；"
    "仅在视觉对比有助于用户决策时使用。"
)

# ---------------------------------------------------------------------------
# Structured answer payload
# ---------------------------------------------------------------------------


class StructuredAskUserPayload(BaseModel):
    """Payload for structured user answers."""

    answers: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description=(
            "Mapping of question text to selected option label(s). "
            "Value is a str for single-select, or list[str] for multi-select."
        ),
    )


# ---------------------------------------------------------------------------
# Extended AskUserTool
# ---------------------------------------------------------------------------


class StructuredAskUserTool(Tool):
    """AskUser tool with extended schema supporting structured questions."""

    def __init__(self, language: str = "cn", agent_id: Optional[str] = None):
        input_params = (
            EXTENDED_INPUT_PARAMS_EN
            if language == "en"
            else EXTENDED_INPUT_PARAMS_CN
        )
        description = (
            _EXTENDED_DESCRIPTION_EN if language == "en" else _EXTENDED_DESCRIPTION_CN
        )
        final_tool_id = (
            f"ask_user_{agent_id}" if agent_id else f"ask_user_{uuid.uuid4().hex}"
        )
        card = ToolCard(
            id=final_tool_id,
            name="ask_user",
            description=description,
            input_params=input_params,
        )
        super().__init__(card)

    async def invoke(self, query, questions=None, **kwargs):
        return {}

    async def stream(self, query, questions=None, **kwargs):
        yield {}


# ---------------------------------------------------------------------------
# StructuredAskUserRail
# ---------------------------------------------------------------------------


class StructuredAskUserRail(AskUserRail):
    """Extended AskUserRail that supports structured questions with options.

    When the LLM calls `ask_user` with a `questions` parameter, this rail
    injects the structured question data into the interrupt payload so the
    frontend TUI can render clickable options instead of a free-text input.

    The mechanism relies on ToolCallInterruptRequest.tool_args preserving
    the original tool call arguments. interrupt_helpers._extract_questions_from_value()
    checks tool_args for a `questions` field and converts it to frontend format.
    """

    def __init__(
        self,
        tool_names: Optional[Iterable[str]] = None,
        language: Optional[str] = None,
    ):
        super().__init__(tool_names=tool_names)
        self._structured_tools: list[StructuredAskUserTool] = []
        self._language = language

    def init(self, agent):
        """Register the extended ask_user tool with structured questions schema."""
        language = self._language or resolve_language()
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        tool = StructuredAskUserTool(language=language, agent_id=agent_id)
        self._structured_tools = [tool]

        # Unified registration: add_ability qualifies the stateful tool id to
        # ``{name}_{owner_id}`` and binds it in the resource manager, so
        # teardown_tools drops it at round-end instead of leaking a bare id that
        # refresh-warns on the next native rebuild.
        for tool in self._structured_tools:
            agent.ability_manager.add_ability(tool.card, tool)

    def uninit(self, agent):
        """Remove the extended ask_user tool."""
        if not hasattr(agent, "ability_manager"):
            self._structured_tools = []
            return
        for tool in self._structured_tools:
            name = getattr(tool.card, "name", None)
            if name:
                # Mirror add_ability: removes the agent-qualified id from both
                # this manager and the shared resource manager.
                agent.ability_manager.remove_ability(name)
        self._structured_tools = []

    def get_structured_tools(self) -> list[StructuredAskUserTool]:
        """Return the list of registered structured tools."""
        return self._structured_tools

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> InterruptDecision:
        """Handle interrupt resolution with structured answer support.

        For structured questions: user_input contains a dict with an `answers`
        key mapping question text → selected option label. We convert this to
        a rejection with the answer text.

        For plain query: delegate to parent class behavior (AskUserPayload).
        """
        args = self._parse_tool_args(tool_call)
        raw_questions = args.get("questions")
        if "questions" in args and not isinstance(raw_questions, list):
            return self.reject(
                tool_result=(
                    "[INVALID_ARGUMENT] questions must be an array when provided."
                )
            )
        questions_data = raw_questions if raw_questions else None
        if (
            questions_data is not None
            and len(questions_data) > MAX_STRUCTURED_QUESTIONS
        ):
            return self.reject(
                tool_result=(
                    "[INVALID_ARGUMENT] ask_user accepts at most "
                    f"{MAX_STRUCTURED_QUESTIONS} questions per call; "
                    f"received {len(questions_data)}. "
                    "Split them across multiple calls."
                )
            )

        for question_index, question in enumerate(questions_data or []):
            if not isinstance(question, Mapping):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}] "
                        "must be an object."
                    )
                )
            question_text = question.get("question")
            if not isinstance(question_text, str) or not question_text.strip():
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].question "
                        "is required and must be a non-empty string."
                    )
                )
            if "header" in question and not isinstance(question["header"], str):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].header "
                        "must be a string when provided."
                    )
                )
            if "options" not in question:
                continue
            options = question["options"]
            if not isinstance(options, list):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].options "
                        "must be an array when provided."
                    )
                )
            for option_index, option in enumerate(options):
                label = option.get("label") if isinstance(option, Mapping) else None
                if not isinstance(label, str) or not label.strip():
                    path = (
                        f"questions[{question_index}]."
                        f"options[{option_index}].label"
                    )
                    return self.reject(
                        tool_result=(
                            f"[INVALID_ARGUMENT] {path} is required "
                            "and must be a non-empty string."
                        )
                    )
            if options and not 2 <= len(options) <= 4:
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].options "
                        "must contain either 0 or 2-4 items; "
                        f"received {len(options)}."
                    )
                )

        if user_input is None:
            return self.interrupt(self._build_ask_request(tool_call))

        # Detect if this was a structured questions call by checking tool_args
        is_structured = questions_data is not None and len(questions_data) > 0

        if is_structured:
            try:
                if isinstance(user_input, StructuredAskUserPayload):
                    payload = user_input
                elif isinstance(user_input, dict):
                    if "answers" in user_input:
                        payload = StructuredAskUserPayload(
                            answers=user_input.get("answers", {}),
                        )
                    else:
                        # Frontend sends answers as {question: selected_option}
                        payload = StructuredAskUserPayload(answers=user_input)
                elif isinstance(user_input, AskUserPayload):
                    # Upstream AskUserPayload changed: answer (str) → answers (dict)
                    free_text = getattr(user_input, "answer", None)
                    if free_text is not None:
                        payload = StructuredAskUserPayload(
                            answers={"__free_text__": free_text},
                        )
                    else:
                        payload = StructuredAskUserPayload(
                            answers=user_input.answers,
                        )
                elif isinstance(user_input, str):
                    payload = StructuredAskUserPayload(
                        answers={"__free_text__": user_input},
                    )
                else:
                    return self.interrupt(self._build_ask_request(tool_call))

                # Format answer as readable text for the LLM
                answer_parts = []
                for q_text, selected in payload.answers.items():
                    if q_text == "__free_text__":
                        answer_parts.append(selected if isinstance(selected, str) else ", ".join(selected))
                    else:
                        value_text = selected if isinstance(selected, str) else ", ".join(selected)
                        answer_parts.append(f"{q_text}: {value_text}")
                answer_text = "\n".join(answer_parts) if answer_parts else ""
                if not answer_text.strip():
                    # Empty resume (e.g. bare Other) must not look like a valid answer (#2330).
                    return self.reject(
                        tool_result=(
                            "[INVALID_ARGUMENT] answers must include at least "
                            "one non-empty response."
                        )
                    )
                logger.info(
                    "[StructuredAskUserRail] Resolved structured answer: %s",
                    answer_text,
                )
                return self.reject(tool_result=answer_text)

            except Exception as exc:
                logger.warning(
                    "[StructuredAskUserRail] Failed to parse structured answer: %s, "
                    "falling back to interrupt",
                    exc,
                )
                return self.interrupt(self._build_ask_request(tool_call))

        # Plain query — delegate to parent which handles AskUserPayload.answers
        if isinstance(user_input, AskUserPayload):
            return await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
        elif isinstance(user_input, str):
            return self.reject(tool_result=user_input)
        return await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )

    def _build_ask_request(self, tool_call: Optional[ToolCall]) -> InterruptRequest:
        """Build interrupt request. For structured questions, the questions data
        flows through ToolCallInterruptRequest.tool_args (preserved by the
        interrupt handler). No need to attach questions to InterruptRequest
        itself since from_tool_call() doesn't copy extra fields."""
        request = super()._build_ask_request(tool_call)
        return request

    def extract_questions(
        self, tool_call: Optional[ToolCall]
    ) -> Optional[list[dict]]:
        """Extract questions data from tool call arguments."""
        if tool_call is None:
            return None

        args = tool_call.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                return None

        if isinstance(args, Mapping):
            questions = args.get("questions")
            if questions and isinstance(questions, list) and len(questions) > 0:
                return questions

        return None
