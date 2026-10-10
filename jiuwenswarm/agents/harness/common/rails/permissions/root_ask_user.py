# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact ordinary ``ask_user`` continuation owned by the root Host."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

from jiuwenswarm.agents.harness.common.rails.ask_user_contract import (
    MAX_STRUCTURED_QUESTIONS,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    AUTO_REVIEW_BLOCK_INTENT_INPUT_TOO_LARGE,
    ROOT_INTENT_MAX_TURN_CHARS,
    RootAskUserClarification,
    RootAskUserOption,
    RootDecisionContext,
    append_root_clarification,
    root_intent_turn_text,
    RootIntentTurn,
    RootIntentTurnKind,
)

ASK_USER_TOOL_NAME = "ask_user"
ASK_USER_CONTINUATION_METADATA_KEY = "jiuwenswarm_root_ask_user_v1"
ASK_USER_RESUME_DTO_KEY = "jiuwenswarm.root_ask_user_resume.v1"
_MAX_ANSWERS = 8


@dataclass(frozen=True, slots=True)
class AskUserQuestionReference:
    question: str
    options: tuple[RootAskUserOption, ...]
    multi_select: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": [option.to_mapping() for option in self.options],
            "multi_select": self.multi_select,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> AskUserQuestionReference:
        if not isinstance(value, Mapping) or set(value) != {
            "question",
            "options",
            "multi_select",
        }:
            raise ValueError("invalid ask-user question")
        question, raw_options, multi_select = (
            value["question"],
            value["options"],
            value["multi_select"],
        )
        if not isinstance(question, str) or not question.strip():
            raise ValueError("invalid ask-user question")
        if not isinstance(raw_options, list) or not isinstance(multi_select, bool):
            raise ValueError("invalid ask-user question")
        return cls(
            question,
            tuple(RootAskUserOption.from_mapping(item) for item in raw_options),
            multi_select,
        )


@dataclass(frozen=True, slots=True)
class RootAskUserContinuation:
    tool_call_id: str
    context: RootDecisionContext
    questions: tuple[AskUserQuestionReference, ...]


@dataclass(frozen=True, slots=True)
class RootAskUserResume:
    continuation: RootAskUserContinuation
    clarifications: tuple[RootAskUserClarification, ...] | None
    capacity_reason: str = ""


def put_ask_user_resume_in_inputs(
    inputs: Mapping[str, Any], prepared: RootAskUserResume | None
) -> dict[str, Any]:
    result = dict(inputs)
    run = dict(result.get("run")) if isinstance(result.get("run"), Mapping) else {}
    context = dict(run.get("context")) if isinstance(run.get("context"), Mapping) else {}
    extra = dict(context.get("extra")) if isinstance(context.get("extra"), Mapping) else {}
    extra.pop(ASK_USER_RESUME_DTO_KEY, None)
    if prepared is not None:
        extra[ASK_USER_RESUME_DTO_KEY] = prepared
    context["extra"] = extra
    run["context"] = context
    result["run"] = run
    return result


def build_ask_user_metadata(
    *,
    context: RootDecisionContext,
    tool_name: Any,
    tool_call_id: Any,
    tool_args: Any,
) -> dict[str, Any] | None:
    call_id = str(tool_call_id or "").strip()
    questions = _questions(tool_args)
    if tool_name != ASK_USER_TOOL_NAME or not call_id:
        return None
    if len(call_id) > 512 or not questions:
        return None
    return {
        "tool_call_id": call_id,
        "context": context.to_mapping(),
        "questions": [question.to_mapping() for question in questions],
    }


def ask_user_continuation(
    metadata: Any, *, expected_tool_call_id: str
) -> RootAskUserContinuation | None:
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(ASK_USER_CONTINUATION_METADATA_KEY)
    if not isinstance(raw, Mapping) or set(raw) != {
        "tool_call_id",
        "context",
        "questions",
    }:
        return None
    call_id, raw_questions = raw["tool_call_id"], raw["questions"]
    if call_id != expected_tool_call_id or not isinstance(raw_questions, list):
        return None
    try:
        context = RootDecisionContext.from_mapping(raw["context"])
        questions = tuple(
            AskUserQuestionReference.from_mapping(item) for item in raw_questions
        )
    except (TypeError, ValueError):
        return None
    if not 1 <= len(questions) <= MAX_STRUCTURED_QUESTIONS:
        return None
    texts = [question.question for question in questions]
    return None if len(texts) != len(set(texts)) else RootAskUserContinuation(call_id, context, questions)


def prepare_ask_user_resume(
    *, continuation: RootAskUserContinuation, user_input: Any
) -> RootAskUserResume | None:
    if not isinstance(user_input, InteractiveInput) or user_input.raw_inputs is not None:
        return None
    if set(user_input.user_inputs) != {continuation.tool_call_id}:
        return None
    payload = user_input.user_inputs[continuation.tool_call_id]
    if not isinstance(payload, Mapping) or set(payload) != {"answers"}:
        return None
    answers = payload["answers"]
    if not isinstance(answers, Mapping) or set(answers) != {
        question.question for question in continuation.questions
    }:
        return None
    clarifications: list[RootAskUserClarification] = []
    for question in continuation.questions:
        values = _answer_values(
            answers[question.question], multi_select=question.multi_select
        )
        if values is None:
            return None
        clarifications.append(
            RootAskUserClarification(question.question, values, question.options)
        )
    turn = RootIntentTurn(
        continuation.context.request_id,
        RootIntentTurnKind.ASK_USER_CLARIFICATION,
        clarifications=tuple(clarifications),
    )
    if len(root_intent_turn_text(turn, include_question=True)) > ROOT_INTENT_MAX_TURN_CHARS:
        return RootAskUserResume(
            continuation,
            None,
            AUTO_REVIEW_BLOCK_INTENT_INPUT_TOO_LARGE,
        )
    return RootAskUserResume(continuation, tuple(clarifications))


def apply_ask_user_resume(prepared: RootAskUserResume) -> RootDecisionContext:
    if prepared.capacity_reason:
        return replace(
            prepared.continuation.context,
            auto_review_block_reason=prepared.capacity_reason,
        )
    if prepared.clarifications is None:
        raise ValueError("ask-user clarification missing")
    return append_root_clarification(
        prepared.continuation.context,
        prepared.clarifications,
    )


def _questions(tool_args: Any) -> tuple[AskUserQuestionReference, ...]:
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (TypeError, ValueError):
            return ()
    if not isinstance(tool_args, Mapping):
        return ()
    raw_questions = tool_args.get("questions")
    if raw_questions is None or raw_questions == []:
        query = tool_args.get("query")
        return (
            (AskUserQuestionReference(query, (), False),)
            if isinstance(query, str) and query.strip()
            else ()
        )
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_STRUCTURED_QUESTIONS:
        return ()
    result: list[AskUserQuestionReference] = []
    try:
        for raw in raw_questions:
            if not isinstance(raw, Mapping):
                return ()
            question = raw.get("question")
            multi_select = raw.get("multi_select", False)
            options = _options(raw.get("options", []))
            if not isinstance(question, str) or not question.strip():
                return ()
            if not isinstance(multi_select, bool) or options is None:
                return ()
            result.append(AskUserQuestionReference(question, options, multi_select))
    except ValueError:
        return ()
    texts = [item.question for item in result]
    return tuple(result) if len(texts) == len(set(texts)) else ()


def _options(value: Any) -> tuple[RootAskUserOption, ...] | None:
    if not isinstance(value, list) or (value and not 2 <= len(value) <= 4):
        return None
    result: list[RootAskUserOption] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            return None
        result.append(
            RootAskUserOption(
                raw.get("label"),
                raw.get("description", ""),
                raw.get("preview", ""),
            )
        )
    labels = [option.label for option in result]
    return None if len(labels) != len(set(labels)) or "Other" in labels else tuple(result)


def _answer_values(value: Any, *, multi_select: bool) -> tuple[str, ...] | None:
    values = value if isinstance(value, list) else [value]
    if not values or len(values) > _MAX_ANSWERS:
        return None
    if not multi_select and len(values) != 1:
        return None
    if any(not isinstance(item, str) or not item.strip() for item in values):
        return None
    return tuple(item.strip() for item in values)


__all__ = [
    "ASK_USER_CONTINUATION_METADATA_KEY",
    "ASK_USER_RESUME_DTO_KEY",
    "ASK_USER_TOOL_NAME",
    "RootAskUserContinuation",
    "RootAskUserResume",
    "apply_ask_user_resume",
    "ask_user_continuation",
    "build_ask_user_metadata",
    "prepare_ask_user_resume",
    "put_ask_user_resume_in_inputs",
]
