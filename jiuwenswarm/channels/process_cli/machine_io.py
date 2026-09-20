# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded one-document input and streaming output for the machine CLI.

Input ends at EOF: stdin is not a duplex control channel. This module owns
only transport validation and rendering, never Runtime startup or execution.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any, BinaryIO, TextIO

from jiuwenswarm.channels.process_cli.protocol.jsonl import encode_jsonl_record
from jiuwenswarm.channels.process_cli.protocol.model import (
    OneShotEvent,
    OneShotRunInput,
    OneShotRunResult,
)

if TYPE_CHECKING:
    from jiuwenswarm.runtime.events import RuntimeEvent


MAX_RUN_INPUT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 8192
_SCHEMA_FIELDS = (
    "schema_version",
    "type",
    "request_id",
    "session_id",
    "input",
    "agent",
    "name",
    "instructions",
    "description",
    "model",
    "tools",
    "skills",
    "max_iterations",
    "mode",
    "workspace",
    "cwd",
    "project_dir",
    "trusted_dirs",
    "timeout_seconds",
)


class MachineInputError(ValueError):
    """Safe diagnostic for a rejected machine document, before Runtime starts."""

    code = "INVALID_INPUT"

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


def _read_limited(stream: BinaryIO | TextIO) -> str:
    content = bytearray()
    while True:
        remaining = MAX_RUN_INPUT_BYTES - len(content) + 1
        chunk = stream.read(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        encoded = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if len(content) + len(encoded) > MAX_RUN_INPUT_BYTES:
            raise MachineInputError("run input exceeds the byte limit")
        content.extend(encoded)
    return content.decode("utf-8")


def _request_id(value: dict[str, Any]) -> str | None:
    request_id = value.get("request_id")
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return None


def decode_machine_document(document: str) -> dict[str, Any]:
    """Decode a strict machine JSON object without echoing unsafe values."""
    if not document.strip():
        raise MachineInputError("run input must not be empty")
    duplicate_key = False
    non_finite_number = False

    def collect_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate_key
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                duplicate_key = True
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        nonlocal non_finite_number
        non_finite_number = True

    try:
        value = json.loads(
            document,
            object_pairs_hook=collect_pairs,
            parse_constant=reject_constant,
        )
    except (ValueError, RecursionError):
        raise MachineInputError(
            "run input must contain one valid JSON document"
        ) from None
    if not isinstance(value, dict):
        raise MachineInputError("run input must be a JSON object")
    request_id = _request_id(value)
    if duplicate_key:
        raise MachineInputError(
            "run input contains duplicate JSON keys", request_id=request_id
        )
    if non_finite_number:
        raise MachineInputError(
            "run input must contain only finite JSON numbers", request_id=request_id
        )
    return value


def _safe_schema_reason(error: Exception) -> str:
    """Name known schema fields without echoing values or unknown field names."""

    message = str(error)
    if message.startswith("unsupported schema_version"):
        return "schema_version is not supported"
    if message.startswith("unsupported single-Agent mode"):
        return "mode is not supported"
    for name in ("run", "agent", "workspace"):
        if message.startswith(f"{name} contains unknown fields"):
            return f"{name} contains unknown fields"
        if message.startswith(f"{name} is missing required fields"):
            return f"{name} is missing required fields"
    for name in _SCHEMA_FIELDS:
        if message.startswith((f"{name} ", f"{name}[", f"{name}.")):
            return f"{name} is invalid"
    return "run input does not match the schema"


def read_run_input(
    source: str, *, stdin: BinaryIO | TextIO | None = None
) -> OneShotRunInput:
    """Read one strict UTF-8 JSON document from a file or stdin through EOF.

    ``source='-'`` uses the supplied stdin, or the process stdin. A binary
    buffer is preferred when available so Windows locale decoding cannot
    reinterpret a UTF-8 request. Borrowed stdin streams are never closed.
    """

    value = read_machine_document(source, stdin=stdin)
    try:
        return OneShotRunInput.from_dict(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise MachineInputError(
            _safe_schema_reason(error), request_id=_request_id(value)
        ) from None


def read_machine_document(
    source: str, *, stdin: BinaryIO | TextIO | None = None
) -> dict[str, Any]:
    """Read a bounded strict UTF-8 document before any Runtime import."""
    try:
        if source == "-":
            input_stream = sys.stdin if stdin is None else stdin
            stream = getattr(input_stream, "buffer", input_stream)
            document = _read_limited(stream)
        else:
            with open(source, "rb") as stream:
                document = _read_limited(stream)
    except MachineInputError:
        raise
    except UnicodeError:
        raise MachineInputError("run input must be valid UTF-8") from None
    except (OSError, ValueError):
        raise MachineInputError("run input could not be read") from None
    return decode_machine_document(document)


class OneShotWriter:
    """Write each event immediately, followed by at most one terminal result.

    The runner assigns ``session_id`` after resolving the Runtime Session.
    Runtime event identities never replace the external command identity.
    Once output fails, ``broken`` forbids further writes or retries because
    a record may already have been partially delivered.
    """

    def __init__(self, stdout: TextIO, *, request_id: str) -> None:
        if not isinstance(request_id, str):
            raise TypeError("request_id must be a string")
        if not request_id.strip():
            raise ValueError("request_id must not be empty")
        self._stdout = stdout
        self.request_id = request_id.strip()
        self.sequence = 0
        self.session_id: str | None = None
        self.broken = False
        self.final_written = False

    def _check_writable(self) -> None:
        if self.broken:
            raise BrokenPipeError("machine output stream is broken")
        if self.final_written:
            raise ValueError("terminal result has already been written")

    def _write(self, record: OneShotEvent | OneShotRunResult) -> None:
        line = encode_jsonl_record(record)
        try:
            written = self._stdout.write(line)
            if written != len(line):
                raise OSError("machine output write was incomplete")
            self._stdout.flush()
        except (OSError, ValueError):
            self.broken = True
            raise
        self.sequence += 1

    def write_event(self, event: RuntimeEvent) -> None:
        """Render one observation without changing Runtime-owned data."""

        self._check_writable()
        record = OneShotEvent(
            sequence=self.sequence,
            request_id=self.request_id,
            session_id=self.session_id,
            event_type=event.event_type or "runtime.event",
            payload=event.payload,
        )
        self._write(record)

    def write_result(self, result: OneShotRunResult) -> None:
        """Render the final outcome using this command's sequence and identity."""

        self._check_writable()
        record = replace(
            result,
            sequence=self.sequence,
            request_id=self.request_id,
            session_id=self.session_id,
        )
        self._write(record)
        self.final_written = True


__all__ = [
    "MAX_RUN_INPUT_BYTES",
    "MachineInputError",
    "OneShotWriter",
    "decode_machine_document",
    "read_run_input",
]
