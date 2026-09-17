# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Incremental envelope checks only; Agent/policy semantics stay in Runtime."""

from __future__ import annotations

import json
import math
from typing import Any

SCHEMA_VERSION = "0.1"
MAX_INPUT_BYTES = 1024 * 1024


class ProtocolError(RuntimeError):
    """Malformed, incomplete, mismatched, or unsupported CLI output."""


def encode(record: dict[str, Any]) -> bytes:
    encoded = (
        json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("input record exceeds 1 MiB")
    return encoded


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate output key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ProtocolError("non-finite output number")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError("non-finite output number")
    return result


class Records:
    """Bounded state: no event history is retained by the SDK."""

    def __init__(
        self, request_id: str, *, query: bool = False, operation: str | None = None
    ) -> None:
        self.request_id = request_id
        self.query = query
        self.operation = operation
        self.sequence = 0
        self.session_id: str | None = None
        self.result: dict[str, Any] | None = None

    def accept(self, line: bytes) -> dict[str, Any]:
        if self.result is not None:
            raise ProtocolError("output after terminal result")
        if not line.endswith(b"\n"):
            raise ProtocolError("truncated JSONL record")
        try:
            record = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
        except (ValueError, RecursionError) as error:
            raise ProtocolError("invalid UTF-8 JSON record") from error
        if not isinstance(record, dict):
            raise ProtocolError("output record must be an object")
        if (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("request_id") != self.request_id
        ):
            raise ProtocolError("output version/request identity mismatch")
        sequence = record.get("sequence")
        if type(sequence) is not int or sequence != self.sequence:
            raise ProtocolError("non-contiguous output sequence")
        self.sequence += 1
        session_id = record.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise ProtocolError("invalid Session identity")
            if self.session_id is not None and self.session_id != session_id:
                raise ProtocolError("Session identity changed")
            self.session_id = session_id
        kind = record.get("type")
        if kind == "event" and not self.query:
            if (
                not isinstance(record.get("event_type"), str)
                or not record["event_type"]
            ):
                raise ProtocolError("invalid event type")
            return record
        expected = "query_result" if self.query else "result"
        if kind != expected or session_id != self.session_id:
            raise ProtocolError("invalid terminal record")
        if self.query:
            if session_id is not None or self.sequence != 1:
                raise ProtocolError("query must not emit Session events")
            if record.get("operation") not in (None, self.operation):
                raise ProtocolError("query operation mismatch")
        self._validate_terminal(record)
        self.result = record
        return record

    def _validate_terminal(self, record: dict[str, Any]) -> None:
        status = record.get("status")
        code = record.get("exit_code")
        if type(code) is not int or code not in (0, 1, 2, 124, 130):
            raise ProtocolError("invalid exit code")
        expected = {
            0: "completed",
            1: "failed",
            2: "failed",
            124: "timed_out",
            130: "cancelled",
        }[code]
        if status != expected:
            raise ProtocolError("inconsistent result status/exit code")
        error = record.get("error")
        if code != 0:
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise ProtocolError("failed result requires structured error")
        elif error is not None:
            raise ProtocolError("successful result contains error")
        elif self.query:
            if record.get("operation") != self.operation or not isinstance(
                record.get("data"), dict
            ):
                raise ProtocolError("successful query requires data and operation")
        elif self.session_id is None:
            raise ProtocolError("successful run requires Session identity")

    def finish(self, exit_code: int) -> dict[str, Any]:
        if self.result is None:
            raise ProtocolError("process exited without a terminal result")
        if self.result["exit_code"] != exit_code:
            raise ProtocolError("terminal result does not match process exit code")
        return self.result
