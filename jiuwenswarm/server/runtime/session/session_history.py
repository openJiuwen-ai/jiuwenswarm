from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import logging
import json
import os
import queue
import re
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Iterator

from jiuwenswarm.common.utils import get_agent_sessions_dir, is_persistable_session_id


logger = logging.getLogger(__name__)
# Writers can read during legacy append/bootstrap or truncation while holding this lock.
_FILE_LOCK = threading.RLock()
_WRITE_QUEUE: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=20000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_LEGACY_HISTORY_FILENAME = "history.json"
_JSONL_HISTORY_FILENAME = "history.jsonl"
_LEGACY_HISTORY_ENV = "JIUWENSWARM_USE_LEGACY_HISTORY_JSON"
_HEARTBEAT_OK = "HEARTBEAT_OK"
_HISTORY_FLUSH_ASYNC_TIMEOUT_SECONDS = 5.0
_HISTORY_CURSOR_VERSION = 1
_HISTORY_CURSOR_READ_BLOCK_BYTES = 64 * 1024
_HISTORY_CURSOR_MAX_TOKEN_CHARS = 6 * 1024 * 1024
_HISTORY_CURSOR_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
_HISTORY_CURSOR_MAX_DEDUP_KEYS = 256
_HISTORY_CURSOR_MAX_TEXTS_PER_KEY = 128
_HISTORY_CURSOR_MAX_TEXT_CHARS = 2 * 1024 * 1024
_VALID_SESSION_ID = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$"
)


class InvalidHistoryCursor(ValueError):
    """The cursor is malformed, invalid, or belongs to another history."""


class HistorySnapshotChanged(RuntimeError):
    """The history file was replaced or truncated after the snapshot began."""


def is_valid_session_id(session_id: str) -> bool:
    """Return whether a session id is safe to use as one path component."""

    return _VALID_SESSION_ID.fullmatch(session_id) is not None


def _is_ephemeral_heartbeat_session(session_id: str) -> bool:
    """Heartbeat sessions are one-shot and should not pollute history.json(l)."""
    return (session_id or "").startswith("heartbeat")


def _has_persistable_assistant_payload(
    *,
    content_text: str,
    event_type: str | None,
    extra: dict[str, Any] | None,
) -> bool:
    """Return False for blank assistant shells that would show as empty history rows."""
    content = (content_text or "").strip()
    if content.upper() == _HEARTBEAT_OK:
        return False

    et = str(event_type or "").strip()
    payload = extra if isinstance(extra, dict) else {}
    if et == "chat.final" and payload.get("aborted") is True:
        return True
    if content:
        return True
    if str(payload.get("reasoning_content") or "").strip():
        return True
    if et == "chat.file" and payload.get("files"):
        return True
    if et == "chat.reference" and payload.get("references"):
        return True
    if et == "chat.tool_call" and (payload.get("tool_call") or payload.get("tool_calls")):
        return True
    if et == "chat.tool_result" and (payload.get("tool_result") or payload.get("tool_call_id")):
        return True
    if payload.get("error") or payload.get("files"):
        return True
    if payload.get("tool_call") or payload.get("tool_calls"):
        return True
    # Empty chat.final / chat.* status shells and other blank assistants: skip.
    if et.startswith("chat.") or et in {"", "chat.final"}:
        return False
    # team.* / context.* monitor events may carry structured extras without content.
    return bool(payload)


def _serialize_value_with_flag(obj: Any) -> tuple[Any, bool]:
    """将对象转换为 JSON 可序列化的格式，并返回是否发生降级处理."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj, False
    if isinstance(obj, datetime.datetime):
        return obj.isoformat(), True
    if isinstance(obj, datetime.date):
        return obj.isoformat(), True
    if callable(obj):
        name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) or type(obj).__name__
        return f"<callable:{name}>", True
    if isinstance(obj, dict):
        changed = False
        serialized: dict[Any, Any] = {}
        for k, v in obj.items():
            serialized_value, value_changed = _serialize_value_with_flag(v)
            serialized[k] = serialized_value
            changed = changed or value_changed
        return serialized, changed
    if isinstance(obj, (list, tuple, set, frozenset)):
        changed = not isinstance(obj, list)
        serialized_items = []
        for item in obj:
            serialized_item, item_changed = _serialize_value_with_flag(item)
            serialized_items.append(serialized_item)
            changed = changed or item_changed
        return serialized_items, changed
    try:
        json.dumps(obj, ensure_ascii=False)
    except TypeError:
        return repr(obj), True
    return obj, False


def _serialize_value(obj: Any) -> Any:
    return _serialize_value_with_flag(obj)[0]


def _session_dir(session_id: str, *, create: bool = True) -> Path:
    session_dir = get_agent_sessions_dir() / session_id
    if create:
        session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def resolve_session_dir(
    session_id: str, *, create: bool = False, sessions_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """安全解析 session 目录路径（防路径遍历）。

    采用严格白名单判据：session id 只能包含 ASCII 字母、数字、点、横线和下划线，
    长度不超过 80，且首尾必须是字母或数字。不合法输入直接拒绝，根本不拼路径。

    再用 ``resolve()`` + ``relative_to`` 做纵深防御，兜底白名单逻辑被绕过的极端情况。

    Args:
        session_id: 待校验的 session id（调用方应先 ``.strip()``）。
        create: 是否创建目录（delete 流程传 False）。
        sessions_root: sessions 根目录。由调用方传入

    Returns:
        ``(resolved_path, None)`` —— 合法，返回解析后的绝对路径（确认在 sessions 目录内）。
        ``(None, error_reason)`` —— 非法，根本未触碰磁盘路径。
    """
    if not session_id or not is_valid_session_id(session_id):
        return None, "invalid session_id"

    if sessions_root is None:
        sessions_root = get_agent_sessions_dir()
    session_dir = sessions_root / session_id
    # 纵深防御必须在 mkdir 之前：先 resolve + relative_to 确认路径仍在 sessions
    # 目录内，通过后才允许创建。否则白名单一旦被绕过，mkdir(parents=True) 会
    # 先在 sessions 根目录之外越界创建目录，relative_to 才事后检测到——此时
    # 副作用已发生，越界空目录残留在磁盘上（虽不触发 rmtree，但仍是文件系统泄漏）。
    try:
        resolved = session_dir.resolve(strict=False)
        resolved.relative_to(sessions_root.resolve(strict=False))
    except (ValueError, OSError):
        return None, "invalid session_id"
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved, None


def _history_file(session_id: str, *, create: bool = True) -> Path:
    return _session_dir(session_id, create=create) / _LEGACY_HISTORY_FILENAME


def _history_jsonl_file(session_id: str, *, create: bool = True) -> Path:
    return _session_dir(session_id, create=create) / _JSONL_HISTORY_FILENAME


def use_legacy_history_json() -> bool:
    raw = str(os.environ.get(_LEGACY_HISTORY_ENV, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_write_history_path(session_id: str) -> Path:
    """Return the preferred durable history write target for a session."""
    if use_legacy_history_json():
        return _history_file(session_id)
    return _history_jsonl_file(session_id)


def get_read_history_path(session_id: str) -> Path:
    """Return the preferred history source, falling back to legacy json."""
    if use_legacy_history_json():
        legacy_path = _history_file(session_id, create=False)
        if legacy_path.exists():
            return legacy_path
        jsonl_path = _history_jsonl_file(session_id, create=False)
        if jsonl_path.exists():
            return jsonl_path
        return legacy_path

    jsonl_path = _history_jsonl_file(session_id, create=False)
    if jsonl_path.exists():
        return jsonl_path
    legacy_path = _history_file(session_id, create=False)
    if legacy_path.exists():
        return legacy_path
    return jsonl_path


def history_exists(session_id: str) -> bool:
    return get_read_history_path(session_id).exists()


def get_history_mtime(session_id: str) -> float | None:
    path = get_read_history_path(session_id)
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with _FILE_LOCK:
            text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 history.json 失败，已忽略并重建: %s", exc)
        return []
    if isinstance(data, list):
        return data
    return []


def _read_history_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        # Capture a complete write under the storage lock; parse the snapshot unlocked.
        with _FILE_LOCK:
            text = path.read_text(encoding="utf-8")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 history.jsonl 第 %d 行失败，已跳过: %s", lineno, exc)
                continue
            if isinstance(item, dict):
                records.append(item)
            else:
                logger.warning(
                    "读取 history.jsonl 第 %d 行不是对象记录，已跳过: %s",
                    lineno,
                    type(item).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 history.jsonl 失败，已忽略: %s", exc)
        return []
    return records


def _history_dedup_key(record: dict[str, Any]) -> str:
    record_id = str(record.get("id") or "").strip()
    bubble_seq = record.get("bubble_seq")
    if bubble_seq is not None:
        return f"{record_id}#b{bubble_seq}"
    return record_id


def _history_final_text(record: dict[str, Any]) -> str:
    content = record.get("content")
    return content.strip() if isinstance(content, str) else ""


def _dedup_records_last_wins(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同 id 的 chat.final 记录去重（内容前缀语义，替代 last-wins）。

    一轮内 chat.final 会多次落盘，id 均为 ``<request_id>:assistant``，但内容
    分两种形态：
    - 「累计快照」：中间稿是终稿前缀，前端会重复展示——丢弃；
    - 「独立正文段」：多次尝试各自的正文（穿插时间线重建依赖）——保留。
    因此只丢弃「终稿前缀」或「与已保留正文逐字重复（重试同稿）」的中间稿，
    其余中间稿全部保留；无 id 记录原样保留。

    只收敛 event_type=="chat.final"：tool_call/tool_result 等记录合法共享
    回合 rid（同 id 多条是正常形态），不能误收。

    多气泡（leader 同轮多次发言）：各泡 chat.final 带 ``bubble_seq``，共享 id
    但语义上是不同卡片——去重键带上 bubble_seq（同一泡的快照重写仍按前缀
    语义收敛，不同泡互不收敛）。无 bubble_seq 的存量记录行为不变。
    """
    _dedup_key = _history_dedup_key
    _record_text = _history_final_text

    # 逐 key 收集 finals，从后往前链式判定：存在「后续同 key final 以本稿为前缀」
    # （累计快照链，不限是否为终稿）→ 本稿丢弃；否则独立正文段保留。
    final_indices_by_key: dict[str, list[tuple[int, str]]] = {}
    for index, record in enumerate(records):
        if str(record.get("event_type") or "") != "chat.final":
            continue
        key = _dedup_key(record)
        if not key:
            continue
        final_indices_by_key.setdefault(key, []).append((index, _record_text(record)))

    drop_indices: set[int] = set()
    resumed_stop_indices: set[int] = set()
    # fork/截断可能把已归并的停止字段写回工具记录；恢复时一并清除。
    for index, record in enumerate(records):
        finals = final_indices_by_key.get(_dedup_key(record))
        if record.get("aborted") is True and finals and index < finals[-1][0]:
            resumed_stop_indices.add(index)
    for finals in final_indices_by_key.values():
        later_texts: list[str] = []
        for index, text in reversed(finals):
            # 同 rid 恢复执行后的新 final 取代停止标记（包括无正文标记）。
            if index in resumed_stop_indices and not text:
                drop_indices.add(index)
                continue
            if text and any(later.startswith(text) for later in later_texts):
                drop_indices.add(index)
            later_texts.append(text)

    kept: list[dict[str, Any]] = []
    kept_text_by_key: dict[str, str] = {}
    for index, record in enumerate(records):
        if index in resumed_stop_indices:
            record = {k: v for k, v in record.items() if k != "aborted"}
        if str(record.get("event_type") or "") != "chat.final":
            kept.append(record)
            continue
        key = _dedup_key(record)
        if not key:
            kept.append(record)
            continue
        if index in drop_indices:
            continue
        text = _record_text(record)
        # 与已保留正文逐字重复（重试同稿）也丢弃
        if text and kept_text_by_key.get(key) == text:
            continue
        kept_text_by_key[key] = text
        kept.append(record)
    # 空停止记录附到原轮已有记录上，避免同 assistant id 多记录在前端串轮。
    # 零产出轮没有可合并对象，保留独立标记供历史恢复。
    merged: list[dict[str, Any]] = []
    previous_assistant: dict[str, int] = {}
    for record in kept:
        key = _dedup_key(record)
        et = str(record.get("event_type") or "")
        previous = previous_assistant.get(key) if key else None
        if et == "chat.final" and record.get("aborted") is True and not _record_text(record) and previous is not None:
            marked = {**merged[previous], "aborted": True}
            reasoning = str(record.get("reasoning_content") or "")
            if reasoning:
                marked["reasoning_content"] = str(marked.get("reasoning_content") or "") + reasoning
            merged[previous] = marked
            continue
        merged.append(record)
        if key and (record.get("role") == "assistant" or et.startswith("chat.")):
            previous_assistant[key] = len(merged) - 1
    return merged


def load_history_records(session_id: str) -> list[dict[str, Any]]:
    path = get_read_history_path(session_id)
    if path.suffix.lower() == ".jsonl":
        return _dedup_records_last_wins(_read_history_jsonl(path))
    return _dedup_records_last_wins(_read_history(path))


_JSONL_TAIL_CHUNK = 8192


def _iter_jsonl_file_lines_reversed(path: Path):
    """Yield non-empty jsonl lines from EOF backward as raw bytes."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        buffer = b""
        while position > 0:
            step = min(_JSONL_TAIL_CHUNK, position)
            position -= step
            fh.seek(position)
            buffer = fh.read(step) + buffer
            lines = buffer.split(b"\n")
            buffer = lines[0]
            for raw in reversed(lines[1:]):
                stripped = raw.strip()
                if stripped:
                    yield stripped
        stripped = buffer.strip()
        if stripped:
            yield stripped


def _request_id_from_raw_history_line(raw: bytes, *, exclude_request_id: str) -> str | None:
    try:
        item = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(item, dict):
        return None
    rid = str(item.get("request_id") or "").strip()
    if not rid or (exclude_request_id and rid == exclude_request_id):
        return None
    return rid


def load_history_tail_request_id(
    session_id: str,
    *,
    exclude_request_id: str | None = None,
) -> str | None:
    """Last non-empty ``request_id`` on disk, optionally skipping the in-flight turn.

    ``history.jsonl`` is scanned from EOF so fingerprint compares do not load
    the whole file. Legacy ``history.json`` is small and still read in full.
    """
    path = get_read_history_path(session_id)
    if not path.exists():
        return None
    excluded = (exclude_request_id or "").strip()
    if path.suffix.lower() == ".jsonl":
        for raw in _iter_jsonl_file_lines_reversed(path):
            rid = _request_id_from_raw_history_line(raw, exclude_request_id=excluded)
            if rid:
                return rid
        return None
    records = _dedup_records_last_wins(_read_history(path))
    tail: str | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        rid = str(record.get("request_id") or "").strip()
        if not rid or (excluded and rid == excluded):
            continue
        tail = rid
    return tail


def _write_records_to_path(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonl":
        payload = "\n".join(
            json.dumps(record, ensure_ascii=False) for record in records
        )
        if payload:
            payload += "\n"
        path.write_text(payload, encoding="utf-8")
        return

    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_record_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


def _ensure_jsonl_bootstrap(session_id: str) -> Path:
    jsonl_path = _history_jsonl_file(session_id)
    if jsonl_path.exists():
        return jsonl_path

    legacy_path = _history_file(session_id)
    if legacy_path.exists():
        legacy_records = _read_history(legacy_path)
        _write_records_to_path(jsonl_path, legacy_records)
    else:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return jsonl_path


def _write_records_to_jsonl_atomically(path: Path, records: list[dict[str, Any]]) -> None:
    """Replace a JSONL history without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            temporary_path = Path(fh.name)
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _cursor_stream_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _encode_history_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    compressed = zlib.compress(raw.encode("ascii"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor.strip():
        raise InvalidHistoryCursor("history cursor must be a non-empty string")
    token = cursor.strip()
    if len(token) > _HISTORY_CURSOR_MAX_TOKEN_CHARS:
        raise InvalidHistoryCursor("history cursor payload exceeds size limit")
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        # Accept cursors issued before dedup state was embedded.
        if decoded.startswith(b"{"):
            if len(decoded) > _HISTORY_CURSOR_MAX_PAYLOAD_BYTES:
                raise InvalidHistoryCursor(
                    "history cursor payload exceeds size limit"
                )
            raw = decoded
        else:
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(
                decoded,
                _HISTORY_CURSOR_MAX_PAYLOAD_BYTES + 1,
            )
            if (
                len(raw) > _HISTORY_CURSOR_MAX_PAYLOAD_BYTES
                or decompressor.unconsumed_tail
            ):
                raise InvalidHistoryCursor(
                    "history cursor payload exceeds size limit"
                )
            remaining = _HISTORY_CURSOR_MAX_PAYLOAD_BYTES + 1 - len(raw)
            raw += decompressor.flush(remaining)
            if (
                len(raw) > _HISTORY_CURSOR_MAX_PAYLOAD_BYTES
                or not decompressor.eof
                or decompressor.unused_data
            ):
                raise InvalidHistoryCursor(
                    "history cursor payload exceeds size limit"
                )
        payload = json.loads(raw.decode("ascii"))
    except InvalidHistoryCursor:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InvalidHistoryCursor("history cursor is malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != _HISTORY_CURSOR_VERSION:
        raise InvalidHistoryCursor("history cursor version is unsupported")
    return payload


def _history_snapshot_id(
    *, stream_key: str, device: int, inode: int, snapshot_end: int
) -> str:
    raw = f"{stream_key}:{device}:{inode}:{snapshot_end}".encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _ensure_cursor_jsonl_path(session_id: str) -> Path:
    """Return JSONL storage, atomically migrating legacy JSON once when enabled."""
    path = get_read_history_path(session_id)
    if path.suffix.lower() == ".jsonl":
        return path
    if use_legacy_history_json():
        raise InvalidHistoryCursor(
            "cursor history requires JSONL storage; "
            "disable JIUWENSWARM_USE_LEGACY_HISTORY_JSON"
        )
    records = _read_history(path)
    jsonl_path = path.with_name(_JSONL_HISTORY_FILENAME)
    _write_records_to_jsonl_atomically(jsonl_path, records)
    return jsonl_path


def _iter_reverse_jsonl_lines(
    fh: Any,
    *,
    start_position: int,
    read_block_bytes: int,
    bytes_read: list[int],
) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(line, start, end)`` from newest to oldest without splitting UTF-8."""
    position = start_position
    tail = b""
    while position > 0:
        read_start = max(0, position - read_block_bytes)
        fh.seek(read_start)
        chunk = fh.read(position - read_start)
        bytes_read[0] += len(chunk)
        data = chunk + tail
        search_end = len(data)
        while True:
            newline = data.rfind(b"\n", 0, search_end)
            if newline < 0:
                break
            line_start = newline + 1
            yield (
                data[line_start:search_end].rstrip(b"\r"),
                read_start + line_start,
                read_start + search_end,
            )
            search_end = newline
        tail = data[:search_end]
        position = read_start

    if tail:
        yield tail.rstrip(b"\r"), 0, len(tail)


def _load_cursor_dedup_state(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_state = payload.get("dedup", {})
    if raw_state is None:
        return {}
    if not isinstance(raw_state, dict):
        raise InvalidHistoryCursor("history cursor dedup state is invalid")
    if len(raw_state) > _HISTORY_CURSOR_MAX_DEDUP_KEYS:
        raise InvalidHistoryCursor("history cursor dedup state is too large")

    state: dict[str, dict[str, Any]] = {}
    total_text_chars = 0
    for key, item in raw_state.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(item, dict)
        ):
            raise InvalidHistoryCursor("history cursor dedup state is invalid")
        texts = item.get("texts", [])
        pending = item.get("pending")
        pending_position = item.get("pending_position")
        if (
            not isinstance(texts, list)
            or len(texts) > _HISTORY_CURSOR_MAX_TEXTS_PER_KEY
            or any(not isinstance(text, str) for text in texts)
            or not isinstance(item.get("last", ""), str)
            or not isinstance(item.get("has_final", False), bool)
            or not isinstance(item.get("request", ""), str)
            or (pending is not None and not isinstance(pending, dict))
            or (
                pending_position is not None
                and (
                    not isinstance(pending_position, int)
                    or isinstance(pending_position, bool)
                    or pending_position < 0
                )
            )
        ):
            raise InvalidHistoryCursor("history cursor dedup state is invalid")
        last_text = item.get("last", "")
        total_text_chars += sum(len(text) for text in texts) + len(last_text)
        if total_text_chars > _HISTORY_CURSOR_MAX_TEXT_CHARS:
            raise InvalidHistoryCursor("history cursor dedup state is too large")
        state[key] = {
            "texts": texts,
            "last": last_text,
            "has_final": item.get("has_final", False),
            "request": item.get("request", ""),
            "pending": pending,
            "pending_position": pending_position,
        }
    return state


def _add_cursor_later_text(state: dict[str, Any], text: str) -> None:
    if not text:
        return
    later_texts = state["texts"]
    if any(later.startswith(text) for later in later_texts):
        return
    state["texts"] = [
        later for later in later_texts if not text.startswith(later)
    ]
    state["texts"].append(text)


def _attach_pending_abort(
    record: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return record
    marked = {**record, "aborted": True}
    reasoning = str(pending.get("reasoning_content") or "")
    if reasoning:
        marked["reasoning_content"] = (
            str(record.get("reasoning_content") or "") + reasoning
        )
    state["pending"] = None
    state["pending_position"] = None
    return marked


def _apply_cursor_dedup(
    record: dict[str, Any],
    state_by_key: dict[str, dict[str, Any]],
    output_position: int,
) -> tuple[dict[str, Any] | None, str]:
    """Apply the legacy chat.final normalization while reading newest-first."""
    key = _history_dedup_key(record)
    request_id = str(record.get("request_id") or "").strip()
    if not key:
        return record, request_id

    state = state_by_key.get(key)
    if state is None:
        if (
            str(record.get("event_type") or "") != "chat.final"
            and record.get("aborted") is not True
        ):
            return record, request_id
        state = state_by_key[key] = {
            "texts": [],
            "last": "",
            "has_final": False,
            "request": request_id,
            "pending": None,
            "pending_position": None,
        }
    state["request"] = request_id
    current = record
    if current.get("aborted") is True and state["has_final"]:
        current = {k: v for k, v in current.items() if k != "aborted"}

    if str(current.get("event_type") or "") == "chat.final":
        text = _history_final_text(current)
        prior_has_final = bool(state["has_final"])
        is_aborted_stop = record.get("aborted") is True and not text
        is_prefix_snapshot = bool(
            text and any(later.startswith(text) for later in state["texts"])
        )
        is_duplicate_retry = bool(text and state["last"] == text)
        _add_cursor_later_text(state, text)
        state["has_final"] = True

        if is_aborted_stop and prior_has_final:
            return None, request_id
        if text and (is_prefix_snapshot or is_duplicate_retry):
            return None, request_id
        if is_aborted_stop:
            state["pending"] = current
            state["pending_position"] = output_position
            return None, request_id

    event_type = str(current.get("event_type") or "")
    if state.get("pending") is not None and (
        current.get("role") == "assistant" or event_type.startswith("chat.")
    ):
        current = _attach_pending_abort(current, state)
    if event_type == "chat.final":
        state["last"] = _history_final_text(current)
    return current, request_id


def _flush_cursor_pending_aborts(
    records: list[dict[str, Any]],
    state_by_key: dict[str, dict[str, Any]],
) -> None:
    pending_items: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for state in state_by_key.values():
        pending = state.get("pending")
        if isinstance(pending, dict):
            position = state.get("pending_position")
            if not isinstance(position, int):
                position = len(records)
            pending_items.append((position, pending, state))
            state["pending"] = None
            state["pending_position"] = None
    # Insert from the largest index first so earlier insertions do not shift it.
    for position, pending, _ in sorted(
        pending_items,
        key=lambda item: item[0],
        reverse=True,
    ):
        records.insert(min(max(position, 0), len(records)), pending)


def _cursor_state_for_boundary(
    records: list[dict[str, Any]],
    state_by_key: dict[str, dict[str, Any]],
    boundary_request_id: str,
    lookahead_request_id: str,
) -> dict[str, dict[str, Any]]:
    continues_boundary = bool(
        boundary_request_id
        and boundary_request_id == lookahead_request_id
    )
    carried: dict[str, dict[str, Any]] = {}
    completed_pending: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for key, state in state_by_key.items():
        if continues_boundary and state.get("request") == boundary_request_id:
            if state.get("pending") is not None:
                state["pending_position"] = 0
            carried[key] = state
            continue
        pending = state.get("pending")
        if isinstance(pending, dict):
            position = state.get("pending_position")
            if not isinstance(position, int):
                position = len(records)
            completed_pending.append((position, pending, state))
            state["pending"] = None
            state["pending_position"] = None
    for position, pending, _ in sorted(
        completed_pending,
        key=lambda item: item[0],
        reverse=True,
    ):
        records.insert(min(max(position, 0), len(records)), pending)
    return carried


def read_history_cursor_page(
    session_id: str,
    *,
    cursor: str | None,
    limit: int,
    is_restorable: Callable[[dict[str, Any]], bool],
    read_block_bytes: int = _HISTORY_CURSOR_READ_BLOCK_BYTES,
) -> dict[str, Any]:
    """Read a newest-to-oldest JSONL batch from one fixed file snapshot.

    The initial call waits for queued writes before fixing ``snapshot_end``.
    Later calls validate the same file identity and ignore bytes appended after
    the snapshot, making retries with the same cursor idempotent.
    """
    normalized_session_id = (session_id or "").strip()
    if not normalized_session_id or not is_valid_session_id(normalized_session_id):
        raise InvalidHistoryCursor("invalid session_id")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
    ):
        raise InvalidHistoryCursor("history limit must be a positive integer")
    if read_block_bytes <= 0:
        raise InvalidHistoryCursor("history read block size must be positive")

    stream_key = _cursor_stream_key(normalized_session_id)
    cursor_payload = _decode_history_cursor(cursor) if cursor is not None else None
    if cursor_payload is not None and cursor_payload.get("stream") != stream_key:
        raise InvalidHistoryCursor("history cursor belongs to a different session")
    dedup_state = (
        _load_cursor_dedup_state(cursor_payload)
        if cursor_payload is not None
        else {}
    )

    if cursor_payload is None:
        flush_history_writes()
    with _FILE_LOCK:
        path = _ensure_cursor_jsonl_path(normalized_session_id)
        if not path.exists():
            return {
                "messages": [],
                "next_cursor": None,
                "has_more": False,
                "snapshot_id": None,
                "snapshot_end": 0,
                "scanned_bytes": 0,
            }

        stat = path.stat()
        device = int(stat.st_dev)
        inode = int(stat.st_ino)
        if cursor_payload is None:
            snapshot_end = int(stat.st_size)
            start_position = snapshot_end
        else:
            try:
                expected_device = int(cursor_payload["dev"])
                expected_inode = int(cursor_payload["ino"])
                snapshot_end = int(cursor_payload["end"])
                start_position = int(cursor_payload["pos"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidHistoryCursor("history cursor fields are invalid") from exc
            if (
                device != expected_device
                or inode != expected_inode
                or stat.st_size < snapshot_end
            ):
                raise HistorySnapshotChanged("history snapshot changed during restore")
            if not 0 <= start_position <= snapshot_end:
                raise InvalidHistoryCursor("history cursor position is outside the snapshot")

        snapshot_id = _history_snapshot_id(
            stream_key=stream_key,
            device=device,
            inode=inode,
            snapshot_end=snapshot_end,
        )
        records: list[dict[str, Any]] = []
        next_position = start_position
        has_more = False
        boundary_request_id = ""
        lookahead_request_id = ""
        bytes_read = [0]
        with path.open("rb") as fh:
            for raw_line, line_start, line_end in _iter_reverse_jsonl_lines(
                fh,
                start_position=start_position,
                read_block_bytes=read_block_bytes,
                bytes_read=bytes_read,
            ):
                next_position = line_start
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "读取 history.jsonl 游标记录失败，已跳过: session_id=%s error=%s",
                        normalized_session_id,
                        exc,
                    )
                    continue
                if not isinstance(item, dict) or not is_restorable(item):
                    continue
                if len(records) >= limit:
                    has_more = True
                    next_position = line_end
                    lookahead_request_id = str(
                        item.get("request_id") or ""
                    ).strip()
                    break
                record, request_id = _apply_cursor_dedup(
                    item,
                    dedup_state,
                    len(records),
                )
                if request_id:
                    boundary_request_id = request_id
                if record is not None:
                    records.append(record)

        if has_more:
            next_dedup_state = _cursor_state_for_boundary(
                records,
                dedup_state,
                boundary_request_id,
                lookahead_request_id,
            )
        else:
            _flush_cursor_pending_aborts(records, dedup_state)
            next_dedup_state = {}

        next_cursor = None
        if has_more:
            next_cursor = _encode_history_cursor(
                {
                    "v": _HISTORY_CURSOR_VERSION,
                    "stream": stream_key,
                    "dev": device,
                    "ino": inode,
                    "end": snapshot_end,
                    "pos": next_position,
                    "dedup": next_dedup_state,
                }
            )
        return {
            "messages": records,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "snapshot_id": snapshot_id,
            "snapshot_end": snapshot_end,
            "scanned_bytes": bytes_read[0],
        }


def _ensure_legacy_json_bootstrap(session_id: str) -> Path:
    legacy_path = _history_file(session_id)
    if legacy_path.exists():
        return legacy_path

    jsonl_path = _history_jsonl_file(session_id)
    if jsonl_path.exists():
        jsonl_records = _read_history_jsonl(jsonl_path)
        _write_records_to_path(legacy_path, jsonl_records)
    else:
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
    return legacy_path


def write_history_records(
    session_id: str,
    records: list[dict[str, Any]],
    *,
    preserve_existing_format: bool = True,
) -> Path:
    """Rewrite a session's history in its current format, defaulting new sessions to jsonl."""
    if not is_persistable_session_id(session_id):
        # 空 id 或 "default"（上游兜底占位符）不应落盘，避免凭空 mkdir
        # 出空的 default 目录。见 is_persistable_session_id 说明。
        raise ValueError(
            f"write_history_records: non-persistable session_id={session_id!r}"
        )
    path = (
        get_read_history_path(session_id)
        if preserve_existing_format
        else get_write_history_path(session_id)
    )
    with _FILE_LOCK:
        _write_records_to_path(path, records)
    return path


_TEAM_RELEVANT_EVENT_TYPES = frozenset({
    "team.message",
    "team.member",
    "team.task",
    "team.event",
    "chat.tool_call", "chat.tracer_agent",
    "chat.final", "chat.tool_result", "chat.file", "chat.html_card", "chat.reference",
})


def _is_team_relevant(item: dict[str, Any]) -> bool:
    et = item.get("event_type")
    if not isinstance(et, str):
        return False
    if et in _TEAM_RELEVANT_EVENT_TYPES:
        if et == "chat.file":
            role = item.get("role")
            return isinstance(role, str) and role.strip().lower() in {
                "assistant",
                "teammate",
            }
        if et in ("chat.tool_call", "chat.tracer_agent"):
            mode = item.get("mode")
            return isinstance(mode, str) and mode.strip().lower() == "team"
        if et in ("chat.final", "chat.tool_result", "chat.file", "chat.html_card", "chat.reference"):
            role = item.get("role")
            return isinstance(role, str) and role.strip().lower() == "teammate"
        return True
    return False


def read_team_history_records(session_id: str) -> list[dict[str, Any]]:
    """读取指定会话的历史记录，仅返回 team 模式相关的记录。"""
    fpath = get_read_history_path(session_id)
    all_records = load_history_records(session_id)
    # write_text 非原子写入（先截断再写入），读取可能命中截断窗口，
    # 用递增间隔重试最多 5 次等待写入完成
    if not all_records and fpath.exists():
        for attempt in range(1, 6):
            time.sleep(0.2 * attempt)
            all_records = load_history_records(session_id)
            if all_records:
                logger.info("read_team_history_records: recovered on retry %d", attempt)
                break
        if not all_records:
            logger.warning(
                "read_team_history_records: all retries exhausted, file_size=%d",
                fpath.stat().st_size,
            )

    return [item for item in all_records if isinstance(item, dict) and _is_team_relevant(item)]


def _read_history_by_path(path: Path) -> list[dict[str, Any]]:
    """根据文件扩展名选择正确的读取函数。"""
    if path.suffix.lower() == ".jsonl":
        return _read_history_jsonl(path)
    return _read_history(path)


def _is_member_relevant(item: dict[str, Any], member_name: str) -> bool:
    """判断一条 team 历史记录是否与指定 member 相关（用于飞书 /join 历史推送）。

    与实时 fan_out 投递语义一致：每个 member 只看到"涉及自己的对话"：
    - team.message.p2p 且 to_member 或 from_member == member_name →
      发给/由该成员发出的私聊（与 fan_out
      [godview, mention(to_member), private(from_member)] 对齐：收件人和
      发送方都能看到 P2P 卡片）
    - team.message.broadcast → @all 广播，所有人都能看到
    - chat.* teammate 流式输出 且 member_name == 该成员 →
      该成员扮演的 agent 的输出（与 fan_out [godview, private(member)] 对齐）

    不含 team.member/team.task 上下文事件（不会发给飞书，避免刷屏）。
    """
    et = item.get("event_type")
    if not isinstance(et, str):
        return False

    if et == "team.message":
        inner = item.get("event", {}) if isinstance(item.get("event"), dict) else {}
        msg_type = inner.get("type", "") or item.get("type", "")
        if msg_type == "team.message.broadcast":
            return True
        if msg_type == "team.message.p2p":
            to_m = item.get("to_member", "") or inner.get("to_member", "")
            from_m = item.get("from_member", "") or inner.get("from_member", "")
            return member_name in {to_m, from_m}
        return False

    # chat.* teammate outputs: 已在 _is_team_relevant 中按 role/mode 过滤。
    # 实时投递只发给该 member 的 private 席位，历史同样只对该 member 可见。
    if et in {"chat.final", "chat.tool_call", "chat.tool_result", "chat.file", "chat.tracer_agent"}:
        src_member = str(item.get("member_name", "") or "").strip()
        return bool(src_member) and src_member == member_name

    # 注意：team.member / team.task / team.event 不包含，
    # 这些是上下文事件，飞书端不需要看到，避免刷屏。
    return False


def read_member_history_records(session_id: str, member_name: str) -> list[dict[str, Any]]:
    """读取 team 历史记录，仅返回与指定 member 相关的记录。

    与实时 fan_out 投递语义一致：
    - 发给/由该 member 发出的 p2p 消息
    - @all 广播消息
    - 该 member 扮演的 teammate 的流式输出

    不含 team.member/team.task 上下文事件，也不含其他 member 的输出。
    无 member_name 时回退到 read_team_history_records（供 web 前端面板恢复用）。
    """
    if not member_name or not isinstance(member_name, str):
        return read_team_history_records(session_id)
    all_team_records = read_team_history_records(session_id)
    mn = member_name.strip()
    return [item for item in all_team_records if _is_member_relevant(item, mn)]


def read_session_history_records(session_id: str) -> list[dict[str, Any]]:
    """读取指定会话的历史记录，返回所有记录。

    用于 auto memory 功能提取对话消息。
    """
    fpath = get_read_history_path(session_id)
    all_records = _read_history_by_path(fpath)
    # write_text 非原子写入（先截断再写入），读取可能命中截断窗口，
    # 用递增间隔重试最多 5 次等待写入完成
    if not all_records and fpath.exists():
        for attempt in range(1, 6):
            time.sleep(0.2 * attempt)
            all_records = _read_history_by_path(fpath)
            if all_records:
                logger.info("read_session_history_records: recovered on retry %d", attempt)
                break
        if not all_records:
            logger.warning(
                "read_session_history_records: all retries exhausted, file_size=%d",
                fpath.stat().st_size,
            )

    return [item for item in all_records if isinstance(item, dict)]


def _write_item(session_id: str, item: dict[str, Any]) -> None:
    with _FILE_LOCK:
        if use_legacy_history_json():
            target_path = _ensure_legacy_json_bootstrap(session_id)
            records = _read_history(target_path)
            records.append(item)
            _write_records_to_path(target_path, records)
            return

        target_path = _ensure_jsonl_bootstrap(session_id)
        _append_record_jsonl(target_path, item)


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return

        def _worker() -> None:
            while True:
                sid, item = _WRITE_QUEUE.get()
                try:
                    _write_item(sid, item)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("history 异步写入失败: %s", exc)
                finally:
                    _WRITE_QUEUE.task_done()

        t = threading.Thread(target=_worker, name="session-history-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def append_history_record(
    *,
    session_id: str,
    request_id: str,
    channel_id: str,
    role: str,
    content: Any,
    timestamp: float,
    event_type: str | None = None,
    extra: dict[str, Any] | None = None,
    channel_metadata: dict[str, Any] | None = None,
    mode: str | None = None,
    wait: bool = False,
) -> bool:
    """Append a history record and report whether it was accepted or persisted.

    ``wait=False`` keeps the existing asynchronous contract and returns True
    once the record is queued. ``wait=True`` flushes prior writes and persists
    this record synchronously so callers can truthfully report a durable save.
    """
    sid = (session_id or "").strip()
    if not is_persistable_session_id(sid):
        # 空 id 或 "default"（上游 or "default" 兜底占位符）不应落盘：
        # 这类 id 不是真实会话，落盘只会凭空 mkdir 出空的 default 目录。
        logger.debug("skip history append: non-persistable session_id=%r", sid)
        return False
    if _is_ephemeral_heartbeat_session(sid):
        logger.debug("skip heartbeat session history: session_id=%s event_type=%s", sid, event_type)
        return False
    rid = str(request_id or "").strip()
    cid = str(channel_id or "").strip()
    role_norm = "assistant" if role == "assistant" else "user"
    content_text = content if isinstance(content, str) else str(content)
    if role_norm == "assistant" and not _has_persistable_assistant_payload(
        content_text=content_text,
        event_type=event_type,
        extra=extra,
    ):
        logger.debug(
            "skip empty assistant history: session_id=%s event_type=%s",
            sid,
            event_type or "",
        )
        return False

    item: dict[str, Any] = {
        "id": f"{rid}:{role_norm}",
        "role": role_norm,
        "request_id": rid,
        "channel_id": cid,
        "timestamp": float(timestamp),
        "content": content_text,
    }
    if role_norm == "assistant" and event_type:
        item["event_type"] = event_type
    if isinstance(extra, dict) and extra:
        serialized_extra, extra_changed = _serialize_value_with_flag(extra)
        if isinstance(serialized_extra, dict):
            item.update(serialized_extra)
            if extra_changed:
                logger.debug(
                    "history payload sanitized: session_id=%s request_id=%s event_type=%s extra_keys=%s",
                    sid,
                    rid,
                    event_type or "",
                    list(serialized_extra.keys()),
                )
    if mode:
        item["mode"] = str(mode)

    # 调试落盘开关（trace.json 的 session_history / JIUWENSWARM_TRACE_SESSION=1）：
    # 把这条即将落盘的记录镜像到 <日志目录>/session_flat/<sid>_history.jsonl，
    # 缺省关闭；异常一律吞掉，不影响落盘与业务。
    try:
        from jiuwenswarm.common.e2a.wire_trace import trace_history_record

        trace_history_record(item, sid)
    except Exception:  # noqa: BLE001
        pass

    if wait:
        _ensure_worker_started()
        _WRITE_QUEUE.join()
        _write_item(sid, item)
    else:
        _ensure_worker_started()
        try:
            _WRITE_QUEUE.put_nowait((sid, item))
        except queue.Full:
            # 队列满时退化为同步写，避免丢历史记录。
            _write_item(sid, item)

    # 更新会话元数据
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            set_session_delivery_context,
            update_session_metadata,
        )
        update_session_metadata(
            session_id=sid,
            channel_id=cid,
            increment_message_count=True,
            # 传入用户消息内容,用于自动生成标题
            user_content=content_text if role_norm == "user" else None,
            # 传入渠道元数据,首次写入时持久化
            channel_metadata=channel_metadata,
            mode=mode,
            # 用户消息时刷新 last_user_message_at(用消息时间戳,比请求到达时刻更精确;
            # 与 AgentServer 的 _sync_chat_request_metadata 互补,覆盖所有记录用户消息的路径)
            last_user_message_at=float(timestamp) if role_norm == "user" else None,
        )
        if role_norm == "user":
            set_session_delivery_context(
                session_id=sid,
                channel_id=cid,
                source_request_id=rid,
                route_metadata=channel_metadata,
            )
    except Exception as exc:
        logger.warning("更新会话元数据失败: %s", exc)
    return True


def flush_history_writes() -> None:
    """等待异步 history writer 队列刷盘，供错误恢复前强制同步读取历史."""
    _ensure_worker_started()
    _WRITE_QUEUE.join()


async def flush_history_writes_async(
    timeout: float = _HISTORY_FLUSH_ASYNC_TIMEOUT_SECONDS,
) -> bool:
    """在事件循环外等待历史落盘，并以有界等待避免错误恢复卡死服务。

    ``Queue.join()`` 没有超时参数。错误恢复路径若在事件循环线程直接调用，
    一旦 writer 卡住或队列计数异常，整个 AgentServer 都会停止响应。这里把
    同步等待移入线程，并限制错误恢复愿意等待的时间；超时后跳过本轮 rewind，
    不阻塞 ``chat.error`` 终态和其它会话。
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(flush_history_writes),
            timeout=max(float(timeout), 0.0),
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "history writer flush timed out after %.1fs; skip synchronous rewind",
            timeout,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("history writer flush failed; skip synchronous rewind: %s", exc)
        return False


def append_compact_history_records(
    *,
    session_id: str,
    request_id: str,
    channel_id: str,
    summary: str | None,
    timestamp: float,
    trigger: str = "auto",
    stats: dict[str, Any] | None = None,
    mode: str | None = None,
) -> None:
    """Persist a compact boundary and optional transcript-only summary."""
    clean_summary = (summary or "").strip()
    metadata = {
        "compact_metadata": {
            "trigger": trigger,
            **(_serialize_value(stats) if isinstance(stats, dict) else {}),
        },
    }

    append_history_record(
        session_id=session_id,
        request_id=request_id,
        channel_id=channel_id,
        role="assistant",
        event_type="context.compact_boundary",
        content="Conversation compacted",
        timestamp=timestamp,
        extra=metadata,
        mode=mode,
    )

    if not clean_summary:
        return

    append_history_record(
        session_id=session_id,
        request_id=request_id,
        channel_id=channel_id,
        role="assistant",
        event_type="context.compact_summary",
        content=clean_summary,
        timestamp=timestamp + 0.001,
        extra={
            **metadata,
            "is_compact_summary": True,
            "transcript_only": True,
        },
        mode=mode,
    )


def truncate_history_records(*, session_id: str, cut_index: int) -> dict[str, Any]:
    """截断会话历史到指定位置（线程安全）。

    先等待异步写入队列刷盘，再持锁截断当前激活的历史文件。
    返回截断结果 dict，包含 remaining / removed 计数。
    """
    sid = (session_id or "").strip()
    if not is_persistable_session_id(sid):
        logger.debug("skip history truncate: non-persistable session_id=%r", sid)
        return {"remaining_records": 0, "removed_records": 0}
    _WRITE_QUEUE.join()

    fpath = get_read_history_path(sid)
    with _FILE_LOCK:
        if not fpath.exists():
            return {"remaining_records": 0, "removed_records": 0}
        history = load_history_records(sid)
        if not isinstance(history, list):
            return {"remaining_records": 0, "removed_records": 0}
        total = len(history)
        if cut_index < 0:
            cut_index = 0
        if cut_index > total:
            cut_index = total
        truncated = history[:cut_index]
        _write_records_to_path(fpath, truncated)
        return {
            "remaining_records": len(truncated),
            "removed_records": total - len(truncated),
        }
