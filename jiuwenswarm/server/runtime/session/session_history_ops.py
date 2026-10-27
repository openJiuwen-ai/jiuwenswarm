# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound-session transcript operations, independent of extension dispatch."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
import re
import time
from typing import Any

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.session_history import (
    HistoryAppendConflict,
    HistoryAppendOutcomeUnknown,
    HistorySnapshotChanged,
    InvalidHistoryCursor,
    PreparedHistoryRecord,
    commit_history_record,
    prepare_history_record,
    read_history_cursor_page,
    resolve_session_dir,
    use_legacy_history_json,
)
from jiuwenswarm.server.wire_truncate import is_restorable_history_record

MAX_APPEND_BYTES = 1024 * 1024
_MESSAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


class SessionHistoryError(RuntimeError):
    """A stable capability failure without underlying file contents or paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"session history operation failed: {code}")


def require_session_id(session_id: object) -> str:
    """Validate a bound session identity before any filesystem access."""
    if not isinstance(session_id, str) or not is_valid_session_id(session_id):
        raise SessionHistoryError("INVALID_SESSION")
    return session_id


def preflight_append(session_id: str, *, require_existing: bool) -> int:
    """Check session path and lifecycle before executing extension code."""
    require_session_id(session_id)
    path, error = resolve_session_dir(session_id, create=False)
    if error or path is None:
        raise SessionHistoryError("INVALID_SESSION")
    if require_existing and not path.is_dir():
        raise SessionHistoryError("SESSION_NOT_FOUND")
    try:
        with lifecycle.resource_lock("session", session_id):
            lifecycle.write_guard(session_id)
            return lifecycle.state("session", session_id).get("generation", 0)
    except lifecycle.LifecycleError as error:
        raise SessionHistoryError(error.code) from None


def public_history_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached public record without the internal commit marker."""
    return copy.deepcopy({key: value for key, value in record.items() if key != "_session_append"})


def read_session_messages(session_id: str, limit: int, cursor: str | None) -> dict[str, Any]:
    """Read one stable, newest-first page from the current main transcript."""
    require_session_id(session_id)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise SessionHistoryError("INVALID_MESSAGE_LIMIT")
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise SessionHistoryError("INVALID_CURSOR")
    if use_legacy_history_json():
        raise SessionHistoryError("HISTORY_FORMAT_UNSUPPORTED")
    try:
        page = read_history_cursor_page(
            session_id, cursor=cursor, limit=limit, is_restorable=is_restorable_history_record,
        )
    except HistorySnapshotChanged:
        raise SessionHistoryError("SNAPSHOT_CHANGED") from None
    except InvalidHistoryCursor:
        raise SessionHistoryError("INVALID_CURSOR") from None
    except OSError:
        raise SessionHistoryError("HISTORY_IO_FAILED") from None
    return {
        "messages": [public_history_record(row) for row in page["messages"]],
        "next_cursor": page["next_cursor"], "has_more": page["has_more"], "snapshot_id": page["snapshot_id"],
    }


def original_fingerprint(value: Mapping[str, Any]) -> str:
    """Hash a canonical original request, excluding its generated timestamp."""
    original = {key: item for key, item in value.items() if key != "timestamp"}
    normalized = json.loads(json.dumps(original, ensure_ascii=False))
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def prepare_extension_append(
    session_id: str,
    package_id: str,
    message: object,
) -> tuple[PreparedHistoryRecord, str, str, str]:
    """Validate a user message and create a namespaced transcript-only record."""
    require_session_id(session_id)
    if not isinstance(message, Mapping) or set(message) != {"id", "role", "content"}:
        raise SessionHistoryError("INVALID_MESSAGE")
    message_id, role, content = message["id"], message["role"], message["content"]
    if not isinstance(message_id, str) or _MESSAGE_ID.fullmatch(message_id) is None:
        raise SessionHistoryError("INVALID_MESSAGE")
    if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
        raise SessionHistoryError("INVALID_MESSAGE")
    try:
        content_bytes = len(content.encode("utf-8"))
    except UnicodeError:
        raise SessionHistoryError("INVALID_MESSAGE") from None
    if content_bytes > MAX_APPEND_BYTES:
        raise SessionHistoryError("INVALID_MESSAGE")
    if not isinstance(package_id, str) or not package_id:
        raise SessionHistoryError("PACKAGE_CONTEXT_REQUIRED")
    key = hashlib.sha256(f"{package_id}\0{message_id}".encode("utf-8")).hexdigest()
    prepared = prepare_history_record(
        session_id=session_id, request_id="ext-" + key[:32], channel_id="extension",
        role=role, content=content, timestamp=time.time(),
        event_type="chat.final" if role == "assistant" else None,
        extra={"id": "ext:" + key, "extension_message_id": message_id,
               "extension_package_id": package_id, "message_origin": "extension", "transcript_only": True},
    )
    if prepared is None:
        raise SessionHistoryError("INVALID_MESSAGE")
    return prepared, message_id, "extension:" + key, original_fingerprint(dict(message))


def commit_session_message(
    prepared: PreparedHistoryRecord,
    append_key: str,
    fingerprint: str,
    update_metadata: bool,
) -> tuple[dict[str, Any], bool]:
    """Wait for an actual history commit and translate storage failures."""
    try:
        record, duplicate = commit_history_record(
            prepared, append_key=append_key, fingerprint=fingerprint, update_metadata=update_metadata,
        )
    except HistoryAppendConflict:
        raise SessionHistoryError("APPEND_ID_CONFLICT") from None
    except HistoryAppendOutcomeUnknown:
        raise SessionHistoryError("APPEND_OUTCOME_UNKNOWN") from None
    except lifecycle.LifecycleError as error:
        raise SessionHistoryError(error.code) from None
    except OSError:
        raise SessionHistoryError("HISTORY_IO_FAILED") from None
    return public_history_record(record), duplicate
