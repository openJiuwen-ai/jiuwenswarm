"""Isolated OpenClaw-format history captured from model runtime callbacks.

The regular ``history.jsonl`` remains the business source of truth. This module
only writes a separate transcript for test analysis. Every public entry point
is fail-safe: queue saturation or filesystem errors never propagate into chat.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir, mask_sensitive

logger = logging.getLogger(__name__)

# Code-level kill switches and retention controls. No environment variables are
# involved so a beta can be built with deterministic behavior.
OPENCLAW_HISTORY_ENABLED = True
OPENCLAW_HISTORY_DIRNAME = "openclaw_history"
OPENCLAW_HISTORY_RETENTION_DAYS = 7
OPENCLAW_HISTORY_MAX_BYTES = 256 * 1024 * 1024
OPENCLAW_HISTORY_HIGH_WATER_RATIO = 0.8
OPENCLAW_HISTORY_CLEANUP_INTERVAL_SECONDS = 60 * 60
OPENCLAW_HISTORY_ACTIVE_LEASE_SECONDS = 24 * 60 * 60
OPENCLAW_HISTORY_MAX_STRING_CHARS = 2 * 1024
OPENCLAW_HISTORY_MAX_LIST_ITEMS = 100
OPENCLAW_HISTORY_MAX_DEPTH = 8

_SECRET_TOKENS = frozenset(
    {
        "token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "apikey",
        "authorization",
        "authorisation",
        "cookie",
    }
)

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$")
_QUEUE: queue.Queue[tuple[str, int, dict[str, Any], int]] = queue.Queue(maxsize=20000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SESSIONS: dict[str, tuple[int, float]] = {}
_DELETED_LOCK = threading.Lock()
_DELETED_SESSIONS: set[str] = set()
_GENERATION_LOCK = threading.Lock()
_SESSION_GENERATIONS: dict[str, int] = {}
_SESSION_IO_LOCK = threading.RLock()
_LAST_CLEANUP_SECONDS = 0.0


def openclaw_history_path(session_id: str) -> Path:
    """Return the isolated transcript path for *session_id*."""
    sid = str(session_id or "").strip()
    if sid == "default" or not _SAFE_SESSION_ID.fullmatch(sid):
        raise ValueError(f"invalid session id for OpenClaw history: {sid!r}")
    return get_agent_sessions_dir().parent / OPENCLAW_HISTORY_DIRNAME / f"{sid}.jsonl"


def _open_private_text(path: Path, mode: str):
    """Open a diagnostic file with owner-only permissions where supported."""
    flags = os.O_WRONLY | os.O_CREAT
    if mode == "x":
        flags |= os.O_EXCL
    elif mode == "a":
        flags |= os.O_APPEND
    else:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, mode.replace("x", "w"), encoding="utf-8")


def _sanitize_openclaw_value(value: Any, *, depth: int = 0) -> Any:
    """Mask secrets and bound diagnostic payload size without mutating input."""
    if depth > OPENCLAW_HISTORY_MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, str):
        original_chars = len(value)
        bounded = value[:OPENCLAW_HISTORY_MAX_STRING_CHARS]
        sanitized = mask_sensitive(bounded)
        if original_chars > OPENCLAW_HISTORY_MAX_STRING_CHARS:
            return (
                sanitized + f" [truncated, original_chars={original_chars}]"
            )
        return sanitized
    if isinstance(value, dict):
        sanitized_dict: dict[str, Any] = {}
        for key, item in list(value.items())[:OPENCLAW_HISTORY_MAX_LIST_ITEMS]:
            key_text = mask_sensitive(str(key))
            if _looks_secret(str(key)):
                sanitized_dict[key_text] = "******"
            else:
                sanitized_dict[key_text] = _sanitize_openclaw_value(
                    item, depth=depth + 1
                )
        return sanitized_dict
    if isinstance(value, (list, tuple)):
        items = list(value[:OPENCLAW_HISTORY_MAX_LIST_ITEMS])
        sanitized_items = [
            _sanitize_openclaw_value(item, depth=depth + 1) for item in items
        ]
        if len(value) > len(items):
            sanitized_items.append(f"<truncated, omitted={len(value) - len(items)}>")
        return sanitized_items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_openclaw_value(str(value), depth=depth + 1)


def _looks_secret(key: str) -> bool:
    """Match secret-like key tokens without treating token counts as secrets."""
    tokens = {part for part in re.split(r"[^a-z0-9]+", key.lower()) if part}
    if not tokens or "tokens" in tokens:
        return False
    if tokens & _SECRET_TOKENS:
        return True
    return {"api", "key"} <= tokens


def mark_openclaw_session_active(session_id: str) -> None:
    """Protect a live session transcript from retention cleanup."""
    sid = str(session_id or "").strip()
    if sid == "default" or not _SAFE_SESSION_ID.fullmatch(sid):
        return
    with _DELETED_LOCK:
        _DELETED_SESSIONS.discard(sid)
    with _ACTIVE_LOCK:
        count = _ACTIVE_SESSIONS.get(sid, (0, time.time()))[0]
        _ACTIVE_SESSIONS[sid] = (count + 1, time.time())


def release_openclaw_session(session_id: str) -> None:
    """Release one active-session reference."""
    sid = str(session_id or "").strip()
    with _ACTIVE_LOCK:
        count = _ACTIVE_SESSIONS.get(sid, (0, 0.0))[0]
        if count <= 1:
            _ACTIVE_SESSIONS.pop(sid, None)
        else:
            _ACTIVE_SESSIONS[sid] = (count - 1, time.time())


def _touch_active_session(session_id: str) -> None:
    sid = str(session_id or "").strip()
    with _ACTIVE_LOCK:
        current = _ACTIVE_SESSIONS.get(sid)
        if current is not None:
            _ACTIVE_SESSIONS[sid] = (current[0], time.time())


def _is_deleted_session(session_id: str) -> bool:
    with _DELETED_LOCK:
        return session_id in _DELETED_SESSIONS


def _current_generation(session_id: str) -> int:
    with _GENERATION_LOCK:
        return _SESSION_GENERATIONS.get(session_id, 0)


def _active_sessions_snapshot(now: float | None = None) -> set[str]:
    current = time.time() if now is None else float(now)
    cutoff = current - OPENCLAW_HISTORY_ACTIVE_LEASE_SECONDS
    with _ACTIVE_LOCK:
        expired = [
            session_id
            for session_id, (_, last_seen) in _ACTIVE_SESSIONS.items()
            if last_seen < cutoff
        ]
        for session_id in expired:
            _ACTIVE_SESSIONS.pop(session_id, None)
        return set(_ACTIVE_SESSIONS)


def _iso_timestamp(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cleanup_openclaw_history(*, now: float | None = None) -> dict[str, int]:
    """Apply age and disk-usage retention to diagnostic transcripts."""
    current = time.time() if now is None else float(now)
    directory = get_agent_sessions_dir().parent / OPENCLAW_HISTORY_DIRNAME
    result = {"removed": 0, "errors": 0, "remaining_bytes": 0}
    try:
        if not directory.is_dir():
            return result
        cutoff = current - OPENCLAW_HISTORY_RETENTION_DAYS * 24 * 60 * 60
        active = _active_sessions_snapshot(now=current)
        candidates: list[tuple[Path, int, float]] = []
        total_bytes = 0
        for path in directory.iterdir():
            try:
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.suffix != ".jsonl"
                    or not _SAFE_SESSION_ID.fullmatch(path.stem)
                ):
                    continue
                stat = path.stat()
            except OSError:
                result["errors"] += 1
                continue
            candidates.append((path, stat.st_size, stat.st_mtime))
            total_bytes += stat.st_size

        for path, size, mtime in list(candidates):
            if path.stem in active or mtime >= cutoff:
                continue
            try:
                path.unlink()
                candidates.remove((path, size, mtime))
                total_bytes -= size
                result["removed"] += 1
            except OSError:
                result["errors"] += 1

        high_water = int(OPENCLAW_HISTORY_MAX_BYTES * OPENCLAW_HISTORY_HIGH_WATER_RATIO)
        if total_bytes > OPENCLAW_HISTORY_MAX_BYTES:
            for path, size, _ in sorted(candidates, key=lambda item: item[2]):
                if total_bytes <= high_water:
                    break
                if path.stem in active:
                    continue
                try:
                    path.unlink()
                    total_bytes -= size
                    result["removed"] += 1
                except OSError:
                    result["errors"] += 1
        result["remaining_bytes"] = max(0, total_bytes)
    except Exception:
        result["errors"] += 1
        logger.warning("[OpenClawHistory] cleanup failed", exc_info=True)
    return result


def delete_openclaw_history(session_id: str) -> bool:
    """Remove a deleted conversation's diagnostic transcript.

    A short-lived tombstone drops records that were already queued when the
    business session was deleted. A later invocation for the same session id
    clears the tombstone and starts a fresh transcript.
    """
    sid = str(session_id or "").strip()
    if sid == "default" or not _SAFE_SESSION_ID.fullmatch(sid):
        return False
    with _SESSION_IO_LOCK:
        with _DELETED_LOCK:
            _DELETED_SESSIONS.add(sid)
        with _GENERATION_LOCK:
            _SESSION_GENERATIONS[sid] = _SESSION_GENERATIONS.get(sid, 0) + 1
        with _ACTIVE_LOCK:
            _ACTIVE_SESSIONS.pop(sid, None)
        try:
            openclaw_history_path(sid).unlink(missing_ok=True)
            return True
        except Exception:
            logger.warning(
                "[OpenClawHistory] delete failed session_id=%s",
                sid,
                exc_info=True,
            )
            return False


def _maybe_cleanup_history() -> None:
    global _LAST_CLEANUP_SECONDS
    now = time.time()
    if now - _LAST_CLEANUP_SECONDS < OPENCLAW_HISTORY_CLEANUP_INTERVAL_SECONDS:
        return
    _LAST_CLEANUP_SECONDS = now
    cleanup_openclaw_history(now=now)


def _ensure_session_header(path: Path, session_id: str) -> str | None:
    """Ensure an OpenClaw v3 header exists and return the current leaf id."""
    exists = path.exists()
    if exists:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    if not exists or path.stat().st_size == 0:
        header = {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": _iso_timestamp(int(time.time() * 1000)),
            "cwd": os.getcwd(),
        }
        with _open_private_text(path, "w" if exists else "x") as handle:
            handle.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        return None

    first_line = ""
    leaf_id: str | None = None
    needs_utf8_repair = False
    with path.open("rb") as handle:
        for index, raw_line in enumerate(handle):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                line = raw_line.decode("utf-8", errors="replace")
                needs_utf8_repair = True
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                if index == 0:
                    raise
                continue
            if index == 0:
                if (
                    not isinstance(parsed, dict)
                    or parsed.get("type") != "session"
                    or parsed.get("version") != 3
                    or not str(parsed.get("id") or "").strip()
                ):
                    raise ValueError(f"invalid OpenClaw session header: {path}")
                first_line = line
                continue
            if (
                isinstance(parsed, dict)
                and parsed.get("type") == "message"
                and isinstance(parsed.get("id"), str)
                and parsed["id"]
            ):
                leaf_id = parsed["id"]
    if not first_line:
        raise ValueError(f"missing OpenClaw session header: {path}")
    if needs_utf8_repair:
        repaired = path.read_text(encoding="utf-8", errors="replace")
        with _open_private_text(path, "w") as handle:
            handle.write(repaired)
    return leaf_id


def _ensure_trailing_newline(path: Path) -> None:
    """Separate a crash-truncated tail from the next valid JSONL record."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return
    with path.open("ab") as handle:
        handle.write(b"\n")


def _worker() -> None:
    leaf_ids: dict[str, str | None] = {}
    failed_sessions: set[str] = set()
    while True:
        session_id, generation, message, timestamp_ms = _QUEUE.get()
        try:
            if generation != _current_generation(session_id):
                failed_sessions.discard(session_id)
                leaf_ids.pop(session_id, None)
                continue
            if session_id in failed_sessions:
                continue
            with _SESSION_IO_LOCK:
                if generation != _current_generation(session_id):
                    failed_sessions.discard(session_id)
                    leaf_ids.pop(session_id, None)
                    continue
                if _is_deleted_session(session_id):
                    continue
                _maybe_cleanup_history()
                path = openclaw_history_path(session_id)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.chmod(path.parent, 0o700)
                except OSError:
                    pass
                if (
                    session_id not in leaf_ids
                    or not path.exists()
                    or path.stat().st_size == 0
                ):
                    leaf_ids[session_id] = _ensure_session_header(path, session_id)
                _ensure_trailing_newline(path)
                record = {
                    "type": "message",
                    "id": str(uuid.uuid4()),
                    "parentId": leaf_ids[session_id],
                    "timestamp": _iso_timestamp(timestamp_ms),
                    "message": _sanitize_openclaw_value(
                        {**message, "timestamp": timestamp_ms}
                    ),
                }
                try:
                    line = json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                except (TypeError, ValueError):
                    logger.warning(
                        "[OpenClawHistory] record serialization failed; dropping record "
                        "session_id=%s",
                        session_id,
                    )
                    continue
                with _open_private_text(path, "a") as handle:
                    handle.write(line)
                    handle.write("\n")
                leaf_ids[session_id] = record["id"]
        except (TypeError, ValueError):
            failed_sessions.add(session_id)
            logger.warning(
                "[OpenClawHistory] invalid transcript; disabling session append "
                "session_id=%s",
                session_id,
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "[OpenClawHistory] append failed session_id=%s",
                session_id,
                exc_info=True,
            )
        finally:
            _QUEUE.task_done()


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(
            target=_worker,
            name="openclaw-runtime-history-writer",
            daemon=True,
        ).start()
        _WORKER_STARTED = True


def append_openclaw_message(
    session_id: str,
    message: dict[str, Any],
    *,
    timestamp_ms: int | None = None,
) -> None:
    """Queue one runtime message for isolated persistence."""
    if not OPENCLAW_HISTORY_ENABLED or not isinstance(message, dict):
        return
    sid = str(session_id or "").strip()
    if sid == "default" or not _SAFE_SESSION_ID.fullmatch(sid):
        return
    try:
        _touch_active_session(sid)
        _ensure_worker_started()
        generation = _current_generation(sid)
        _QUEUE.put_nowait(
            (
                sid,
                generation,
                message,
                int(timestamp_ms if timestamp_ms is not None else time.time() * 1000),
            )
        )
    except queue.Full:
        logger.warning(
            "[OpenClawHistory] queue full; dropping record session_id=%s",
            sid,
        )
    except Exception:
        logger.warning(
            "[OpenClawHistory] enqueue failed session_id=%s",
            sid,
            exc_info=True,
        )


def flush_openclaw_history(timeout: float = 5.0) -> None:
    """Wait for already queued diagnostic records to reach disk."""
    if not _WORKER_STARTED:
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while _QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)


__all__ = [
    "OPENCLAW_HISTORY_ENABLED",
    "OPENCLAW_HISTORY_RETENTION_DAYS",
    "OPENCLAW_HISTORY_MAX_BYTES",
    "append_openclaw_message",
    "cleanup_openclaw_history",
    "delete_openclaw_history",
    "flush_openclaw_history",
    "mark_openclaw_session_active",
    "openclaw_history_path",
    "release_openclaw_session",
]
