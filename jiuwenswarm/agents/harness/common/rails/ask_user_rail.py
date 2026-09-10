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

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.core.single_agent.interrupt import InterruptRequest
from openjiuwen.core.single_agent.rail import AgentCallbackContext
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRail
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    InterruptDecision,
)
from jiuwenswarm.common.schema.ask_user import (
    AskUserResponseError,
    ask_user_response_schema,
    decode_user_input,
    parse_ask_user_response,
)

logger = logging.getLogger(__name__)

MAX_STRUCTURED_QUESTIONS = 4


def _decode_questions_array(raw_questions: Any) -> tuple[Any, bool]:
    """Accept one accidental JSON encoding layer around a questions array."""
    if not isinstance(raw_questions, str):
        return raw_questions, False
    try:
        decoded = json.loads(raw_questions)
    except (ValueError, TypeError):
        return raw_questions, False
    return (decoded, True) if isinstance(decoded, list) else (raw_questions, False)


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
        "preview": {
            "type": "object",
            "description": (
                "Optional question-level artifact preview. Use this for a shared "
                "outline or document that the choices confirm or edit."
            ),
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string", "minLength": 1},
                "format": {"type": "string", "enum": ["markdown"]},
                "editable": {"type": "boolean"},
                "outline_ref": {"type": "string"},
                "meta": {"type": "object"},
            },
            "required": ["text"],
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
        "return_json": {
            "type": "boolean",
            "default": False,
            "description": (
                "Return the answered structured-question envelope as compact JSON "
                "instead of readable text. Use only when a downstream tool must "
                "preserve status and the canonical answers array."
            ),
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
        "return_json": {
            "type": "boolean",
            "default": False,
            "description": (
                "将结构化问题的回答 envelope 作为紧凑 JSON 返回，而不是可读文本。"
                "仅当下游工具必须保留 status 和标准 answers 数组时使用。"
            ),
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

        All answers use the canonical ``status + answers[]`` response. The
        readable and machine outputs are derived from that single value.
        """
        args = self._parse_tool_args(tool_call)
        return_json = args.get("return_json", False)
        if not isinstance(return_json, bool):
            return self.reject(
                tool_result=(
                    "[INVALID_ARGUMENT] return_json must be a boolean when provided."
                )
            )
        raw_questions, questions_were_decoded = _decode_questions_array(
            args.get("questions")
        )
        if questions_were_decoded:
            args["questions"] = raw_questions
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
            if "preview" in question:
                preview = question["preview"]
                preview_text = (
                    preview.get("text") if isinstance(preview, Mapping) else None
                )
                if not isinstance(preview_text, str) or not preview_text.strip():
                    return self.reject(
                        tool_result=(
                            f"[INVALID_ARGUMENT] questions[{question_index}].preview "
                            "must be an object with non-empty text."
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
            interrupt_tool_call = tool_call
            if tool_call is not None and questions_were_decoded:
                interrupt_tool_call = tool_call.model_copy(
                    update={"arguments": json.dumps(args, ensure_ascii=False)}
                )
            return self.interrupt(self._build_ask_request(interrupt_tool_call))

        # Detect if this was a structured questions call by checking tool_args
        is_structured = questions_data is not None and len(questions_data) > 0
        # user_input is the USER's response, not the model's tool-call args.
        # Any parse failure (non-dict, missing fields, illegal status, non-array
        # answers, etc.) means "the user did not provide a usable structured
        # answer" -> treat as skipped and let the model decide. Do NOT hardcode
        # [INVALID_ARGUMENT] here: that code semantically signals "the MODEL's
        # tool-call args were invalid" and misleads the LLM into thinking the
        # tool is broken, triggering fallback cascades and deadlocks.
        decoded = decode_user_input(user_input)
        try:
            response = parse_ask_user_response(decoded)
        except AskUserResponseError as exc:
            logger.info(
                "[StructuredAskUserRail] user_input parse failed (%s), "
                "treating as skipped",
                exc,
            )
            return self.reject(tool_result=self._build_skipped_tool_result())

        machine_payload = response.to_dict(include_original_request=False)
        if is_structured and return_json:
            return self.reject(
                tool_result=json.dumps(
                    machine_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

        if response.status == "skipped":
            answer_text = "用户已跳过本次问答，未提供回答。"
        elif is_structured:
            answer_text = response.to_readable_text()
        else:
            answer_text = str(
                {
                    answer.question or "__free_text__": answer.readable_value()
                    for answer in response.answers
                }
            )
        if not answer_text:
            answer_text = "用户未提供回答。"
        logger.info(
            "[StructuredAskUserRail] Resolved answer: %s",
            answer_text,
        )
        return self.reject(tool_result=answer_text)

    def _build_ask_request(self, tool_call: Optional[ToolCall]) -> InterruptRequest:
        """Build interrupt request. For structured questions, the questions data
        flows through ToolCallInterruptRequest.tool_args (preserved by the
        interrupt handler). No need to attach questions to InterruptRequest
        itself since from_tool_call() doesn't copy extra fields."""
        request = super()._build_ask_request(tool_call)
        return request.model_copy(
            update={"payload_schema": ask_user_response_schema()}
        )

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
