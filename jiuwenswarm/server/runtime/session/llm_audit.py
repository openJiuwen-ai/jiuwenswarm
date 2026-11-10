# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded call records referencing immutable, reusable model-input blocks."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from contextlib import closing, ExitStack
from typing import Any, Iterable

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.session import lifecycle as lc

MAX_CHUNK_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
_CALL_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


class AuditCopyError(ValueError):
    """A fork cannot retain an audit record without its original blocks."""


def _directory(session_id: str) -> Path:
    return get_agent_sessions_dir() / lc.validate_id(session_id) / "llm_audit"


def _chunks(value: Any, depth: int = 0) -> Iterable[bytes]:
    if depth > 64:
        raise ValueError("Audit JSON nesting exceeds the limit")
    if isinstance(value, str):
        yield b'"'
        for offset in range(0, len(value), 16384):
            yield json.dumps(value[offset:offset + 16384], ensure_ascii=False)[1:-1].encode("utf-8")
        yield b'"'
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError("Audit object keys must be strings")
            if index:
                yield b","
            yield from _chunks(key, depth + 1)
            yield b":"
            yield from _chunks(item, depth + 1)
        yield b"}"
    elif isinstance(value, (list, tuple)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from _chunks(item, depth + 1)
        yield b"]"
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _json_value(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else copy.deepcopy(value)


def _put(directory: Path, value: Any) -> dict[str, Any]:
    digest, length = hashlib.sha256(), 0
    for block in _chunks(value):
        digest.update(block)
        length += len(block)
    reference = {"sha256": digest.hexdigest(), "bytes": length, "media_type": "application/json"}
    blobs = directory / "blocks"
    path = blobs / reference["sha256"]
    if path.exists():
        if path.stat().st_size != length:
            raise ValueError("Existing audit block has an invalid size")
        return reference
    blobs.mkdir(parents=True, exist_ok=True)
    temporary = blobs / ("." + uuid.uuid4().hex + ".tmp")
    try:
        written = hashlib.sha256()
        with temporary.open("xb") as output:
            for block in _chunks(value):
                written.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if written.hexdigest() != reference["sha256"]:
            raise ValueError("Audit value changed while being saved")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return reference


def _connection(directory: Path, *, writable: bool = False) -> sqlite3.Connection:
    path = directory / "calls.sqlite"
    if writable:
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS calls ("
                           "call_id TEXT PRIMARY KEY, started_ns INTEGER NOT NULL, record TEXT NOT NULL)")
        connection.execute("CREATE INDEX IF NOT EXISTS calls_started ON calls(started_ns DESC, call_id DESC)")
        return connection
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _call_id(value: Any) -> str:
    if not isinstance(value, str) or not _CALL_ID.fullmatch(value):
        raise ValueError("Invalid model call ID")
    return value


def write_call(session_id: str, record: dict[str, Any]) -> None:
    _call_id(record.get("call_id"))
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("Audit call metadata exceeds the limit")
    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        with closing(_connection(_directory(session_id), writable=True)) as connection, connection:
            connection.execute("INSERT INTO calls(call_id, started_ns, record) VALUES (?, ?, ?) "
                               "ON CONFLICT(call_id) DO UPDATE SET record=excluded.record",
                               (record["call_id"], record["started_ns"], encoded))


def save_input(session_id: str, messages: Iterable[Any], tools: Iterable[Any]) -> dict[str, Any]:
    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        directory = _directory(session_id)
        manifest = {"schema_version": "llm-input.v1", "messages": [], "tools": []}
        for key, values in (("messages", messages), ("tools", tools)):
            for value in values:
                manifest[key].append(_put(directory, _json_value(value)))
        reference = _put(directory, manifest)
        if reference["bytes"] > MAX_MANIFEST_BYTES:
            raise ValueError("Audit input manifest exceeds the limit")
        return {**reference, "messages": len(manifest["messages"]), "tools": len(manifest["tools"])}


def save_response(session_id: str, response: Any) -> dict[str, Any]:
    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        return _put(_directory(session_id), _json_value(response))


def _read_json(directory: Path, reference: dict[str, Any]) -> Any:
    digest = reference.get("sha256")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid audit block digest")
    path = directory / "blocks" / digest
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Audit manifest exceeds the read limit")
    raw = path.read_bytes()
    if len(raw) != reference.get("bytes") or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Audit block integrity check failed")
    return json.loads(raw)


def get_call(session_id: str, call_id: str) -> dict[str, Any]:
    with closing(_connection(_directory(session_id))) as connection:
        row = connection.execute("SELECT record FROM calls WHERE call_id=?", (_call_id(call_id),)).fetchone()
    if row is None:
        raise ValueError("Model call was not found in this session")
    record = json.loads(row[0])
    if record.get("status") == "running":
        import psutil

        try:
            owner = psutil.Process(record["worker_pid"])
            if owner.create_time() != record["worker_created"] or owner.status() == psutil.STATUS_ZOMBIE:
                record["status"] = "interrupted"
        except psutil.NoSuchProcess:
            record["status"] = "interrupted"
    return record


def list_calls(session_id: str, *, cursor: str | None = None, limit: int = 30) -> dict[str, Any]:
    directory = _directory(session_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Audit page limit must be between 1 and 100")
    parameters: list[Any] = []
    condition = ""
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError("Invalid audit cursor")
        try:
            started, identity = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid audit cursor") from exc
        if type(started) is not int or not 0 < started < 2 ** 63:
            raise ValueError("Invalid audit cursor time")
        condition = "WHERE (started_ns, call_id) < (?, ?)"
        parameters.extend((started, _call_id(identity)))
    if not (directory / "calls.sqlite").exists():
        return {"calls": [], "next_cursor": None}
    parameters.append(limit + 1)
    with closing(_connection(directory)) as connection:
        rows = connection.execute(f"SELECT call_id, started_ns, record FROM calls {condition} "
                                  "ORDER BY started_ns DESC, call_id DESC LIMIT ?", parameters).fetchall()
    selected = rows[:limit]
    continuation = None
    if len(rows) > limit:
        last = selected[-1]
        continuation = base64.urlsafe_b64encode(json.dumps([last[1], last[0]]).encode()).decode()
    return {"calls": [get_call(session_id, row[0]) for row in selected], "next_cursor": continuation}


def get_parts(session_id: str, call_id: str, *, collection: str = "messages",
              start: int = 0, limit: int = 30) -> dict[str, Any]:
    if collection not in {"messages", "tools", "response"}:
        raise ValueError("Unknown audit collection")
    if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid audit part page")
    record = get_call(session_id, call_id)
    if collection == "response":
        references = [record["response"]] if record.get("response") else []
    elif record.get("input"):
        manifest = _read_json(_directory(session_id), record["input"])
        references = manifest[collection]
    else:
        references = []
    return {"collection": collection, "total": len(references),
            "parts": [{"index": index, **reference} for index, reference in
                      enumerate(references[start:start + limit], start=start)]}


def read_part(session_id: str, call_id: str, *, collection: str = "messages", index: int = 0,
              offset: int = 0, max_bytes: int = MAX_CHUNK_BYTES) -> dict[str, Any]:
    if type(index) is not int or index < 0:
        raise ValueError("Invalid audit part index")
    page = get_parts(session_id, call_id, collection=collection, start=index, limit=1)
    if not page["parts"]:
        raise ValueError("Audit part was not found")
    reference = page["parts"][0]
    invalid_offset = type(offset) is not int or offset < 0
    invalid_size = type(max_bytes) is not int or not 4 <= max_bytes <= MAX_CHUNK_BYTES
    if invalid_offset or invalid_size:
        raise ValueError("Invalid audit byte range")
    path = _directory(session_id) / "blocks" / reference["sha256"]
    if path.stat().st_size != reference["bytes"] or offset > reference["bytes"]:
        raise ValueError("Audit part size or offset changed")
    with path.open("rb") as source:
        source.seek(offset)
        raw = source.read(max_bytes)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data":
            raise ValueError("Audit offset is not a UTF-8 boundary") from exc
        raw = raw[:exc.start]
        content = raw.decode("utf-8")
    end = offset + len(raw)
    return {**reference, "content": content, "offset": offset, "next_offset": end,
            "eof": end == reference["bytes"]}


def copy_call(source_session: str, target_session: str, call_id: str, *, include_response: bool,
              required: bool = True) -> None:
    """Copy only calls referenced by the forked history, including partial calls."""
    try:
        with ExitStack() as stack:
            for identity in sorted({lc.validate_id(source_session), lc.validate_id(target_session)}):
                stack.enter_context(lc.resource_lock("session", identity))
            lc.guard(source_session)
            lc.write_guard(target_session)
            try:
                previous = get_call(target_session, call_id)
            except (OSError, ValueError, sqlite3.Error):
                previous = None
            if previous and previous["status"] in {"completed", "failed"}:
                return
            try:
                record = get_call(source_session, call_id)
            except (OSError, ValueError, sqlite3.Error):
                if not required:
                    return
                raise
            source_dir, target_dir = _directory(source_session), _directory(target_session)
            references = []
            if record.get("input"):
                manifest = _read_json(source_dir, record["input"])
                references.extend(manifest["messages"] + manifest["tools"] + [record["input"]])
            if include_response and record.get("response"):
                references.append(record["response"])
            for reference in references:
                digest = reference["sha256"]
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    raise ValueError("Invalid fork audit block digest")
                destination = target_dir / "blocks" / digest
                if destination.exists() and destination.stat().st_size == reference["bytes"]:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.parent / ("." + uuid.uuid4().hex + ".tmp")
                try:
                    checksum, length = hashlib.sha256(), 0
                    with (source_dir / "blocks" / digest).open("rb") as source, temporary.open("xb") as output:
                        block = source.read(MAX_CHUNK_BYTES)
                        while block:
                            checksum.update(block)
                            length += len(block)
                            output.write(block)
                            block = source.read(MAX_CHUNK_BYTES)
                    if checksum.hexdigest() != digest or length != reference["bytes"]:
                        raise ValueError("Fork audit block integrity check failed")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            if not include_response:
                record.pop("response", None)
                record.pop("usage", None)
                record.pop("finished_ns", None)
                record.pop("latency_ms", None)
                record["status"] = "interrupted"
                record["error"] = "The fork ends before this model call completed"
            write_call(target_session, record)
    except (OSError, ValueError, KeyError, sqlite3.Error, lc.LifecycleError) as exc:
        raise AuditCopyError("Could not preserve the forked model-call audit") from exc
