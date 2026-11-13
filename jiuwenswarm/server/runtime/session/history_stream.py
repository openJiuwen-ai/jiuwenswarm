"""Bounded projection of old JSON/JSONL histories, including giant audit rows.

The parser validates JSON while skipping strings in chunks. A decoder never
receives a giant prompt merely to discover the row's event_type at its end.
"""
from __future__ import annotations

import codecs
from contextlib import nullcontext
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from jiuwenswarm.server.runtime.session.history_artifacts import put_chunks
from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError, is_context_record

_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_WHITE = re.compile(rb'[^ \t\r\n]')
_LARGE_FIELDS = {"content", "rendered_result", "result", "raw_output"}
_OMIT_FIELDS = {"prompt", "event_payload", "usage", "session_usage"}
_METADATA_FIELDS = {"role", "event_type", "request_id", "channel_id", "id", "compact_metadata", "cron_failure_record"}


class _JSONStream:
    def __init__(self, source: BinaryIO, end: int, *, deadline: float):
        self.source = source
        self.end = end
        self.deadline = deadline
        self.buffer = b""
        self.index = 0
        self.position = source.tell()

    def fill(self) -> bool:
        if self.index < len(self.buffer):
            return True
        if time.monotonic() > self.deadline:
            raise HistoryReplayError("Legacy migration time budget exceeded", code="HISTORY_RESTORE_LIMIT")
        self.buffer = self.source.read(min(65536, self.end - self.position))
        self.index = 0
        return bool(self.buffer)

    def peek(self) -> int:
        return self.buffer[self.index] if self.fill() else -1

    def advance(self, count: int = 1) -> None:
        self.index += count
        self.position += count

    def expect(self, char: int) -> None:
        if self.peek() != char:
            raise HistoryReplayError(f"Malformed legacy JSON at byte {self.position}")
        self.advance()

    def white(self) -> None:
        while self.fill():
            match = _WHITE.search(self.buffer, self.index)
            if match is not None:
                self.advance(match.start() - self.index)
                return
            self.advance(len(self.buffer) - self.index)

    def string(self) -> None:
        self.expect(34)
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        while self.fill():
            match = _SPECIAL.search(self.buffer, self.index)
            stop = match.start() if match is not None else len(self.buffer)
            try:
                decoder.decode(self.buffer[self.index:stop], final=match is not None)
            except UnicodeError as exc:
                raise HistoryReplayError("Invalid UTF-8 in legacy history string") from exc
            self.advance(stop - self.index)
            if match is None:
                continue
            char = self.peek()
            self.advance()
            if char == 34:
                return
            if char != 92:
                raise HistoryReplayError("Control character in legacy JSON string")
            escape = self.peek()
            if escape < 0:
                raise HistoryReplayError("Incomplete escape in legacy history")
            self.advance()
            if escape == 117:
                for _ in range(4):
                    if self.peek() < 0 or self.peek() not in b"0123456789abcdefABCDEF":
                        raise HistoryReplayError("Invalid Unicode escape in legacy history")
                    self.advance()
            elif escape not in b'"\\/bfnrt':
                raise HistoryReplayError("Invalid escape in legacy history")
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
        raise HistoryReplayError("Unterminated legacy JSON string")

    def value(self, depth: int = 0) -> None:
        if depth > 64:
            raise HistoryReplayError("Legacy JSON nesting exceeds the migration budget")
        self.white()
        char = self.peek()
        if char == 34:
            self.string()
        elif char in (123, 91):
            self.advance()
            closing = 125 if char == 123 else 93
            self.white()
            if self.peek() == closing:
                self.advance()
                return
            while True:
                if char == 123:
                    self.white()
                    self.string()
                    self.white()
                    self.expect(58)
                self.value(depth + 1)
                self.white()
                if self.peek() == closing:
                    self.advance()
                    return
                self.expect(44)
        else:
            token = bytearray()
            while self.peek() not in (-1, 32, 9, 10, 13, 44, 93, 125):
                token.append(self.peek())
                self.advance()
                if len(token) > 128:
                    raise HistoryReplayError("Invalid or oversized legacy JSON token")
            try:
                value = json.loads(token, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, (bool, int, float, type(None))):
                    raise ValueError()
            except ValueError as exc:
                raise HistoryReplayError("Invalid legacy JSON token") from exc


@dataclass(frozen=True)
class HistoryRow:
    record: dict[str, Any]
    start: int
    end: int
    value_end: int


def _read_value(source: BinaryIO, span: tuple[int, int], *, max_bytes: int) -> Any:
    start, end = span
    if end - start > max_bytes:
        raise HistoryReplayError("Legacy record metadata exceeds the migration budget", code="HISTORY_RESTORE_LIMIT")
    source.seek(start)
    return json.loads(source.read(end - start))


def _range_chunks(source: BinaryIO, start: int, end: int) -> Iterator[bytes]:
    source.seek(start)
    while start < end:
        chunk = source.read(min(65536, end - start))
        if not chunk:
            raise HistoryReplayError("Legacy history ended during artifact extraction")
        yield chunk
        start += len(chunk)


def iter_history_rows(path: Path, *, session_id: str, start: int = 0, end: int | None = None,
                      metadata_only: bool = False, max_field_bytes: int = 4 * 1024 * 1024,
                      max_scan_bytes: int = 8 * 1024 * 1024 * 1024,
                      max_seconds: float = 120,
                      include_record: Callable[[dict[str, Any]], bool] | None = None,
                      omit_fields: frozenset[str] | None = None) -> Iterator[HistoryRow]:
    """Project semantic rows with a scan-time budget that excludes consumer waits.

    Consumers may await native compression between pages. That wait must not
    exhaust the scanner's time budget while no history bytes are being parsed.
    A caller may override diagnostic omissions; field and row budgets still apply.
    """
    omitted = _OMIT_FIELDS if omit_fields is None else omit_fields
    end = path.stat().st_size if end is None else end
    if end < start or end - start > max_scan_bytes:
        raise HistoryReplayError("Legacy history exceeds the migration disk budget", code="HISTORY_RESTORE_LIMIT")
    if end == 0:
        return
    with path.open("rb") as source, path.open("rb") as values:
        source.seek(start)
        reader = _JSONStream(source, end, deadline=time.monotonic() + max_seconds)
        if start == 0 and reader.peek() == 239:
            reader.expect(239)
            reader.expect(187)
            reader.expect(191)
        legacy = path.suffix.lower() != ".jsonl"
        if legacy and start == 0:
            reader.white()
            reader.expect(91)
        reader.white()
        while reader.peek() != -1 and not (legacy and reader.peek() == 93):
            row_start = reader.position
            reader.expect(123)
            spans = {}
            reader.white()
            if reader.peek() != 125:
                while True:
                    key_start = reader.position
                    reader.string()
                    key = _read_value(values, (key_start, reader.position), max_bytes=1024)
                    if key in spans or len(spans) >= 256:
                        raise HistoryReplayError("Duplicate or excessive legacy history fields")
                    reader.white()
                    reader.expect(58)
                    reader.white()
                    value_start = reader.position
                    reader.value()
                    spans[key] = (value_start, reader.position)
                    reader.white()
                    if reader.peek() == 125:
                        break
                    reader.expect(44)
                    reader.white()
            reader.expect(125)
            value_end = reader.position
            row_end = reader.position
            record = {key: _read_value(values, span, max_bytes=65536)
                      for key, span in spans.items() if key in _METADATA_FIELDS}
            selected = include_record(record) if include_record is not None else is_context_record(record)
            if not metadata_only and selected:
                retained = 0
                for key, span in spans.items():
                    if key in record or key in omitted:
                        continue
                    length = span[1] - span[0]
                    if length > max_field_bytes:
                        if record.get("event_type") != "chat.tool_result" or key not in _LARGE_FIELDS:
                            raise HistoryReplayError(f"Legacy input field {key} needs explicit migration",
                                                     code="HISTORY_RESTORE_LIMIT")
                        ref = put_chunks(session_id, _range_chunks(values, *span), media_type="application/json")
                        record.setdefault("payload_refs", {})[key] = ref
                        record[key] = f"[Complete legacy {key}: {length} bytes; JSON artifact {ref['path']}]"
                    else:
                        retained += length
                        if retained > 4 * 1024 * 1024:
                            raise HistoryReplayError("Legacy semantic row exceeds migration budget",
                                                     code="HISTORY_RESTORE_LIMIT")
                        record[key] = _read_value(values, span, max_bytes=max_field_bytes)
            if legacy:
                reader.white()
                if reader.peek() == 44:
                    reader.advance()
                    reader.white()
                    if reader.peek() == 93:
                        raise HistoryReplayError("Trailing comma in legacy history")
                elif reader.peek() != 93:
                    raise HistoryReplayError("Malformed legacy history array")
                row_end = reader.position
            else:
                while reader.peek() in (32, 9, 13):
                    reader.advance()
                if reader.peek() not in (-1, 10):
                    raise HistoryReplayError("Multiple JSON values on a history line")
                if reader.peek() == 10:
                    reader.advance()
                row_end = reader.position
                reader.white()
            paused_at = time.monotonic()
            try:
                yield HistoryRow(record, row_start, row_end, value_end)
            finally:
                reader.deadline += time.monotonic() - paused_at
        if legacy:
            reader.expect(93)
            reader.white()
            if reader.peek() != -1:
                raise HistoryReplayError("Data after legacy history array")


def convert_legacy_jsonl(session_id: str, source_path: Path, target_path: Path) -> None:
    """Convert an old array in bounded space, preserving the original file."""
    from jiuwenswarm.server.runtime.session import lifecycle as lc

    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        if target_path.exists():
            return
        before = source_path.stat()
        temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source_path.open("rb") as raw, temporary.open("xb") as output:
                for row in iter_history_rows(source_path, session_id=session_id, metadata_only=True):
                    # Valid JSON string literals contain escaped newlines. Raw
                    # CR/LF are whitespace, so removing them creates one row.
                    for chunk in _range_chunks(raw, row.start, row.value_end):
                        output.write(chunk.replace(b"\r", b"").replace(b"\n", b""))
                    output.write(b"\n")
                output.flush()
                os.fsync(output.fileno())
            after = source_path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise HistoryReplayError("Legacy history changed during conversion", code="HISTORY_RESTORE_CHANGED")
            os.replace(temporary, target_path)
        finally:
            temporary.unlink(missing_ok=True)


def rewrite_legacy_json(session_id: str, source_path: Path, target_path: Path, *,
                        append_record: dict[str, Any] | None = None,
                        skip_append: Callable[[dict[str, Any]], bool] | None = None) -> None:
    """Atomically copy an array/JSONL prefix without retaining its records."""
    from jiuwenswarm.server.runtime.session import lifecycle as lc
    from jiuwenswarm.server.runtime.session.context_snapshot import invalidate_snapshot_hint
    from jiuwenswarm.server.runtime.session.history_artifacts import json_chunks
    from jiuwenswarm.server.runtime.session.history_replay import _identity, invalidate_history_head

    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        before = source_path.stat() if source_path.exists() else None
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        duplicate = False
        try:
            source = source_path.open("rb") if before is not None else nullcontext(None)
            with (
                source as raw,
                os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output,
            ):
                output.write(b"[")
                count = 0
                if before is not None:
                    for row in iter_history_rows(source_path, session_id=session_id, metadata_only=True):
                        if count:
                            output.write(b",")
                        for chunk in _range_chunks(raw, row.start, row.value_end):
                            output.write(chunk)
                        count += 1
                        if skip_append is not None and skip_append(row.record):
                            duplicate = True
                if append_record is not None and not duplicate:
                    if count:
                        output.write(b",")
                    for chunk in json_chunks(append_record):
                        output.write(chunk)
                output.write(b"]")
                output.flush()
                os.fsync(output.fileno())
            after = source_path.stat() if source_path.exists() else None
            if ((before is None) != (after is None)
                    or before is not None and _identity(before) != _identity(after)):
                raise HistoryReplayError("History changed during rewrite", code="HISTORY_RESTORE_CHANGED")
            if duplicate:
                return
            os.replace(temporary, target_path)
            invalidate_history_head(target_path)
            invalidate_snapshot_hint(session_id)
        finally:
            temporary.unlink(missing_ok=True)
