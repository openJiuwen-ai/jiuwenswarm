# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical AskUser response contract shared by adapters, rails, and tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


_OTHER_OPTION_LABELS = frozenset({"Other", "其他"})


class AskUserResponseError(ValueError):
    """Raised when an AskUser response violates the canonical contract."""


@dataclass(frozen=True)
class AskUserAnswer:
    """One normalized answer item."""

    question: str
    selected_options: tuple[str, ...]
    custom_input: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "selected_options": list(self.selected_options),
            "custom_input": self.custom_input,
        }

    def readable_value(self) -> str:
        values = [*self.selected_options]
        if self.custom_input:
            values.append(self.custom_input)
        return ", ".join(values)


@dataclass(frozen=True)
class AskUserResponse:
    """The only internal representation of an AskUser response."""

    status: Literal["answered", "skipped"]
    answers: tuple[AskUserAnswer, ...]
    original_request: str | None = None

    def to_dict(self, *, include_original_request: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "answers": [answer.to_dict() for answer in self.answers],
        }
        if include_original_request and self.original_request:
            payload["original_request"] = self.original_request
        return payload

    def to_readable_text(self) -> str:
        parts = []
        for answer in self.answers:
            value = answer.readable_value()
            if answer.question:
                parts.append(f"{answer.question}: {value}")
            else:
                parts.append(value)
        return "\n".join(parts)


def ask_user_response_schema() -> dict[str, Any]:
    """Return the public JSON schema for the canonical resume response."""

    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["answered", "skipped"],
            },
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "question": {"type": "string"},
                        "selected_options": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "custom_input": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ],
                        },
                    },
                    "required": ["selected_options"],
                },
            },
            "original_request": {"type": "string"},
        },
        "required": ["status", "answers"],
    }


def _normalize_answer(item: Any, index: int) -> AskUserAnswer | None:
    if not isinstance(item, Mapping):
        raise AskUserResponseError(f"answers[{index}] must be an object")

    raw_question = item.get("question", "")
    if not isinstance(raw_question, str):
        raise AskUserResponseError(f"answers[{index}].question must be a string")
    question = raw_question.strip()

    if "selected_options" not in item:
        raise AskUserResponseError(
            f"answers[{index}].selected_options is required"
        )
    raw_selected = item["selected_options"]
    if not isinstance(raw_selected, list):
        raise AskUserResponseError(
            f"answers[{index}].selected_options must be an array"
        )
    selected_options: list[str] = []
    for option_index, option in enumerate(raw_selected):
        if not isinstance(option, str):
            raise AskUserResponseError(
                f"answers[{index}].selected_options[{option_index}] "
                "must be a string"
            )
        normalized_option = option.strip()
        if normalized_option and normalized_option not in _OTHER_OPTION_LABELS:
            selected_options.append(normalized_option)

    raw_custom = item.get("custom_input")
    if raw_custom is not None and not isinstance(raw_custom, str):
        raise AskUserResponseError(
            f"answers[{index}].custom_input must be a string or null"
        )
    custom_input = raw_custom.strip() if isinstance(raw_custom, str) else None
    if not custom_input:
        custom_input = None

    if not selected_options and custom_input is None:
        return None
    return AskUserAnswer(
        question=question,
        selected_options=tuple(selected_options),
        custom_input=custom_input,
    )


def normalize_ask_user_response(
    *,
    status: Any,
    answers: Any,
    original_request: Any = None,
) -> AskUserResponse:
    """Normalize the current array protocol into one semantic response."""

    if not isinstance(status, str):
        raise AskUserResponseError("status must be a string")
    normalized_status = status.strip().lower()
    if normalized_status not in {"", "answered", "skipped"}:
        raise AskUserResponseError(
            "status must be 'answered' or 'skipped' when provided"
        )
    if not isinstance(answers, list):
        raise AskUserResponseError("answers must be an array")
    if original_request is not None and not isinstance(original_request, str):
        raise AskUserResponseError("original_request must be a string when provided")

    normalized_answer_items: list[AskUserAnswer] = []
    for index, item in enumerate(answers):
        answer = _normalize_answer(item, index)
        if answer is not None:
            normalized_answer_items.append(answer)
    normalized_answers = tuple(normalized_answer_items)
    if normalized_status == "skipped" and normalized_answers:
        raise AskUserResponseError("skipped response must not contain user input")

    effective_status: Literal["answered", "skipped"] = (
        "skipped" if normalized_status == "skipped" else "answered"
    )
    normalized_original_request = (
        original_request.strip() if isinstance(original_request, str) else ""
    )
    return AskUserResponse(
        status=effective_status,
        answers=normalized_answers,
        original_request=normalized_original_request or None,
    )


def parse_ask_user_response(value: Any) -> AskUserResponse:
    """Parse the canonical internal mapping without legacy fallbacks."""

    if not isinstance(value, Mapping):
        raise AskUserResponseError("AskUser response must be an object")
    fields = set(value)
    if not {"status", "answers"}.issubset(fields):
        raise AskUserResponseError("AskUser response must contain status and answers")
    return normalize_ask_user_response(
        status=value["status"],
        answers=value["answers"],
        original_request=value.get("original_request"),
    )


def decode_user_input(value: Any) -> Any:
    """Decode user_input into a canonical form before strict parsing.

    Handles two accidental encodings that leak into resolve_interrupt:
    1. JSON string (double-encoded by transport layer) -> dict via json.loads.
    2. pydantic model with model_dump -> dict.

    Anything else is returned as-is so callers (e.g. StructuredAskUserRail)
    can treat non-Mapping values as "user did not provide a structured answer"
    rather than raising INVALID_ARGUMENT on the model's behalf.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    return value
