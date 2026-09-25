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
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    ToolInterruptionState,
)
from openjiuwen.core.single_agent.rail import AgentCallbackContext
from openjiuwen.harness.prompts import resolve_language
from openjiuwen.harness.rails.interrupt.ask_user_rail import (
    AskUserPayload,
    AskUserRail,
)
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    InterruptDecision,
)

from jiuwenswarm.agents.harness.common.rails.ask_user_contract import (
    MAX_STRUCTURED_QUESTIONS,
)

logger = logging.getLogger(__name__)


def _is_saved_pending_tool_call(
    ctx: AgentCallbackContext,
    tool_call: Optional[ToolCall],
) -> bool:
    """Use Core's persisted interruption state to identify sparse replay."""
    session = getattr(ctx, "session", None)
    get_state = getattr(session, "get_state", None)
    if tool_call is None or not callable(get_state):
        return False
    state = get_state(INTERRUPTION_KEY)
    if not isinstance(state, ToolInterruptionState):
        return False
    call_id = str(tool_call.id or "")
    return bool(
        call_id
        and any(
            call_id in entry.interrupt_requests
            for entry in state.interrupted_tools.values()
        )
    )

# ---------------------------------------------------------------------------
# Extended input schema
# ---------------------------------------------------------------------------

# The tightest cap any renderer puts on one question's inputs. A renderer that
# cannot draw a longer question refuses it after the model has written it. The
# schema states the same number so the model does not write a question no
# channel can show.
MAX_QUESTION_INPUTS = 10

# The input types a question may declare, and the whole of what the model is
# told exists.
#
# Seven names, chosen as the ones whose answer any channel can produce. A type
# whose answer is a platform identifier is left out. That identifier means
# nothing on the next channel the question reaches. Such an answer does not
# travel. A renderer may still resolve such a type for a question written for
# one platform. This tuple does not advertise it.
#
# A vocabulary is the expensive thing to withdraw. A name the model has been
# told about is one questions get written against. Adding to this tuple later
# costs nothing. Removing a name from it breaks questions already in the wild.
DECLARED_INPUT_TYPES: tuple[str, ...] = (
    "text",
    "number",
    "date",
    "time",
    "datetime",
    "select",
    "multi_select",
)


def _declares_inputs(question: Mapping[str, Any]) -> bool:
    """Whether a question asks for values to fill in rather than a choice.

    Kept to the one thing the validator needs to know: an ``inputs`` question
    is exempt from carrying its own ``question`` sentence, because its prompt is
    derived from ``header`` or the call's top-level ``query``. An empty array is
    not a declaration -- it asks for nothing, and such a question still needs
    text of its own to mean anything.
    """
    inputs = question.get("inputs")
    return isinstance(inputs, list) and bool(inputs)



def _answer_keys_of(question: Mapping[str, Any], query: str) -> set[str]:
    """Every text an answer to this question can be keyed by.

    An answer arrives keyed by the prompt that the channel showed. The channels
    do not all show the same prompt. A question that has its own
    ``question`` sentence is keyed by that sentence. A question that declares
    only ``inputs`` takes its prompt from ``header``, or from the call's
    top-level ``query``. This function collects all three, because the rail
    cannot tell which channel answered it.
    """
    keys = {
        value.strip()
        for field in ("question", "header")
        for value in (question.get(field),)
        if isinstance(value, str) and value.strip()
    }
    if query.strip():
        keys.add(query.strip())
    return keys


def _resolve_question_prompt(question: Mapping[str, Any], query: str) -> str:
    """The prompt text of one question, in the order every channel resolves it."""
    for candidate in (question.get("question"), question.get("header"), query):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _undelivered_report(
    questions_data: list, answers: Mapping[str, Any], query: str
) -> str:
    """State which questions came back with no answer, or "" when all did.

    The two lengths decide whether to report. A channel that shows one question
    and discards the rest answers the interrupt with one answer, so the call
    gets back fewer answers than it asked questions. That shortfall holds
    whatever the answer keys say.

    The keys then decide whether to name the missing questions. They are used
    only when the questions they fail to match account for the shortfall
    exactly. A different count means the rail cannot match these keys against
    these questions: a free-text resume keys its one answer as
    ``__free_text__``, which matches no question at all. The report then gives
    the counts and no names, because a report that named the wrong questions
    would send the model to ask a question the user already answered.
    """
    asked = len(questions_data)
    answered_count = len(answers)
    shortfall = asked - answered_count
    if shortfall <= 0:
        return ""
    answered = {key.strip() for key in answers if isinstance(key, str)}
    unmatched = [
        (index, _resolve_question_prompt(question, query))
        for index, question in enumerate(questions_data)
        if isinstance(question, Mapping)
        and not (_answer_keys_of(question, query) & answered)
    ]
    lines = [
        f"[NOT_DELIVERED] This ask_user call sent {asked} questions and got "
        f"{answered_count} back."
    ]
    if len(unmatched) == shortfall:
        named = ", ".join(
            f'questions[{index}] "{prompt}"' if prompt else f"questions[{index}]"
            for index, prompt in unmatched
        )
        lines.append(f"These questions got no answer: {named}.")
    else:
        lines.append(f"{shortfall} of them got no answer.")
    lines.append(
        "Some channels show only the first question of a call and discard the "
        "others. Do not tell the user that you asked the missing questions. "
        "Send them again in a new ask_user call. As an alternative, put their "
        "inputs in one question, which every channel delivers whole."
    )
    return " ".join(lines)


def _option_item_schema(text: Mapping[str, str]) -> dict[str, Any]:
    """Build the option schema in one language.

    One object per language, referenced by both ``options`` branches. Kept a
    separate object for the reason it was extracted: the two branches must
    constrain the same element, and Gemini makes each of them say so itself.
    """
    return {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "minLength": 1,
                "description": text["option_label"],
            },
            "description": {
                "type": "string",
                "description": text["option_description"],
            },
            "preview": {
                "type": "string",
                "description": text["option_preview"],
            },
        },
        "required": ["label"],
    }


def _questions_item_schema(text: Mapping[str, str]) -> dict[str, Any]:
    """Build the per-question schema in one language.

    One structure, localized strings. Written as a factory rather than as two
    literals because the English and Chinese schemas must stay the same shape:
    a property added to one and forgotten in the other is a capability half the
    deployments never hear about, and that is exactly the failure this file's
    duplicated ``EXTENDED_INPUT_PARAMS`` pair is prone to.

    "A question carries ``question``, or ``inputs``, or both" is an
    object-level ``anyOf``, and the two flavored validators shape how it is
    written. Moonshot/Kimi refuses any parent holding both ``type`` and
    ``anyOf`` ("type should be defined in anyOf items instead of the parent
    schema"), so ``type`` goes into the branches and this object carries
    ``properties`` and ``anyOf``, with no ``type`` of its own. Google Gemini
    refuses a branch that does not declare what it constrains, which each
    branch does: ``type: object``. The precedent that made ``options`` repeat
    itself was an *array* branch, which has to carry its own ``items``; an
    object branch inherits ``properties`` from this parent, so the block below
    is written once.
    """
    option_item = _option_item_schema(text)
    return {
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "description": text["question"],
            },
            "header": {
                "type": "string",
                "description": text["header"],
            },
            "options": {
                "description": text["options"],
                # Moonshot/Kimi 的 flavored JSON Schema 校验器要求：parent schema 里
                # 不得同时存在 type 与 anyOf，type 必须写进 anyOf 的每个分支
                # （报错 "type should be defined in anyOf items instead of the parent
                # schema"）。语义不变：数组元素由 items 约束（object + required label），
                # 数量由 anyOf 约束（0 个，或 2-4 个）。Google Gemini 还要求
                # 每个 array 分支自己声明 items，不能只在 anyOf 的父级声明。
                "anyOf": [
                    {
                        "type": "array",
                        "items": option_item,
                        "maxItems": 0,
                    },
                    {
                        "type": "array",
                        "items": option_item,
                        "minItems": 2,
                        "maxItems": 4,
                    },
                ],
            },
            "multi_select": {
                "type": "boolean",
                "default": False,
                "description": text["multi_select"],
            },
            "inputs": {
                "type": "array",
                "description": text["inputs"],
                "maxItems": MAX_QUESTION_INPUTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": list(DECLARED_INPUT_TYPES),
                            "description": text["input_type"],
                        },
                        "name": {
                            "type": "string",
                            "description": text["input_name"],
                        },
                        "label": {
                            "type": "string",
                            "description": text["input_label"],
                        },
                        "hint": {
                            "type": "string",
                            "description": text["input_hint"],
                        },
                        "placeholder": {
                            "type": "string",
                            "description": text["input_placeholder"],
                        },
                        "optional": {
                            "type": "boolean",
                            "default": False,
                            "description": text["input_optional"],
                        },
                        "initial": {
                            "description": text["input_initial"],
                        },
                        "multiline": {
                            "type": "boolean",
                            "default": False,
                            "description": text["input_multiline"],
                        },
                        "min": {
                            "type": "number",
                            "description": text["input_min"],
                        },
                        "max": {
                            "type": "number",
                            "description": text["input_max"],
                        },
                        "options": {
                            "type": "array",
                            "description": text["input_options"],
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": text["input_option_label"],
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": text["input_option_value"],
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            text["input_option_description"]
                                        ),
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["type"],
                },
            },
        },
        # ``question`` is required for a question that offers `options`, where
        # the sentence being answered is the whole of what the user is shown,
        # and not for one that declares `inputs`, whose fields carry their own
        # labels and whose prompt falls back to `header` and then the call's
        # top-level `query`. The two branches differ in that alone; the
        # `properties` above apply to both.
        "anyOf": [
            {"type": "object", "required": ["question"]},
            {"type": "object", "required": ["inputs"]},
        ],
    }


_QUESTION_SCHEMA_TEXT_EN: dict[str, str] = {
    "question": (
        "The question to present to the user. Required for a question that "
        "offers `options`. A question that declares `inputs` may omit it: the "
        "prompt shown is this text when given, otherwise `header`, otherwise "
        "the call's top-level `query`."
    ),
    "header": "A short label displayed as a chip/tag.",
    "options": "Available choices for this question (2-4 items).",
    "option_label": "Display text for this option (1-5 words).",
    "option_description": "Explanation of what this option means.",
    "option_preview": (
        "Optional preview content rendered beside this option when comparing "
        "concrete artifacts the user should visually compare (e.g. ASCII "
        "mockups, code snippets). Markdown is supported; use fenced code blocks "
        "for monospace mockups so alignment is preserved. Only rendered for "
        "single-select questions; ignored for multi-select."
    ),
    "multi_select": "Allow multiple selections instead of just one.",
    "inputs": (
        "Values to ask the user to enter, instead of options to choose between. "
        "Use this when the answer is a date, a time, a number or typed text "
        "rather than one of a few named actions -- asking for a date as four "
        "text options is worse than asking for it as a date. "
        "`inputs` and `options` are alternatives: declare one or the other, not "
        "both, and if both are given the inputs are what is asked. "
        "An entry may be a bare type name when there is nothing to say about it "
        "beyond what it is. "
        "A question that declares `inputs` does not need its own `question`: "
        "the prompt shown is `question` when given, otherwise `header`, "
        "otherwise the call's top-level `query`. "
        "The answer comes back as one value per input, in the order declared."
    ),
    "input_type": (
        "What to ask for:\n"
        "- text: typed text. Answered with what was typed. Set `multiline` for "
        "more than one line.\n"
        "- number: a typed number. Answered with the number as written; "
        "something that is not a number is refused before it reaches you. "
        "`min` and `max` bound it.\n"
        "- date: a calendar date. Answered as YYYY-MM-DD.\n"
        "- time: a time of day with no date. Answered as HH:MM:SS, followed by "
        "the answerer's IANA timezone in brackets when it is known. There is "
        "deliberately no UTC offset: a time with no date does not have one.\n"
        "- datetime: a date and a time together. Answered as one ISO 8601 "
        "instant with the timezone name appended, e.g. "
        "2026-08-20T09:30:00+02:00[Europe/Paris]. Prefer this over asking for a "
        "date and a time as two separate inputs, which cannot be resolved to a "
        "single instant.\n"
        "- select: one choice from `options`. Answered with the chosen option's "
        "`value`. Prefer this over `options` when the question also asks for "
        "something entered.\n"
        "- multi_select: any number of choices from `options`. Answered with "
        "one value per choice."
    ),
    "input_name": (
        "Names this value in the answer. Defaults to the type, which is enough "
        "when a question asks for one thing. Required to be distinct when a "
        "question asks for two of the same type."
    ),
    "input_label": (
        "What the field is called where the user sees it. Defaults to the "
        "input's name. A `datetime` shows two fields; the second is named with "
        "`time_label`."
    ),
    "input_hint": "A short line of guidance shown beneath the field.",
    "input_placeholder": "Greyed-out text shown in an empty field.",
    "input_optional": (
        "Let the user submit without filling this in. By default every declared "
        "input must be answered, and a submit missing one is refused with a "
        "note saying which."
    ),
    "input_initial": (
        "The value the field starts at. For `datetime`, use `initial_date` and "
        "`initial_time` instead, since it is two fields. For `select` it must "
        "be one of the declared option values."
    ),
    "input_multiline": "For `text`: accept more than one line.",
    "input_min": "For `number`: the smallest value accepted.",
    "input_max": "For `number`: the largest value accepted.",
    "input_options": (
        "For `select` and `multi_select`: what there is to choose from. "
        "Required for those types and ignored for every other."
    ),
    "input_option_label": "Display text for this choice.",
    "input_option_value": (
        "What the answer carries when this choice is made. Defaults to the "
        "label."
    ),
    "input_option_description": "Explanation of what this choice means.",
}

_QUESTION_SCHEMA_TEXT_CN: dict[str, str] = {
    "question": (
        "向用户展示的问题。提供 `options` 的问题必须填写。"
        "声明了 `inputs` 的问题可以不填：展示的提示语依次取本字段、`header`、"
        "调用顶层的 `query`。"
    ),
    "header": "作为标签展示的简短标题。",
    "options": "该问题的可选项（2-4 个）。",
    "option_label": "该选项的展示文本（1-5 个词）。",
    "option_description": "说明该选项的含义。",
    "option_preview": (
        "可选的预览内容，展示在该选项旁，用于对比用户需要直观比较的具体产物"
        "（如 ASCII mockup、代码片段）。支持 markdown；"
        "等宽 mockup 请使用围栏代码块以保持对齐。仅单选问题会渲染，多选问题忽略。"
    ),
    "multi_select": "允许多选而非单选。",
    "inputs": (
        "请用户填写的值，用于替代让用户在选项间选择。"
        "当答案是日期、时间、数字或自由文本，而不是少数几个具名动作之一时使用；"
        "把日期拆成四个文本选项来问，不如直接按日期来问。"
        "`inputs` 与 `options` 二选一：只声明其中之一；若两者都给出，则以 inputs 为准。"
        "当某一项除类型外无需额外说明时，可直接写类型名字符串。"
        "声明了 `inputs` 的问题无需再写 `question`："
        "展示的提示语依次取 `question`、`header`、调用顶层的 `query`。"
        "答案按声明顺序，每个输入返回一个值。"
    ),
    "input_type": (
        "要询问的内容：\n"
        "- text：自由文本。返回用户输入的内容。需要多行时设置 `multiline`。\n"
        "- number：数字。按输入原样返回；非数字会在到达你之前被拒绝。"
        "可用 `min`、`max` 限定范围。\n"
        "- date：日历日期。返回格式为 YYYY-MM-DD。\n"
        "- time：不带日期的时刻。返回格式为 HH:MM:SS，已知时区时在方括号中附上"
        "回答者的 IANA 时区。刻意不带 UTC 偏移：没有日期的时刻本就没有偏移。\n"
        "- datetime：日期与时刻一起询问。返回一个 ISO 8601 时间点，并附上时区名，"
        "例如 2026-08-20T09:30:00+02:00[Europe/Paris]。"
        "优先使用它，而不是拆成 date 和 time 两个输入——那样无法还原为同一个时间点。\n"
        "- select：从 `options` 中单选。返回所选项的 `value`。"
        "当问题同时还需要填写内容时，优先用它而不是 `options`。\n"
        "- multi_select：从 `options` 中多选。每选中一项返回一个值。"
    ),
    "input_name": (
        "该值在答案中的名称。默认取类型名，单个输入的问题无需另行命名。"
        "同一问题中出现两个同类型输入时，必须分别命名以示区分。"
    ),
    "input_label": (
        "用户看到的字段名称。默认取该输入的名称。"
        "`datetime` 会显示两个字段，第二个用 `time_label` 命名。"
    ),
    "input_hint": "显示在字段下方的简短提示。",
    "input_placeholder": "字段为空时显示的浅色占位文本。",
    "input_optional": (
        "允许用户不填写该项即可提交。默认每个声明的输入都必须填写，"
        "缺项的提交会被拒绝，并提示缺少哪一项。"
    ),
    "input_initial": (
        "字段的初始值。`datetime` 由两个字段组成，请改用 `initial_date` 与 "
        "`initial_time`。`select` 的初始值必须是已声明的某个选项值。"
    ),
    "input_multiline": "用于 `text`：允许多行输入。",
    "input_min": "用于 `number`：允许的最小值。",
    "input_max": "用于 `number`：允许的最大值。",
    "input_options": (
        "用于 `select` 与 `multi_select`：可供选择的内容。"
        "这两种类型必须提供，其他类型会被忽略。"
    ),
    "input_option_label": "该选项的展示文本。",
    "input_option_value": "选中该项时答案携带的值。默认取 label。",
    "input_option_description": "说明该选项的含义。",
}

_QUESTIONS_ITEM_SCHEMA_EN: dict[str, Any] = _questions_item_schema(
    _QUESTION_SCHEMA_TEXT_EN
)
_QUESTIONS_ITEM_SCHEMA_CN: dict[str, Any] = _questions_item_schema(
    _QUESTION_SCHEMA_TEXT_CN
)


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
                "The user can always select 'Other' for custom input. "
                "A question may instead declare `inputs` -- values to enter, "
                "such as a date or a number -- when the answer is not one of a "
                "few named actions."
            ),
            "items": _QUESTIONS_ITEM_SCHEMA_EN,
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
                "当答案不是少数几个具名动作之一时，问题也可以改为声明 `inputs`——"
                "即需要用户填写的值，例如日期或数字。"
            ),
            "items": _QUESTIONS_ITEM_SCHEMA_CN,
            "maxItems": MAX_STRUCTURED_QUESTIONS,
        },
    },
    "required": ["query"],
}

_EXTENDED_DESCRIPTION_EN: str = (
    "Interrupts execution and requests input from the user. "
    "Supports three modes:\n"
    "1. Plain query (free-text): pass only `query` — the user types their answer.\n"
    "2. Structured questions (multi-choice): pass `query` + `questions` — "
    "the user selects from predefined options. "
    "3. Entered values: pass `query` + `questions`, each question declaring "
    "`inputs` — the user fills in a date, a time, a number, text, or a choice "
    "from a list, and answers them together. Use this when the answer is a "
    "value rather than one of a few named actions; asking for a date as four "
    "text options is worse than asking for it as a date. A question that "
    "declares `inputs` needs no `question` text of its own — `header`, or the "
    "top-level `query`, is the prompt. "
    "Use `questions` when you want the user to choose between specific options "
    "(e.g., 'Apply update' vs 'Skip'). Ask at most 4 questions per call. "
    "Omit options for free-text input; otherwise provide 2-4 options. "
    "For single-select questions, an option may carry a `preview` (markdown, "
    "e.g. fenced code block ASCII mockup) shown beside it to compare concrete "
    "artifacts; use it only when a visual comparison helps the user decide."
)

_EXTENDED_DESCRIPTION_CN: str = (
    "中断执行并向用户请求输入。支持三种模式：\n"
    "1. 纯文本查询：只传 `query` —— 用户自由输入回答。\n"
    "2. 结构化选项：传 `query` + `questions` —— 用户从预定义选项中选择。"
    "3. 填写具体值：传 `query` + `questions`，并在问题中声明 `inputs` —— "
    "用户填写日期、时刻、数字、文本或从列表中选择，并一并提交。"
    "当答案是一个值而非少数几个具名动作之一时使用；"
    "把日期拆成四个文本选项来问，不如直接按日期来问。"
    "声明了 `inputs` 的问题无需再写 `question`——"
    "提示语取 `header`，或顶层的 `query`。"
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
            EXTENDED_INPUT_PARAMS_EN if language == "en" else EXTENDED_INPUT_PARAMS_CN
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
        *,
        strict_continuation_contract: bool = False,
    ):
        super().__init__(tool_names=tool_names)
        self._structured_tools: list[StructuredAskUserTool] = []
        self._language = language
        self.set_strict_continuation_contract(strict_continuation_contract)

    def set_strict_continuation_contract(self, enabled: bool) -> None:
        """Enable the stricter Smart Approval continuation input contract."""
        if not isinstance(enabled, bool):
            raise TypeError("strict continuation contract flag must be bool")
        self._strict_continuation_contract = enabled

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

        每条拒绝信息都必须给出应当改成什么，而不只是指出哪里错了。

        Every rejection below states the remedy as well as the fault. A
        rejection from here never reaches a person: it is handed back to the
        model as the tool result, and the model's only move is to call the tool
        again. A message that names what was wrong without naming what to send
        instead gives it nothing new to write, so the retry carries identical
        arguments -- and repeated identical tool calls are what the loop
        detector ends the run over, with its abort text delivered to the user in
        place of an answer. Quoting the shape to send is what breaks that cycle,
        so a rejection added later should quote one too.
        """
        strict_initial_request = (
            self._strict_continuation_contract
            and user_input is None
            and not _is_saved_pending_tool_call(ctx, tool_call)
        )
        args = self._parse_tool_args(tool_call)
        raw_questions = args.get("questions")
        if "questions" in args and not isinstance(raw_questions, list):
            return self.reject(
                tool_result=(
                    "[INVALID_ARGUMENT] questions must be an array of question "
                    "objects. Send it as a JSON array, e.g. questions: "
                    '[{"question": "Which one?", "options": [{"label": "A"}, '
                    '{"label": "B"}]}]. To ask a single free-text question '
                    "instead, omit questions entirely and send only query."
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
                    "Call ask_user again with the first "
                    f"{MAX_STRUCTURED_QUESTIONS} questions, and ask the "
                    "remaining ones in a further call once these are answered."
                )
            )

        for question_index, question in enumerate(questions_data or []):
            if not isinstance(question, Mapping):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}] "
                        "must be an object. Replace it with an object carrying "
                        "the question text, plus either options (2-4 choices to "
                        "pick between) or inputs (values to fill in), e.g. "
                        '{"question": "Which one?", "options": '
                        '[{"label": "A"}, {"label": "B"}]}.'
                    )
                )
            # A question that declares `inputs` derives its prompt downstream:
            # its own `question` when present, else `header`, else the call's
            # top-level `query`. Requiring `question` here rejected a shape the
            # model kept producing, and a rejection it cannot act on is retried
            # byte-identically until the tool-loop detector aborts the run.
            declares_inputs = _declares_inputs(question)
            if "inputs" in question and not isinstance(question["inputs"], list):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].inputs "
                        "must be an array of input objects. Send it as a JSON "
                        'array, e.g. inputs: [{"type": "date", "label": '
                        '"Date"}, {"type": "text", "label": "Note"}].'
                    )
                )
            question_text = question.get("question")
            has_question_text = (
                isinstance(question_text, str) and bool(question_text.strip())
            )
            if not has_question_text and not declares_inputs:
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}] needs "
                        "either a non-empty question string or an inputs array. "
                        "Add question with the sentence to put to the user, or "
                        "-- when the answer is a value to fill in rather than a "
                        'choice -- declare inputs, e.g. inputs: [{"type": '
                        '"date", "label": "Date"}]; an inputs question takes '
                        "its prompt from header, or from the top-level query."
                    )
                )
            if "header" in question and not isinstance(question["header"], str):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].header "
                        "must be a string when provided. Send a short label of "
                        'a few words, e.g. header: "Deployment", or drop the '
                        "field."
                    )
                )
            if "options" not in question:
                continue
            options = question["options"]
            if not isinstance(options, list):
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].options "
                        "must be an array of option objects. Send it as a JSON "
                        'array, e.g. options: [{"label": "Yes"}, '
                        '{"label": "No"}]. To let the user answer in their own '
                        "words instead, omit options entirely; to ask for a "
                        "typed or picked value rather than a choice, declare "
                        "inputs in place of options."
                    )
                )
            canonical_labels: set[str] = set()
            for option_index, option in enumerate(options):
                label = option.get("label") if isinstance(option, Mapping) else None
                if not isinstance(label, str) or not label.strip():
                    path = f"questions[{question_index}].options[{option_index}].label"
                    return self.reject(
                        tool_result=(
                            f"[INVALID_ARGUMENT] {path} is required "
                            "and must be a non-empty string. Give the option "
                            "the 1-5 words the user should see, e.g. "
                            '{"label": "Apply update"}.'
                        )
                    )
                if strict_initial_request:
                    canonical_label = label.strip()
                    description = option.get("description", "")
                    preview = option.get("preview", "")
                    if not isinstance(description, str) or not isinstance(preview, str):
                        return self.reject(
                            tool_result=(
                                f"[INVALID_ARGUMENT] questions[{question_index}]."
                                f"options[{option_index}] description and preview must "
                                "be strings when provided."
                            )
                        )
                    if canonical_label == "Other" or canonical_label in canonical_labels:
                        return self.reject(
                            tool_result=(
                                f"[INVALID_ARGUMENT] questions[{question_index}].options "
                                "must use unique labels and must not use the reserved "
                                "label 'Other'."
                            )
                        )
                    canonical_labels.add(canonical_label)
            if options and not 2 <= len(options) <= 4:
                return self.reject(
                    tool_result=(
                        f"[INVALID_ARGUMENT] questions[{question_index}].options "
                        "must contain either 0 or 2-4 items; "
                        f"received {len(options)}. Merge or drop choices until "
                        "at most 4 remain, ask the rest in a further ask_user "
                        "call, omit options entirely to let the user answer in "
                        "their own words, or -- when the answer is a value "
                        "rather than a choice -- replace options with inputs."
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
                        answer_parts.append(
                            selected
                            if isinstance(selected, str)
                            else ", ".join(selected)
                        )
                    else:
                        value_text = (
                            selected
                            if isinstance(selected, str)
                            else ", ".join(selected)
                        )
                        answer_parts.append(f"{q_text}: {value_text}")
                answer_text = "\n".join(answer_parts) if answer_parts else ""
                if not answer_text.strip():
                    # Empty resume (e.g. bare Other) must not look like a valid answer (#2330).
                    return self.reject(
                        tool_result=(
                            "[INVALID_ARGUMENT] answers must include at least "
                            "one non-empty response; the user submitted "
                            "nothing. Do not treat this as an answer: either "
                            "call ask_user again with the same question, or "
                            "continue without it and say what you assumed."
                        )
                    )
                logger.info(
                    "[StructuredAskUserRail] Resolved structured answer: %s",
                    answer_text,
                )
                # The model is told, because only the model can act on this.
                # A channel that shows one question of several answers the
                # interrupt with that one answer and says nothing about the
                # rest. Without this line the call reads as fully answered, and
                # the model then reports to the user that every question was
                # asked. That report describes what the model sent. It does not
                # describe what the user saw.
                undelivered = _undelivered_report(
                    questions_data, payload.answers, str(args.get("query") or "")
                )
                if undelivered:
                    logger.warning(
                        "[StructuredAskUserRail] %d of %d questions got no "
                        "answer: %s",
                        len(questions_data) - len(payload.answers),
                        len(questions_data),
                        undelivered,
                    )
                    answer_text = f"{answer_text}\n\n{undelivered}"
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

    def extract_questions(self, tool_call: Optional[ToolCall]) -> Optional[list[dict]]:
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
