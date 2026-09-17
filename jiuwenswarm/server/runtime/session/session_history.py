from __future__ import annotations

import datetime
import logging
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir


logger = logging.getLogger(__name__)
_FILE_LOCK = threading.Lock()
_WRITE_QUEUE: queue.Queue[
    tuple[str, dict[str, Any], Future[None] | None]
] = queue.Queue(maxsize=20000)
_QUEUE_ENQUEUE_LOCK = threading.Lock()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()
_LEGACY_HISTORY_FILENAME = "history.json"
_JSONL_HISTORY_FILENAME = "history.jsonl"
_LEGACY_HISTORY_ENV = "JIUWENSWARM_USE_LEGACY_HISTORY_JSON"
_HEARTBEAT_OK = "HEARTBEAT_OK"
SESSION_REQUEST_COMPLETED_EVENT = "chat.request_completed"
_VALID_SESSION_ID = re.compile(
    r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?$"
)
# Gateway may inline @path as <file-content>...</file-content> before chat.send.
# History should keep the short @path form so jsonl rows stay one physical line
# and refresh UI does not load megabytes of file body.
_FILE_CONTENT_BLOCK_RE = re.compile(
    r"\n?<file-content\s+path=\"([^\"]*)\">.*?</file-content>\n?",
    re.DOTALL,
)


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
    长度不超过 80；首尾允许下划线，以兼容 ``__cron__`` 等内部会话 ID，
    但点和横线仍只允许出现在中间。不合法输入直接拒绝，根本不拼路径。

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
        data = json.loads(path.read_text(encoding="utf-8"))
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


def load_history_records(session_id: str) -> list[dict[str, Any]]:
    path = get_read_history_path(session_id)
    if path.suffix.lower() == ".jsonl":
        return _read_history_jsonl(path)
    return _read_history(path)


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
                sid, item, receipt = _WRITE_QUEUE.get()
                try:
                    _write_item(sid, item)
                except Exception as exc:  # noqa: BLE001
                    if receipt is not None:
                        receipt.set_exception(exc)
                    logger.warning("history 异步写入失败: %s", exc)
                else:
                    if receipt is not None:
                        receipt.set_result(None)
                finally:
                    _WRITE_QUEUE.task_done()

        t = threading.Thread(target=_worker, name="session-history-writer", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _enqueue_history_item(
    session_id: str,
    item: dict[str, Any],
    *,
    receipt: Future[None] | None = None,
) -> None:
    """Keep all history records on one FIFO path, including under pressure."""

    _ensure_worker_started()
    with _QUEUE_ENQUEUE_LOCK:
        try:
            _WRITE_QUEUE.put_nowait((session_id, item, receipt))
        except queue.Full:
            # A synchronous disk-write fallback can overtake queued records.
            # Block only under backpressure so request boundaries remain FIFO.
            _WRITE_QUEUE.put((session_id, item, receipt))


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
) -> None:
    """向指定 session 的当前激活历史文件异步追加一条记录."""
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

    _enqueue_history_item(sid, item)

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


def enqueue_history_request_completion(
    session_id: str,
    request_id: str,
    *,
    terminal_status: str = "success",
) -> Future[None] | None:
    """Persist a request boundary after its previously enqueued history."""

    sid = (session_id or "default").strip() or "default"
    rid = str(request_id or "").strip()
    if not rid or _is_ephemeral_heartbeat_session(sid):
        return None
    status = str(terminal_status or "success").strip().lower() or "success"
    receipt: Future[None] = Future()
    _enqueue_history_item(
        sid,
        {
            "id": f"{rid}:request_completed",
            "role": "assistant",
            "request_id": rid,
            "channel_id": "",
            "timestamp": time.time(),
            "content": "",
            "event_type": SESSION_REQUEST_COMPLETED_EVENT,
            "feedback_only": True,
            "status": status,
        },
        receipt=receipt,
    )
    return receipt


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
