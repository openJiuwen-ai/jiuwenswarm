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
_COMMON_REQUIRED = frozenset({"schema_version", "type", "request_id"})


class DuplexProtocolError(ValueError):
    """Safe control diagnostic with optional usable request correlation."""

    code = "INVALID_CONTROL"

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


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

    kind: Literal["answer", "cancel"]
    request_id: str
    session_id: str | None = None
    interaction_id: str | None = None
    answers: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ("answer", "cancel"):
            raise DuplexProtocolError("control type must be answer or cancel")
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
        else:
            if self.session_id is not None:
                object.__setattr__(
                    self, "session_id", _required_text("session_id", self.session_id)
                )
            if self.interaction_id is not None or self.answers != ():
                raise DuplexProtocolError("cancel must not contain answer fields")

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
        return record


def _validate_fields(record: dict[str, Any]) -> None:
    kind = record.get("type")
    if kind == "answer":
        allowed = _ANSWER_FIELDS
        required = _ANSWER_FIELDS
    elif kind == "cancel":
        allowed = _CANCEL_FIELDS
        required = _COMMON_REQUIRED
    else:
        raise DuplexProtocolError("control type must be answer or cancel")
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
        )
    except DuplexProtocolError as error:
        raise DuplexProtocolError(str(error), request_id=correlation) from None


__all__ = ["DuplexControl", "DuplexProtocolError", "decode_control"]
