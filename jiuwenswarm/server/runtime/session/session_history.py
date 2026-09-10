from __future__ import annotations

import atexit
import datetime
import logging
import json
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Tuple

from jiuwenswarm.common.utils import get_agent_sessions_dir


logger = logging.getLogger(__name__)
_FILE_LOCK = threading.Lock()
_WRITE_QUEUE: queue.Queue[tuple[str, dict[str, Any], str | None]] = queue.Queue(maxsize=20000)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_LEGACY_HISTORY_FILENAME = "history.json"
_JSONL_HISTORY_FILENAME = "history.jsonl"
_LEGACY_HISTORY_ENV = "JIUWENSWARM_USE_LEGACY_HISTORY_JSON"
_HEARTBEAT_OK = "HEARTBEAT_OK"
_VALID_SESSION_ID = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$"
)
# Gateway may inline @path as <file-content>...</file-content> before chat.send.
# History should keep the short @path form so jsonl rows stay one physical line
# and refresh UI does not load megabytes of file body.
_FILE_CONTENT_BLOCK_RE = re.compile(
    r"\n?<file-content\s+path=\"([^\"]*)\">.*?</file-content>\n?",
    re.DOTALL,
)


def _strip_tool_arguments_env(arguments: Any) -> Any:
    """Drop ``env`` from tool-call arguments so skill_envs never hit history disk."""
    if isinstance(arguments, dict):
        if "env" not in arguments:
            return arguments
        cleaned = dict(arguments)
        cleaned.pop("env", None)
        return cleaned
    if isinstance(arguments, str) and arguments.strip().startswith("{"):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return arguments
        if not isinstance(parsed, dict) or "env" not in parsed:
            return arguments
        parsed = dict(parsed)
        parsed.pop("env", None)
        return json.dumps(parsed, ensure_ascii=False)
    return arguments


def _strip_skill_env_from_history_item(item: dict[str, Any]) -> None:
    """Remove tool_args.env from a history record before persist."""
    tool_call = item.get("tool_call")
    if isinstance(tool_call, dict) and "arguments" in tool_call:
        tool_call["arguments"] = _strip_tool_arguments_env(tool_call.get("arguments"))

    tool_calls = item.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict) and "arguments" in call:
                call["arguments"] = _strip_tool_arguments_env(call.get("arguments"))

    if "arguments" in item:
        item["arguments"] = _strip_tool_arguments_env(item.get("arguments"))


def collapse_file_content_blocks(content: str) -> str:
    """Replace inlined ``<file-content>`` bodies with ``@path`` references.

    Used when persisting / serving user history so the agent-facing inline
    expansion is not stored as the user-visible transcript.
    """
    if not content or "<file-content" not in content:
        return content

    def _replacer(match: re.Match[str]) -> str:
        path = match.group(1) or ""
        if not path:
            return "\n"
        ref = f'@"{path}"' if any(ch.isspace() for ch in path) else f"@{path}"
        return f"\n{ref}\n"

    collapsed = _FILE_CONTENT_BLOCK_RE.sub(_replacer, content)
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def is_valid_session_id(session_id: str) -> bool:
    """Return whether a session id is safe to use as one path component."""

    return _VALID_SESSION_ID.fullmatch(session_id) is not None

# 缓冲层分两组：普通缓冲层（_session_buffer，合并后批量写）与暂留层（_session_pending，
# tool_calls.delta 等对应 chat.tool_call 命中后再决定丢弃/落盘）。
BUFFERABLE_EVENT_TYPES = {
    "chat.delta",
    "chat.reasoning",
    "chat.tool_update",
    "chat.tool_calls.delta",
}
NORMAL_BUFFER_EVENT_TYPES = BUFFERABLE_EVENT_TYPES - {"chat.tool_calls.delta"}
PENDING_EVENT_TYPE = "chat.tool_calls.delta"
BUFFER_FLUSH_INTERVAL = 5.0
BUFFER_MAX_SIZE = 100
PENDING_MAX_SECONDS = 2.0

_buffer_lock = threading.Lock()
_session_buffer: dict[str, dict[str, Any]] = {}
_session_buffer_type: dict[str, str] = {}
_session_buffer_request_id: dict[str, str] = {}
_session_buffer_root: dict[str, str | None] = {}
_session_tool_update_buffer: dict[str, "OrderedDict[str, dict[str, Any]]"] = {}
_session_tool_update_root: dict[str, str | None] = {}
_session_pending: dict[str, "_PendingState"] = {}
_pending_raw_counter: int = 0
_FLUSH_THREAD_STARTED = False
_FLUSH_THREAD_LOCK = threading.Lock()
_flush_stop_event = threading.Event()
_FLUSH_THREAD: threading.Thread | None = None
_SHUTDOWN_DONE: bool = False


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
    if content:
        return True
    if str(payload.get("reasoning_content") or "").strip():
        return True
    if et == "chat.file" and payload.get("files"):
        return True
    if et == "chat.tool_call" and (payload.get("tool_call") or payload.get("tool_calls")):
        return True
    if et == "chat.tool_result" and (payload.get("tool_result") or payload.get("tool_call_id")):
        return True
    if et in BUFFERABLE_EVENT_TYPES:
        # Mergeable stream events: persist structured extras even without content.
        if et in {"chat.delta", "chat.reasoning"}:
            return False
        return bool(payload)
    if payload.get("error") or payload.get("files"):
        return True
    if payload.get("tool_call") or payload.get("tool_calls"):
        return True
    # Token diagnostic rows have empty content; numbers live in extras.
    # chat.llm_usage is SkillTurbo's raw event; resume rewrites it to
    # chat.usage_metadata, but keep the original persistable as a fallback.
    if et in {"chat.usage_summary", "chat.usage_metadata", "chat.llm_usage"}:
        return bool(
            payload.get("usage")
            or payload.get("metadata")
            or payload.get("usage_metadata")
        )
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


@dataclass
class _PendingState:
    """tool_calls.delta 暂留状态。

    暂留期间整个 session 不落盘（其它事件进 pending_queue 候着），等对应的
    chat.tool_call 命中丢弃（条件 A）或超时落盘（条件 B）。
    """
    item: dict[str, Any]
    request_id: str
    pending_queue: "OrderedDict[Tuple[str, str], dict[str, Any]]" = field(default_factory=OrderedDict)
    start_time: float = 0.0
    sessions_root: str | None = None


def _is_empty_value(v: Any) -> bool:
    """None / 空串 / 空集合为"空"。注意数值 0、False 不算空（避免误判）。"""
    if v is None:
        return True
    if isinstance(v, str) and v == "":
        return True
    if isinstance(v, (list, dict, tuple, set)) and len(v) == 0:
        return True
    return False


def _merge_delta_events(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    merged["content"] = existing.get("content", "") + new.get("content", "")
    merged.setdefault("start_ts", existing.get("timestamp"))
    merged["timestamp"] = new.get("timestamp", merged.get("timestamp"))
    merged["delta_count"] = existing.get("delta_count", 1) + 1
    if _is_empty_value(merged.get("source_chunk_type")) and not _is_empty_value(new.get("source_chunk_type")):
        merged["source_chunk_type"] = new["source_chunk_type"]
    return merged


def _merge_reasoning_events(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    merged["content"] = existing.get("content", "") + new.get("content", "")
    merged.setdefault("start_ts", existing.get("timestamp"))
    merged["timestamp"] = new.get("timestamp", merged.get("timestamp"))
    merged["delta_count"] = existing.get("delta_count", 1) + 1
    return merged


def _merge_tool_update_events(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    if "status" in new:
        merged["status"] = new["status"]
    if "arguments" in new:
        merged["arguments"] = new["arguments"]
    for k in ("tool_name", "tool_call_id"):
        if _is_empty_value(merged.get(k)) and not _is_empty_value(new.get(k)):
            merged[k] = new[k]
    merged.setdefault("start_ts", existing.get("timestamp"))
    merged["timestamp"] = new.get("timestamp", merged.get("timestamp"))
    merged["delta_count"] = existing.get("delta_count", 1) + 1
    return merged


def _get_tool_call_key(call: dict) -> tuple[str, int]:
    call_id = call.get("id", "") or call.get("tool_call_id", "") or ""
    index = call.get("index", 0)
    return (call_id, index)


def _merge_tool_call(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    merged["arguments"] = existing.get("arguments", "") + new.get("arguments", "")
    for k in ("id", "tool_call_id", "name", "type"):
        if _is_empty_value(merged.get(k)) and not _is_empty_value(new.get(k)):
            merged[k] = new[k]
    if "index" not in merged and "index" in new:
        merged["index"] = new["index"]
    return merged


def _merge_tool_calls_delta_events(existing: dict, new: dict) -> dict:
    merged = dict(existing)
    calls_by_key: dict[tuple, dict] = {}
    by_index: dict[int, tuple] = {}
    for call in existing.get("tool_calls", []):
        key = _get_tool_call_key(call)
        calls_by_key[key] = call
        idx = call.get("index", 0)
        if idx not in by_index:
            by_index[idx] = key

    for call in new.get("tool_calls", []):
        key = _get_tool_call_key(call)
        call_id = call.get("id", "") or call.get("tool_call_id", "") or ""
        if key in calls_by_key:
            calls_by_key[key] = _merge_tool_call(calls_by_key[key], call)
        elif not call_id:
            idx = call.get("index", 0)
            matched_key = by_index.get(idx)
            if matched_key is not None and matched_key in calls_by_key:
                calls_by_key[matched_key] = _merge_tool_call(calls_by_key[matched_key], call)
            else:
                calls_by_key[key] = dict(call)
                by_index.setdefault(idx, key)
        else:
            calls_by_key[key] = dict(call)
            by_index.setdefault(call.get("index", 0), key)

    merged["tool_calls"] = list(calls_by_key.values())
    merged.setdefault("start_ts", existing.get("timestamp"))
    merged["timestamp"] = new.get("timestamp", merged.get("timestamp"))
    merged["delta_count"] = existing.get("delta_count", 1) + 1
    return merged


_MERGE = {
    "chat.delta": _merge_delta_events,
    "chat.reasoning": _merge_reasoning_events,
    "chat.tool_update": _merge_tool_update_events,
}


def _session_dir(
    session_id: str, *, create: bool = True, sessions_root: str | None = None
) -> Path:
    from jiuwenswarm.server.runtime.session.session_metadata import resolve_session_subdir

    selected_root = sessions_root if sessions_root is not None else get_agent_sessions_dir()
    session_dir = resolve_session_subdir(session_id, sessions_root=selected_root)
    if session_dir is None:
        raise ValueError("invalid session_id")
    if create:
        session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def resolve_session_dir(
    session_id: str, *, create: bool = False, sessions_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """安全解析 session 目录路径（防路径遍历）。

    采用白名单判据：``sanitize_session_id(session_id) == session_id`` 才认为合法，
    原样使用；否则直接拒绝，根本不拼路径。这样删除类破坏性操作不会因 sanitize 后的
    字符串（如 ``../config`` -> ``config``）误伤同名合法 session。

    再用 ``resolve()`` + ``relative_to`` 做纵深防御，兜底白名单逻辑被绕过的极端情况。

    Args:
        session_id: 待校验的 session id（调用方应先 ``.strip()``）。
        create: 是否创建目录（delete 流程传 False）。
        sessions_root: sessions 根目录。由调用方传入

    Returns:
        ``(resolved_path, None)`` —— 合法，返回解析后的绝对路径（确认在 sessions 目录内）。
        ``(None, error_reason)`` —— 非法，根本未触碰磁盘路径。
    """
    from jiuwenswarm.server.runtime.session.session_metadata import resolve_session_subdir

    selected_root = sessions_root if sessions_root is not None else get_agent_sessions_dir()
    resolved = resolve_session_subdir(session_id, sessions_root=selected_root)
    if resolved is None:
        return None, "invalid session_id"
    # 纵深防御必须在 mkdir 之前：先 resolve + relative_to 确认路径仍在 sessions
    # 目录内，通过后才允许创建。否则白名单一旦被绕过，mkdir(parents=True) 会
    # 先在 sessions 根目录之外越界创建目录，relative_to 才事后检测到——此时
    # 副作用已发生，越界空目录残留在磁盘上（虽不触发 rmtree，但仍是文件系统泄漏）。
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved, None


def _history_file(
    session_id: str, *, create: bool = True, sessions_root: str | None = None
) -> Path:
    return _session_dir(session_id, create=create, sessions_root=sessions_root) / _LEGACY_HISTORY_FILENAME


def _history_jsonl_file(
    session_id: str, *, create: bool = True, sessions_root: str | None = None
) -> Path:
    return _session_dir(session_id, create=create, sessions_root=sessions_root) / _JSONL_HISTORY_FILENAME


def use_legacy_history_json() -> bool:
    """Prefer ``history.json`` with JSONL content, matching OfficeClaw / test.

    Set ``JIUWENSWARM_USE_LEGACY_HISTORY_JSON=0`` to force ``history.jsonl`` writes.
    """
    raw = os.environ.get(_LEGACY_HISTORY_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_write_history_path(session_id: str, sessions_root: str | None = None) -> Path:
    """Return the preferred durable history write target for a session."""
    if use_legacy_history_json():
        return _history_file(session_id, sessions_root=sessions_root)
    return _history_jsonl_file(session_id, sessions_root=sessions_root)


def get_read_history_path(session_id: str, sessions_root: str | None = None) -> Path:
    """Return the preferred history source, falling back to legacy json."""
    if use_legacy_history_json():
        legacy_path = _history_file(session_id, create=False, sessions_root=sessions_root)
        if legacy_path.exists():
            return legacy_path
        jsonl_path = _history_jsonl_file(session_id, create=False, sessions_root=sessions_root)
        if jsonl_path.exists():
            return jsonl_path
        return legacy_path

    jsonl_path = _history_jsonl_file(session_id, create=False, sessions_root=sessions_root)
    if jsonl_path.exists():
        return jsonl_path
    legacy_path = _history_file(session_id, create=False, sessions_root=sessions_root)
    if legacy_path.exists():
        return legacy_path
    return jsonl_path


def history_exists(session_id: str, sessions_root: str | None = None) -> bool:
    return get_read_history_path(session_id, sessions_root=sessions_root).exists()


def get_history_mtime(session_id: str) -> float | None:
    path = get_read_history_path(session_id)
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _peek_first_non_ws_char(path: Path) -> str | None:
    """Return the first non-whitespace character, or None if empty/unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            while True:
                ch = fh.read(1)
                if not ch:
                    return None
                if not ch.isspace():
                    return ch
    except OSError:
        return None
    return None


def _history_file_is_json_array(path: Path) -> bool:
    """True when the file is a legacy pretty/minified JSON array (not JSONL)."""
    return _peek_first_non_ws_char(path) == "["


def _read_history(path: Path) -> list[dict[str, Any]]:
    """Read history.json / history.jsonl. Accepts JSONL or a legacy JSON array."""
    if not path.exists():
        return []
    if _history_file_is_json_array(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 history.json 失败，已忽略并重建: %s", exc)
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []
    return _read_history_jsonl(path)


def _read_history_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        # JSONL records are delimited by "\n" only. Do NOT use str.splitlines():
        # inlined file bodies may contain Unicode line separators (U+2028 etc.)
        # that splitlines() treats as breaks, corrupting a single JSON object
        # into fragments and dropping the user turn on refresh.
        text = path.read_text(encoding="utf-8")
        for lineno, raw_line in enumerate(text.split("\n"), start=1):
            line = raw_line.rstrip("\r").strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 history.jsonl 第 %d 行失败，已跳过: %s", lineno, exc)
                continue
            if isinstance(item, dict):
                content = item.get("content")
                if (
                    item.get("role") in {"user", "human"}
                    and isinstance(content, str)
                    and "<file-content" in content
                ):
                    item = dict(item)
                    item["content"] = collapse_file_content_blocks(content)
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


def load_history_records(session_id: str, sessions_root: str | None = None) -> list[dict[str, Any]]:
    return _read_history(get_read_history_path(session_id, sessions_root=sessions_root))


def _write_records_to_path(path: Path, records: list[dict[str, Any]]) -> None:
    """Rewrite history as JSONL (one object per line), including ``history.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if payload:
        payload += "\n"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)


def _append_record_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


def _rewrite_json_array_as_jsonl(path: Path) -> None:
    """Convert a legacy JSON-array history.json to JSONL before appending."""
    if not path.exists() or not _history_file_is_json_array(path):
        return
    records = _read_history(path)
    _write_records_to_path(path, records)


def _ensure_jsonl_bootstrap(session_id: str, sessions_root: str | None = None) -> Path:
    jsonl_path = _history_jsonl_file(session_id, sessions_root=sessions_root)
    if jsonl_path.exists():
        return jsonl_path

    legacy_path = _history_file(session_id, sessions_root=sessions_root)
    if legacy_path.exists():
        legacy_records = _read_history(legacy_path)
        _write_records_to_path(jsonl_path, legacy_records)
    else:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return jsonl_path


def _ensure_legacy_json_bootstrap(session_id: str, sessions_root: str | None = None) -> Path:
    legacy_path = _history_file(session_id, sessions_root=sessions_root)
    if legacy_path.exists():
        return legacy_path

    jsonl_path = _history_jsonl_file(session_id, sessions_root=sessions_root)
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
    """Rewrite a session's history in JSONL, defaulting new sessions to history.json."""
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
    "chat.final", "chat.tool_result", "chat.file",
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
        if et in ("chat.final", "chat.tool_result"):
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
    """Read a history file; content (JSONL or JSON array) decides the parser."""
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


def _batch_write_items(session_id: str, items: list[dict], sessions_root: str | None) -> None:
    """批量写入 history.json（一次 open 写多行）。_FILE_LOCK 串行化磁盘写。"""
    if not items:
        return
    with _FILE_LOCK:
        if use_legacy_history_json():
            target_path = _ensure_legacy_json_bootstrap(session_id, sessions_root=sessions_root)
            _rewrite_json_array_as_jsonl(target_path)
        else:
            target_path = _ensure_jsonl_bootstrap(session_id, sessions_root=sessions_root)
        with target_path.open("a", encoding="utf-8", newline="\n") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False))
                fh.write("\n")


def _write_item(session_id: str, item: dict[str, Any], sessions_root: str | None = None) -> None:
    _batch_write_items(session_id, [item], sessions_root)


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return

        def _worker() -> None:
            while True:
                sid, item, sessions_root = _WRITE_QUEUE.get()
                try:
                    _write_item(sid, item, sessions_root)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("history 异步写入失败: %s", exc)
                finally:
                    _WRITE_QUEUE.task_done()

        t = threading.Thread(target=_worker, name="session-history-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _flush_buffer_unlocked(session_id: str) -> tuple[list[dict], str | None]:
    """落盘并清空普通缓冲层（调用方已持锁）。返回 (items, recorded_root)，
    调用方锁外执行 IO。tool_update 走 per-call 缓冲可返回多条。"""
    per_call = _session_tool_update_buffer.pop(session_id, None)
    if per_call is not None:
        _session_buffer_type.pop(session_id, None)
        _session_buffer_request_id.pop(session_id, None)
        _session_buffer_root.pop(session_id, None)
        recorded_root = _session_tool_update_root.pop(session_id, None)
        return list(per_call.values()), recorded_root
    item = _session_buffer.pop(session_id, None)
    if item is None:
        return [], None
    _session_buffer_type.pop(session_id, None)
    _session_buffer_request_id.pop(session_id, None)
    recorded_root = _session_buffer_root.pop(session_id, None)
    return [item], recorded_root


def _flush_buffer(session_id: str, sessions_root: str | None) -> None:
    """落盘并清空普通缓冲层。sessions_root 为 None 时回退到缓冲时记录的 root。"""
    with _buffer_lock:
        items, recorded_root = _flush_buffer_unlocked(session_id)
    if items:
        root = sessions_root if sessions_root is not None else recorded_root
        _batch_write_items(session_id, items, root)


def _flush_on_request_switch(session_id: str, request_id: str, sessions_root: str | None) -> None:
    """新 request_id 到达：落盘旧请求的缓冲 + 激活的暂留层（按条件 B）。"""
    with _buffer_lock:
        current_rid = _session_buffer_request_id.get(session_id, "")
        pending = _session_pending.get(session_id)
        pending_rid = pending.request_id if pending is not None else ""
        old_rid = current_rid or pending_rid
        switched = bool(request_id and old_rid and request_id != old_rid)
        if switched:
            buf_items, buf_root = _flush_buffer_unlocked(session_id)
            _session_pending.pop(session_id, None)
        else:
            buf_items = []
            pending = None
    if buf_items:
        _batch_write_items(session_id, buf_items, buf_root)
    if pending is not None:
        pending_items = [pending.item] + list(pending.pending_queue.values())
        _batch_write_items(session_id, pending_items, pending.sessions_root)


def _flush_on_type_switch_unlocked(session_id: str, event_type: str, request_id: str) -> tuple[list[dict], str | None]:
    """类型/请求切换判定（调用方已持锁）。返回 (待落盘 items, recorded_root)。"""
    current_type = _session_buffer_type.get(session_id)
    current_rid = _session_buffer_request_id.get(session_id, "")
    type_switch = current_type is not None and current_type != event_type
    request_switch = request_id and current_rid and request_id != current_rid
    if type_switch or request_switch:
        return _flush_buffer_unlocked(session_id)
    return [], None


def _extract_tool_call_id(item: dict) -> str:
    """从 chat.tool_call 提取 id（嵌套 item["tool_call"]["tool_call_id"]）。"""
    tc = item.get("tool_call") or {}
    if not isinstance(tc, dict):
        return ""
    return (tc.get("tool_call_id") or tc.get("id")
            or item.get("tool_call_id") or item.get("id") or "")


def _extract_pending_call_ids(pending_item: dict) -> set[str]:
    ids: set[str] = set()
    for call in pending_item.get("tool_calls", []):
        if not isinstance(call, dict):
            continue
        cid = call.get("id") or call.get("tool_call_id") or ""
        if cid:
            ids.add(cid)
    return ids


def _get_buffer_key(item: dict) -> Tuple[str, str]:
    return (item.get("request_id", ""), item.get("event_type", ""))


def _buffer_into(pending_queue: "OrderedDict[Tuple[str, str], dict]", item: dict, event_type: str) -> None:
    """合并事件进 pending_queue（同类型合并，非缓冲事件按到达顺序原样进）。"""
    global _pending_raw_counter
    if event_type in NORMAL_BUFFER_EVENT_TYPES:
        key = _get_buffer_key(item)
        pending_queue[key] = _MERGE[event_type](pending_queue[key], item) if key in pending_queue else dict(item)
    else:
        _pending_raw_counter += 1
        pending_queue[(item.get("request_id", ""), f"__raw_{_pending_raw_counter}")] = dict(item)


def _route_event(sid: str, item: dict, event_type: str, sessions_root_s: str | None) -> None:
    """事件分发：暂留层 / 普通缓冲层 / 非缓冲事件。"""
    rid = item.get("request_id", "")

    flush_items: list[dict] | None = None
    flush_root: str | None = sessions_root_s
    with _buffer_lock:
        pending = _session_pending.get(sid)
        if pending is not None:
            if event_type == PENDING_EVENT_TYPE:
                pending.item = _merge_tool_calls_delta_events(pending.item, item)
                return
            if event_type == "chat.tool_call" and _extract_tool_call_id(item) in \
               _extract_pending_call_ids(pending.item):
                _session_pending.pop(sid, None)
                flush_items = list(pending.pending_queue.values()) + [item]
                flush_root = pending.sessions_root
            else:
                _buffer_into(pending.pending_queue, item, event_type)
                return
    if flush_items is not None:
        _batch_write_items(sid, flush_items, flush_root)
        return

    if event_type == PENDING_EVENT_TYPE:
        with _buffer_lock:
            flush_items, _ = _flush_on_type_switch_unlocked(sid, event_type, rid)
            _session_pending[sid] = _PendingState(
                item=dict(item),
                request_id=rid,
                pending_queue=OrderedDict(),
                start_time=time.monotonic(),
                sessions_root=sessions_root_s,
            )
        if flush_items:
            _batch_write_items(sid, flush_items, sessions_root_s)
        return

    if event_type in NORMAL_BUFFER_EVENT_TYPES:
        with _buffer_lock:
            switch_items, switch_root = _flush_on_type_switch_unlocked(sid, event_type, rid)
            if event_type == "chat.tool_update":
                call_id = (item.get("tool_call_id") or item.get("id") or "")
                per_call = _session_tool_update_buffer.setdefault(sid, OrderedDict())
                existing_call = per_call.get(call_id)
                per_call[call_id] = _merge_tool_update_events(existing_call, item) if existing_call else dict(item)
                _session_buffer_type[sid] = event_type
                _session_buffer_request_id[sid] = rid
                _session_buffer_root[sid] = sessions_root_s
                _session_tool_update_root[sid] = sessions_root_s
                cap_items, cap_root = ([], None)
                if len(per_call) >= BUFFER_MAX_SIZE:
                    cap_items, cap_root = _flush_buffer_unlocked(sid)
            else:
                existing = _session_buffer.get(sid)
                if existing is not None and _session_buffer_type.get(sid) == event_type:
                    merged = _MERGE[event_type](existing, item)
                else:
                    merged = dict(item)
                _session_buffer[sid] = merged
                _session_buffer_type[sid] = event_type
                _session_buffer_request_id[sid] = rid
                _session_buffer_root[sid] = sessions_root_s
                cap_items, cap_root = ([], None)
                if merged.get("delta_count", 1) >= BUFFER_MAX_SIZE:
                    cap_items, cap_root = _flush_buffer_unlocked(sid)
        if switch_items and cap_items and switch_root == cap_root:
            _batch_write_items(sid, switch_items + cap_items, switch_root if switch_root is not None else cap_root)
        else:
            if switch_items:
                _batch_write_items(sid, switch_items, switch_root)
            if cap_items:
                _batch_write_items(sid, cap_items, cap_root)
        return

    with _buffer_lock:
        switch_items, switch_root = _flush_on_type_switch_unlocked(sid, event_type, rid)
    if not switch_items:
        _batch_write_items(sid, [item], sessions_root_s)
    elif switch_root is not None and sessions_root_s is not None and switch_root == sessions_root_s:
        _batch_write_items(sid, switch_items + [item], sessions_root_s)
    else:
        _batch_write_items(sid, switch_items, switch_root if switch_root is not None else sessions_root_s)
        _batch_write_items(sid, [item], sessions_root_s)


def _ensure_flush_thread_started() -> None:
    """启动定时刷新线程：每 BUFFER_FLUSH_INTERVAL 秒刷新普通缓冲层 + 检查暂留超时。"""
    global _FLUSH_THREAD_STARTED, _FLUSH_THREAD
    if _FLUSH_THREAD_STARTED and _FLUSH_THREAD is not None and _FLUSH_THREAD.is_alive():
        return
    with _FLUSH_THREAD_LOCK:
        if _FLUSH_THREAD_STARTED and _FLUSH_THREAD is not None and _FLUSH_THREAD.is_alive():
            return

        def _flush_worker() -> None:
            while not _flush_stop_event.wait(BUFFER_FLUSH_INTERVAL):
                try:
                    _periodic_flush()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("history 定时刷新失败: %s", exc)

        t = threading.Thread(target=_flush_worker, name="session-history-flusher", daemon=True)
        t.start()
        _FLUSH_THREAD = t
        _FLUSH_THREAD_STARTED = True


def _force_flush_all_pending() -> None:
    """强制落盘所有剩余暂留（忽略超时），shutdown 收尾兜底。"""
    with _buffer_lock:
        remaining = list(_session_pending.items())
        _session_pending.clear()
    for sid, pending in remaining:
        try:
            _batch_write_items(sid, [pending.item] + list(pending.pending_queue.values()), pending.sessions_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("history shutdown 暂留落盘失败 sid=%s: %s", sid, exc)


def shutdown() -> None:
    """进程退出前收尾：停定时线程 + flush 落盘（含强制落剩余暂留）+ 排空异步写队列。"""
    global _FLUSH_THREAD_STARTED, _FLUSH_THREAD, _SHUTDOWN_DONE
    with _buffer_lock:
        if _SHUTDOWN_DONE:
            return
        _SHUTDOWN_DONE = True
    _flush_stop_event.set()
    if _FLUSH_THREAD is not None and _FLUSH_THREAD is not threading.current_thread():
        _FLUSH_THREAD.join(timeout=2.0)
    try:
        _periodic_flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("history shutdown flush 失败: %s", exc)
    try:
        _force_flush_all_pending()
    except Exception as exc:  # noqa: BLE001
        logger.warning("history shutdown 强制暂留落盘失败: %s", exc)
    deadline = time.monotonic() + 5.0
    try:
        while _WRITE_QUEUE.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
    except Exception:  # noqa: BLE001
        pass
    try:
        _periodic_flush()
        _force_flush_all_pending()
    except Exception as exc:  # noqa: BLE001
        logger.warning("history shutdown 末次复扫失败: %s", exc)
    _flush_stop_event.clear()
    _FLUSH_THREAD_STARTED = False
    _FLUSH_THREAD = None


atexit.register(shutdown)


def _periodic_flush() -> None:
    """定时刷新：普通缓冲层落盘 + 暂留层超时检查（条件 B）。"""
    with _buffer_lock:
        buffer_sids = list(set(_session_buffer.keys()) | set(_session_tool_update_buffer.keys()))
        pending_sids = [
            (sid, p) for sid, p in _session_pending.items()
            if time.monotonic() - p.start_time >= PENDING_MAX_SECONDS
        ]
        for sid, _ in pending_sids:
            _session_pending.pop(sid, None)

    for sid in buffer_sids:
        _flush_buffer(sid, None)

    for sid, pending in pending_sids:
        try:
            _batch_write_items(sid, [pending.item] + list(pending.pending_queue.values()), pending.sessions_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("history 暂留超时落盘失败 sid=%s: %s", sid, exc)


def flush_session_history(session_id: str, sessions_root: str | None = None) -> None:
    """Flush in-memory merge buffers for one session, then drain the async writer."""
    sid = (session_id or "default").strip() or "default"
    _flush_buffer(sid, sessions_root)
    with _buffer_lock:
        pending = _session_pending.pop(sid, None)
    if pending is not None:
        root = sessions_root if sessions_root is not None else pending.sessions_root
        _batch_write_items(sid, [pending.item] + list(pending.pending_queue.values()), root)
    _WRITE_QUEUE.join()


def enrich_history_messages_session_id(
    messages: Iterable[dict[str, Any]],
    resolved_session_id: str,
) -> list[dict[str, Any]]:
    """为缺少 session_id 的历史记录做浅拷贝补全（兼容旧数据）。"""
    sid = resolved_session_id.strip()
    out: list[dict[str, Any]] = []
    for m in messages:
        if "session_id" not in m:
            out.append({**m, "session_id": sid})
        else:
            out.append(m)
    return out


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
    sessions_root: str | Path | None = None,
    task_id: str | None = None,
) -> None:
    """向指定 session 的 history.json 追加一条 JSONL 记录（可合并事件先缓冲）。"""
    sid = (session_id or "default").strip() or "default"
    if _is_ephemeral_heartbeat_session(sid):
        logger.debug("skip heartbeat session history: session_id=%s event_type=%s", sid, event_type)
        return
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
        return

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
    if task_id:
        item["task_id"] = task_id
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
    item["session_id"] = sid
    # skill_envs injected into bash tool_args.env must not be persisted.
    _strip_skill_env_from_history_item(item)

    sessions_root_s = str(sessions_root) if sessions_root else None
    et = event_type if (role_norm == "assistant" and event_type) else None

    _ensure_flush_thread_started()
    try:
        _flush_on_request_switch(sid, rid, sessions_root_s)
        _route_event(sid, item, et, sessions_root_s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history 缓冲写入失败，降级直写: %s", exc)
        _ensure_worker_started()
        try:
            _WRITE_QUEUE.put_nowait((sid, item, sessions_root_s))
        except queue.Full:
            _write_item(sid, item, sessions_root_s)

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
            sessions_root=sessions_root_s,
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
    sid = (session_id or "default").strip() or "default"
    flush_session_history(sid)

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
