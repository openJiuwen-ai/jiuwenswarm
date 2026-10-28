# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict transport-only answer/cancel records for one duplex command.

Controls carry correlation and answer values, never Runtime construction or
host configuration. The execution adapter resolves the matching pending
interaction and constructs Runtime inputs from its own trusted state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from jiuwenswarm.channels.process_cli.machine_io import (
    MachineInputError,
    decode_machine_document,
)
from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    require_supported_schema_version,
)


_CANCEL_FIELDS = frozenset({"schema_version", "type", "request_id", "session_id"})
_ANSWER_FIELDS = _CANCEL_FIELDS | frozenset({"interaction_id", "answers"})
_TOOL_RESULT_FIELDS = _CANCEL_FIELDS | frozenset({"call_id", "result", "error"})
_COMMON_REQUIRED = frozenset({"schema_version", "type", "request_id"})


class DuplexProtocolError(ValueError):
    """Safe control diagnostic with optional usable request correlation."""

    code = "INVALID_CONTROL"

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.details = details or {}


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DuplexProtocolError(f"{name} must be a non-empty string")
    if not value.strip():
        raise DuplexProtocolError(f"{name} must be a non-empty string")
    return value.strip()


def _copy_json(value: object) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DuplexProtocolError("answers must contain only finite JSON numbers")
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DuplexProtocolError("answers object keys must be strings")
            result[key] = _copy_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    raise DuplexProtocolError("answers must contain only JSON values")


def _validate_answer_fields(answer: dict[str, Any]) -> None:
    """Check public answer fields while retaining existing JSON extensions."""
    for name in ("question", "custom_input"):
        if name in answer and not isinstance(answer[name], str):
            raise DuplexProtocolError(f"answers.{name} must be a string")
    if "card_id" in answer:
        _required_text("answers.card_id", answer["card_id"])
    if "selected_options" in answer:
        options = answer["selected_options"]
        if not isinstance(options, (list, tuple)) or any(
            not isinstance(option, str) for option in options
        ):
            raise DuplexProtocolError(
                "answers.selected_options must be an array of strings"
            )


def _copy_answers(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise DuplexProtocolError("answers must be a non-empty array of objects")
    if not value:
        raise DuplexProtocolError("answers must be a non-empty array of objects")
    copied: list[dict[str, Any]] = []
    try:
        for answer in value:
            if not isinstance(answer, dict):
                raise DuplexProtocolError("answers entries must be objects")
            _validate_answer_fields(answer)
            copied.append(_copy_json(answer))
    except RecursionError:
        raise DuplexProtocolError("answers nesting is invalid") from None
    return tuple(copied)


@dataclass(frozen=True, slots=True, kw_only=True)
class DuplexControl:
    """A correlated answer or cancellation in the current command process.

    Envelope fields are frozen. Answer dictionaries are defensive copies,
    and ``to_dict`` returns fresh JSON data so caller mutations cannot affect
    a previously decoded record through its source or serialized output.
    """

    kind: Literal["answer", "cancel", "tool_result"]
    request_id: str
    session_id: str | None = None
    interaction_id: str | None = None
    answers: tuple[dict[str, Any], ...] = ()
    call_id: str | None = None
    result: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("answer", "cancel", "tool_result"):
            raise DuplexProtocolError(
                "control type must be answer, tool_result or cancel"
            )
        object.__setattr__(
            self, "request_id", _required_text("request_id", self.request_id)
        )
        if self.kind == "answer":
            object.__setattr__(
                self, "session_id", _required_text("session_id", self.session_id)
            )
            object.__setattr__(
                self,
                "interaction_id",
                _required_text("interaction_id", self.interaction_id),
            )
            object.__setattr__(self, "answers", _copy_answers(self.answers))
        elif self.kind == "tool_result":
            object.__setattr__(
                self, "session_id", _required_text("session_id", self.session_id)
            )
            object.__setattr__(self, "call_id", _required_text("call_id", self.call_id))
            if self.interaction_id is not None or self.answers != ():
                raise DuplexProtocolError("tool_result must not contain answer fields")
            if self.error is not None:
                object.__setattr__(self, "error", _required_text("error", self.error))
            else:
                object.__setattr__(self, "result", _copy_json(self.result))
        else:
            if self.session_id is not None:
                object.__setattr__(
                    self, "session_id", _required_text("session_id", self.session_id)
                )
            has_answer_fields = self.interaction_id is not None or self.answers != ()
            has_tool_fields = self.call_id is not None or self.error is not None
            if has_answer_fields or has_tool_fields:
                raise DuplexProtocolError(
                    "cancel must not contain answer fields or tool fields"
                )

    def to_dict(self) -> dict[str, Any]:
        """Return this versioned control as fresh, JSON-compatible data."""

        record: dict[str, Any] = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "type": self.kind,
            "request_id": self.request_id,
            "session_id": self.session_id,
        }
        if self.kind == "answer":
            record["interaction_id"] = self.interaction_id
            record["answers"] = list(_copy_answers(self.answers))
        elif self.kind == "tool_result":
            record["call_id"] = self.call_id
            if self.error is None:
                record["result"] = _copy_json(self.result)
            else:
                record["error"] = self.error
        return record


def _validate_fields(record: dict[str, Any]) -> None:
    kind = record.get("type")
    if kind == "answer":
        allowed = _ANSWER_FIELDS
        required = _ANSWER_FIELDS
    elif kind == "cancel":
        allowed = _CANCEL_FIELDS
        required = _COMMON_REQUIRED
    elif kind == "tool_result":
        allowed = _TOOL_RESULT_FIELDS
        required = _CANCEL_FIELDS | frozenset({"call_id"})
        if ("result" in record) == ("error" in record):
            raise DuplexProtocolError(
                "tool_result requires exactly one result or error"
            )
    else:
        raise DuplexProtocolError("control type must be answer, tool_result or cancel")
    fields = set(record)
    if fields - allowed:
        raise DuplexProtocolError("control contains unknown fields")
    if required - fields:
        raise DuplexProtocolError("control is missing required fields")
    try:
        require_supported_schema_version(record["schema_version"])
    except (TypeError, ValueError):
        raise DuplexProtocolError("schema_version is not supported") from None


def decode_control(line: bytes) -> DuplexControl:
    """Decode one strict control; a second run record is never accepted."""

    if not isinstance(line, bytes):
        raise DuplexProtocolError("control input must be UTF-8 bytes")
    try:
        document = line.decode("utf-8")
    except UnicodeError:
        raise DuplexProtocolError("control input must be valid UTF-8") from None
    try:
        record = decode_machine_document(document)
    except MachineInputError as error:
        message = str(error).replace("run input", "control input", 1)
        raise DuplexProtocolError(message, request_id=error.request_id) from None
    request_id = record.get("request_id")
    correlation = None
    if isinstance(request_id, str) and request_id.strip():
        correlation = request_id.strip()
    try:
        _validate_fields(record)
        return DuplexControl(
            kind=record["type"],
            request_id=record["request_id"],
            session_id=record.get("session_id"),
            interaction_id=record.get("interaction_id"),
            answers=record.get("answers", ()),
            call_id=record.get("call_id"),
            result=record.get("result"),
            error=record.get("error"),
        )
    except DuplexProtocolError as error:
        from jiuwenswarm.channels.process_cli.protocol.schema import input_error_details

        kind = record.get("type")
        definition = kind if kind in ("answer", "cancel", "tool_result") else "control"
        raise DuplexProtocolError(
            str(error),
            request_id=correlation,
            details=input_error_details(record, definition),
        ) from None


__all__ = ["DuplexControl", "DuplexProtocolError", "decode_control"]
