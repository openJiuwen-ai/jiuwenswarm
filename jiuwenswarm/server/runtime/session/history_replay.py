"""Bounded, strict history reads for model restore and shared voice input."""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.server.runtime.session import lifecycle as lc

logger = logging.getLogger(__name__)


class HistoryReplayError(RuntimeError):
    """A history prefix cannot be restored completely and safely."""

    def __init__(self, reason: str, *, code: str = "HISTORY_RESTORE_FAILED") -> None:
        self.code = code
        super().__init__(f"[{code}] {reason}; the conversation was not restored")


@dataclass(frozen=True)
class HistoryReplayLimits:
    max_record_bytes: int = 4 * 1024 * 1024
    max_retained_bytes: int = 16 * 1024 * 1024
    max_records: int = 8192
    max_scan_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        budgets = (
            self.max_record_bytes, self.max_retained_bytes, self.max_records, self.max_scan_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in budgets):
            raise ValueError("History replay limits must be positive integers")


@dataclass(frozen=True)
class HistoryReplayCursor:
    path: Path
    generation: int
    identity: tuple[int, int, int, int, int]
    offset: int


@dataclass(frozen=True)
class HistoryReplayBatch:
    records: list[dict[str, Any]]
    cursor: HistoryReplayCursor
    prefix_cursor: HistoryReplayCursor
    bytes_read: int


@dataclass(frozen=True)
class _HistoryHead:
    identity: tuple[int, int, int, int, int]
    voice_end: int


_READ_SLOTS = threading.BoundedSemaphore(2)
_HEAD_LOCK = threading.Lock()
_HEADS: OrderedDict[Path, _HistoryHead] = OrderedDict()
_HEAD_CACHE_SIZE = 1024
_CONTEXT_EVENTS = frozenset({
    "chat.reasoning", "chat.tool_call", "chat.tool_result", "chat.final",
    "context.compact_summary", "context.rewind_summary", "context.fork_boundary",
})


def _identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _is_voice(record: dict[str, Any]) -> bool:
    return record.get("channel_id") == "video_duplex"


def is_context_record(record: dict[str, Any]) -> bool:
    """Keep semantic inputs; diagnostic prompts and streaming deltas are not inputs."""
    return (record.get("role") in {"user", "human"} or record.get("event_type") in _CONTEXT_EVENTS
            or bool(record.get("forked_from")))


def _remember(path: Path, head: _HistoryHead) -> None:
    with _HEAD_LOCK:
        _HEADS[path] = head
        _HEADS.move_to_end(path)
        while len(_HEADS) > _HEAD_CACHE_SIZE:
            _HEADS.popitem(last=False)


def invalidate_history_head(path: Path) -> None:
    with _HEAD_LOCK:
        _HEADS.pop(path, None)


def note_history_append(path: Path, record: dict[str, Any], before: os.stat_result | None) -> None:
    """Update only a completely known prefix, after the writer closed its file."""
    after = _identity(path.stat())
    with _HEAD_LOCK:
        head = _HEADS.get(path)
    if before is None or before.st_size == 0:
        voice_end = 0
    elif head is not None and head.identity == _identity(before):
        voice_end = head.voice_end
    else:
        invalidate_history_head(path)
        return
    _remember(path, _HistoryHead(after, after[2] if _is_voice(record) else voice_end))


def _check_cursor(cursor: HistoryReplayCursor, path: Path, generation: int,
                  identity: tuple[int, int, int, int, int]) -> None:
    if cursor.generation == generation and cursor.identity == (0, 0, 0, 0, 0) and cursor.offset == 0:
        return
    if cursor.path != path or cursor.generation != generation:
        raise HistoryReplayError("History changed after context restore", code="HISTORY_RESTORE_CHANGED")
    if cursor.identity[:2] != identity[:2] or identity[2] < cursor.offset:
        raise HistoryReplayError("History changed after context restore", code="HISTORY_RESTORE_CHANGED")
    if identity[2] == cursor.identity[2] and identity != cursor.identity:
        raise HistoryReplayError("History changed after context restore", code="HISTORY_RESTORE_CHANGED")


def read_history_replay(
    session_id: str,
    *,
    is_restorable: Callable[[dict[str, Any]], bool] = is_context_record,
    cursor: HistoryReplayCursor | None = None,
    before_request_id: str | None = None,
    voice_only: bool = False,
    limits: HistoryReplayLimits | None = None,
) -> HistoryReplayBatch:
    """Read a fixed durable prefix without returning partial results on failure.

    The limits bound both the largest decoder input and the retained semantic
    records. A cursor is valid only for the same lifecycle generation/file.
    The optional voice head is a cache: unknown or externally changed files
    always take the bounded scan, never an optimistic empty result.
    """
    if not _READ_SLOTS.acquire(timeout=5):
        raise HistoryReplayError("History restore is busy; retry later", code="HISTORY_RESTORE_BUSY")
    try:
        with lc.resource_lock("session", session_id):
            lc.guard(session_id)
            from jiuwenswarm.server.runtime.session.session_history import get_read_history_path

            path = get_read_history_path(session_id)
            generation = int(lc.state("session", session_id).get("generation", 0))
            if not path.exists():
                identity = (0, 0, 0, 0, 0)
                if cursor is not None:
                    _check_cursor(cursor, path, generation, identity)
                empty = HistoryReplayCursor(path, generation, identity, 0)
                return HistoryReplayBatch([], empty, empty, 0)
            identity = _identity(path.stat())
            if cursor is not None:
                _check_cursor(cursor, path, generation, identity)
            start = cursor.offset if cursor is not None else 0
            end_cursor = HistoryReplayCursor(path, generation, identity, identity[2])
            if voice_only:
                with _HEAD_LOCK:
                    head = _HEADS.get(path)
                if head is not None and head.identity == identity and head.voice_end <= start:
                    return HistoryReplayBatch([], end_cursor, end_cursor, 0)
            return _read_prefix(path, generation, identity, start, is_restorable,
                                before_request_id, limits or HistoryReplayLimits())
    except MemoryError as exc:
        raise HistoryReplayError("Insufficient memory for history restore", code="HISTORY_RESTORE_RESOURCE") from exc
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise HistoryReplayError(f"History could not be read ({type(exc).__name__})") from exc
    finally:
        _READ_SLOTS.release()


def read_history_for_rewrite(path: Path) -> list[dict[str, Any]]:
    """Bound legacy format conversion while the caller owns the writer locks.

    Do not enter restore admission here: admitted readers may themselves be
    waiting for this writer's lifecycle lock.
    """
    if not path.exists():
        return []
    try:
        return _read_prefix(path, 0, _identity(path.stat()), 0, lambda _record: True,
                            None, HistoryReplayLimits()).records
    except MemoryError as exc:
        raise HistoryReplayError("Insufficient memory for history conversion", code="HISTORY_RESTORE_RESOURCE") from exc
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise HistoryReplayError("History format conversion failed") from exc


def latest_completed_request_bounded(session_id: str, *, max_tail_bytes: int = 1024 * 1024) -> str | None:
    """Optional reply suggestions only need the last completion, never all history.

    A missing/incomplete tail suppresses suggestions rather than guessing that
    an older answer is still current.
    """
    from jiuwenswarm.common.schema.message import SESSION_REQUEST_COMPLETED_EVENT
    from jiuwenswarm.server.runtime.session.session_history import get_read_history_path

    if type(max_tail_bytes) is not int or not 0 < max_tail_bytes <= 4 * 1024 * 1024:
        raise ValueError("History tail budget must be between 1 byte and 4 MiB")
    try:
        with lc.resource_lock("session", session_id):
            lc.guard(session_id)
            path = get_read_history_path(session_id)
            if not path.exists():
                return None
            end = path.stat().st_size
            with path.open("rb") as source:
                if path.suffix.lower() == ".jsonl":
                    start = max(0, end - max_tail_bytes)
                    source.seek(start)
                    if start:
                        source.readline(max_tail_bytes)
                    tail = source.read(max_tail_bytes)
                    rows = (json.loads(raw) for raw in reversed(tail.split(b"\n")) if raw.strip())
                else:
                    if end > max_tail_bytes:
                        return None
                    records = json.loads(source.read(max_tail_bytes))
                    if not isinstance(records, list):
                        return None
                    rows = reversed(records)
                later = set()
                for record in rows:
                    if not isinstance(record, dict):
                        return None
                    request_id = record.get("request_id")
                    if not isinstance(request_id, (str, type(None))):
                        return None
                    if record.get("event_type") == SESSION_REQUEST_COMPLETED_EVENT:
                        return request_id if record.get("status") == "success" and later <= {request_id} else None
                    later.add(request_id)
    except (OSError, ValueError, TypeError, MemoryError):
        logger.warning("Could not inspect history tail for reply suggestions: session_id=%s", session_id)
    return None


def _read_prefix(
    path: Path,
    generation: int,
    identity: tuple[int, int, int, int, int],
    start: int,
    is_restorable: Callable[[dict[str, Any]], bool],
    before_request_id: str | None,
    limits: HistoryReplayLimits,
) -> HistoryReplayBatch:
    end = identity[2]
    if end - start > limits.max_scan_bytes:
        raise HistoryReplayError("History scan exceeds the restore byte budget", code="HISTORY_RESTORE_LIMIT")
    records = []
    retained_bytes = 0
    prefix_offset = None
    with _HEAD_LOCK:
        previous = _HEADS.get(path)
    voice_end = previous.voice_end if previous and previous.identity == identity else start

    def keep(record, size, offset):
        nonlocal retained_bytes, prefix_offset
        if not isinstance(record, dict):
            raise HistoryReplayError(f"History record at byte {offset} is not an object")
        if prefix_offset is None and before_request_id:
            if (record.get("role") in {"user", "human"}
                    and str(record.get("request_id") or "").strip() == before_request_id):
                prefix_offset = offset
        if not is_restorable(record):
            return
        retained_bytes += size
        if retained_bytes > limits.max_retained_bytes or len(records) >= limits.max_records:
            raise HistoryReplayError("History context exceeds the restore budget", code="HISTORY_RESTORE_LIMIT")
        records.append(record)

    with path.open("rb") as source:
        if path.suffix.lower() != ".jsonl":
            if start or end > limits.max_record_bytes:
                raise HistoryReplayError("Legacy history requires bounded migration", code="HISTORY_RESTORE_LIMIT")
            data = json.loads(source.read(end)) if end else []
            if not isinstance(data, list):
                raise HistoryReplayError("Legacy history is not an array")
            for record in data:
                keep(record, len(json.dumps(record, ensure_ascii=False).encode("utf-8")), 0)
            # Legacy JSON is replaced on every append; it has no incremental cursor.
        else:
            source.seek(start)
            if start == 0:
                voice_end = 0
            while source.tell() < end:
                offset = source.tell()
                raw = source.readline(min(limits.max_record_bytes + 1, end - offset))
                if not raw:
                    raise HistoryReplayError("History ended before its durable boundary")
                if len(raw) > limits.max_record_bytes:
                    raise HistoryReplayError(f"History record at byte {offset} exceeds the byte budget",
                                             code="HISTORY_RESTORE_LIMIT")
                if not raw.strip():
                    continue
                record = json.loads(raw)
                keep(record, len(raw), offset)
                if _is_voice(record):
                    voice_end = source.tell()
        if _identity(os.fstat(source.fileno())) != identity or _identity(path.stat()) != identity:
            raise HistoryReplayError("History changed during restore", code="HISTORY_RESTORE_CHANGED")
    if voice_end is not None and path.suffix.lower() == ".jsonl":
        _remember(path, _HistoryHead(identity, voice_end))
    end_cursor = HistoryReplayCursor(path, generation, identity, end)
    prefix = HistoryReplayCursor(path, generation, identity, prefix_offset if prefix_offset is not None else end)
    return HistoryReplayBatch(records, end_cursor, prefix, end - start)
