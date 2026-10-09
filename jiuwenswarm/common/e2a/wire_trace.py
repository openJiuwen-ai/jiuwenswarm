# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一调试落盘：E2A / A2A / session 历史记录（测试与排障用）。

三类报文/记录按通道分目录落盘，开关集中在一个开关文件里；缺省全部关闭。

开关文件（数据目录，优先 ``trace.json``；旧名 ``e2a_trace.json`` 仍兼容）::

    <JIUWENSWARM_DATA_DIR>/trace.json
    {
      "enabled": true,            # 总开关；false = 全部关闭
      "dir": "D:\\\\trace_root",    # 可选：覆盖落盘根目录（默认见下）
      "e2a": true,                # E2A 原始报文（客户端 ↔ AgentServer）
      "a2a": true,                # A2A 原始报文（云/中转 ↔ 渠道）
      "session_history": true     # history.jsonl 每条落盘记录
    }

    - 只要出现任一通道键（``e2a`` / ``a2a`` / ``session_history``）即按“显式
      通道”解析：没列出的通道视为关闭。
    - 一个通道键都没有时按旧语义解析：``enabled=true`` → E2A 开；
      ``history_records=true`` → session 开（兼容昨天那份开关文件）。

环境变量（源码 / 自行启动后端时，优先级高于开关文件）::

    JIUWENSWARM_TRACE=1                            # 总开关（未单列通道时三通道全开）
    JIUWENSWARM_TRACE_E2A=1 / _A2A=1 / _SESSION=1  # 单通道覆盖（1 开 / 0 关）
    JIUWENSWARM_TRACE_DIR=<root>                   # 覆盖落盘根目录
    兼容旧名：JIUWENSWARM_E2A_TRACE / _DIR、JIUWENSWARM_HISTORY_TRACE

落盘位置（默认根 = 后端"当日日志目录" ``get_dated_logs_dir()``，即 full.log 同级::

    <logs>/<日期>/<…>/e2a/<标题>__<session_id>/<method>__<request_id>.jsonl
    <logs>/<日期>/<…>/a2a/<session_id>.jsonl
    <logs>/<日期>/<…>/session_flat/<session_id>_history.jsonl

    前两类每行统一 ``{"role": "in"/"out", "ts": …, "data": …}``；
    session_flat 每行就是落盘的那条历史记录本身（与 history.jsonl 内容一致）。

改开关文件后 3 秒内生效，无需重启；写入异常一律降级为 warning，绝不影响业务。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
# request_id -> session_id（E2A：发送响应时按 request_id 找回会话）
_REQUEST_SESSION: dict[str, str] = {}
# request_id -> method（E2A：文件名 <method>__<request_id>.jsonl 用）
_REQUEST_METHOD: dict[str, str] = {}
# request_id -> 待认领的会话标题（E2A：session.create/rename 请求先记，响应/后续回填）
_PENDING_TITLE: dict[str, str] = {}
# session_id -> 会话标题（E2A：目录命名 <标题>__<session_id>）
_SESSION_TITLE: dict[str, str] = {}

_TRUE_VALUES = {"1", "true", "on", "yes", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "off", "no", "disable", "disabled"}
_PRIVATE_MEMORY_METHODS = {
    "memory.profile.settings.get", "memory.profile.settings.set",
    "memory.profile.get", "memory.profile.modify",
}

# 开关文件：新名优先，旧名兼容
_MARKER_FILENAME = "trace.json"
_LEGACY_MARKER_FILENAME = "e2a_trace.json"
# 三个通道：环境变量后缀 / 子目录名 / 开关文件里的键（按优先级）
_CHANNEL_ENV_SUFFIX = {"e2a": "E2A", "a2a": "A2A", "session": "SESSION"}
_CHANNEL_SUBDIR = {"e2a": "e2a", "a2a": "a2a", "session": "session_flat"}
_CHANNEL_MARKER_KEYS = {
    "e2a": ("e2a",),
    "a2a": ("a2a",),
    "session": ("session_history", "history_records"),
}
# "显式通道模式"判据只认新键：只要出现 e2a/a2a/session_history 之一，就按显式通道解析。
# 旧键（history_records）与 enabled 一起构成旧语义，不能被当成"显式通道"。
_EXPLICIT_CHANNEL_KEYS = ("e2a", "a2a", "session_history")
_ALL_CHANNELS = ("e2a", "a2a", "session")

# 开关文件缓存（避免每条报文都读磁盘；改动后最多 3 秒生效）
_MARKER_TTL_SECONDS = 3.0
_MARKER_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}, "legacy": False}


def _workspace_dir() -> Path:
    data_dir = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir)
    try:
        from jiuwenswarm.common.utils import get_user_workspace_dir
        return Path(get_user_workspace_dir())
    except Exception:  # noqa: BLE001
        return Path(".")


def _marker_path() -> Path:
    """当前生效的开关文件路径（新名存在用新名，否则旧名，都没有时返回新名）。"""
    workspace = _workspace_dir()
    primary = workspace / _MARKER_FILENAME
    if primary.exists():
        return primary
    legacy = workspace / _LEGACY_MARKER_FILENAME
    if legacy.exists():
        return legacy
    return primary


def _read_marker() -> tuple[dict[str, Any], bool]:
    """读取开关文件，返回 (配置, 是否来自旧文件名)。"""
    now = time.time()
    if now - _MARKER_CACHE["ts"] < _MARKER_TTL_SECONDS:
        return _MARKER_CACHE["data"], bool(_MARKER_CACHE["legacy"])

    data: dict[str, Any] = {}
    legacy = False
    try:
        workspace = _workspace_dir()
        primary = workspace / _MARKER_FILENAME
        path = primary if primary.exists() else workspace / _LEGACY_MARKER_FILENAME
        if path.exists():
            legacy = path.name == _LEGACY_MARKER_FILENAME
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        data, legacy = {}, False
    _MARKER_CACHE.update(ts=now, data=data, legacy=legacy)
    return data, legacy


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return None


def channel_enabled(channel: str) -> bool:
    """解析某个通道（``e2a`` / ``a2a`` / ``session``）当前是否开启。

    优先级：单通道环境变量 > 旧环境变量别名 > 总开关环境变量 > 开关文件。
    所有解析失败都按"关闭"处理——绝不因为开关文件异常而影响业务。
    """
    if channel not in _CHANNEL_SUBDIR:
        return False
    try:
        suffix = _CHANNEL_ENV_SUFFIX.get(channel, channel.upper())
        specific = _env_bool(f"JIUWENSWARM_TRACE_{suffix}")
        if specific is not None:
            return specific
        if channel == "e2a":
            legacy_env = _env_bool("JIUWENSWARM_E2A_TRACE")
            if legacy_env is not None:
                return legacy_env
        if channel == "session":
            legacy_env = _env_bool("JIUWENSWARM_HISTORY_TRACE")
            if legacy_env is not None:
                return legacy_env
        master = _env_bool("JIUWENSWARM_TRACE")
        if master is not None:
            return master

        marker, _legacy_file = _read_marker()
        if not marker:
            return False
        if marker.get("enabled") is False:
            return False
        keys = _CHANNEL_MARKER_KEYS.get(channel, (channel,))
        has_channel_key = any(key in marker for key in _EXPLICIT_CHANNEL_KEYS)
        for key in keys:
            if key in marker:
                return bool(marker.get(key))
        if has_channel_key:
            # 显式通道模式：未列出的通道 = 关闭
            return False
        # 旧语义（开关文件里没有任何通道键）：enabled=true 仅表示 E2A 开
        return channel == "e2a" and bool(marker.get("enabled"))
    except Exception:  # noqa: BLE001
        return False


def _enabled() -> bool:
    """E2A 通道是否开启（保留旧函数名，内部按通道解析）。"""
    return channel_enabled("e2a")


def history_records_enabled() -> bool:
    """session 历史记录 dump 是否开启（保留旧函数名）。"""
    return channel_enabled("session")


def _default_root() -> Path:
    """默认落盘根目录 = 后端当日日志目录（与 full.log 同级）。

    桌面端注入了外层日期布局（``JIUWENSWARM_LOG_DATE_ROOT``）时，
    ``get_dated_logs_dir()`` 返回 ``<logs>/<日期>/<…>/``，即 full.log 所在目录；
    独立运行时退化为 ``<logs_root>/<日期>``。
    """
    try:
        from jiuwenswarm.common.utils import get_dated_logs_dir

        return Path(get_dated_logs_dir())
    except Exception:  # noqa: BLE001
        return _workspace_dir() / "trace"


def _trace_root() -> Path:
    explicit = (
        os.environ.get("JIUWENSWARM_TRACE_DIR", "").strip()
        or os.environ.get("JIUWENSWARM_E2A_TRACE_DIR", "").strip()
    )
    if explicit:
        return Path(explicit)
    marker, _legacy = _read_marker()
    marker_dir = marker.get("dir")
    if isinstance(marker_dir, str) and marker_dir.strip():
        return Path(marker_dir.strip())
    return _default_root()


def _channel_root(channel: str) -> Path:
    return _trace_root() / _CHANNEL_SUBDIR.get(channel, channel)


def _sanitize(value: Any) -> str:
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # noqa: BLE001
            return "unknown"
    value = value.strip()
    if not value:
        return "unknown"
    for ch in '\\/:*?"<>|':
        value = value.replace(ch, "_")
    return value[:160]


def _session_label(session_id: Any) -> str:
    """会话目录名：有标题用 <标题>__<session_id>，否则 session_id。"""
    sid = _sanitize(session_id)
    title = _SESSION_TITLE.get(str(session_id).strip()) if isinstance(session_id, str) else None
    if title:
        return f"{_sanitize(title)}__{sid}"
    return sid


def _note_request_meta(payload: dict[str, Any]) -> None:
    """从请求里记录 method / 会话标题（session.create / session.rename）。"""
    method = payload.get("method")
    rid = payload.get("request_id")
    if isinstance(rid, str) and rid.strip() and isinstance(method, str) and method.strip():
        _REQUEST_METHOD[rid] = method
    params = payload.get("params")
    if not isinstance(params, dict):
        return
    title = params.get("title")
    if not (isinstance(title, str) and title.strip()):
        return
    if method == "session.create" and isinstance(rid, str) and rid.strip():
        _PENDING_TITLE[rid] = title.strip()
    elif method == "session.rename":
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid.strip():
            _SESSION_TITLE[sid] = title.strip()


def _note_session_from_result(wire: dict[str, Any]) -> None:
    """session.create 响应带回 session_id，把待认领标题回填。"""
    body = wire.get("body")
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return
    sid = result.get("session_id") or result.get("sessionId")
    if not (isinstance(sid, str) and sid.strip()):
        return
    rid = wire.get("request_id")
    title = _PENDING_TITLE.pop(rid, None) if isinstance(rid, str) else None
    if title:
        _SESSION_TITLE[sid] = title


def _session_from_wire(wire: dict[str, Any]) -> str | None:
    body = wire.get("body")
    if isinstance(body, dict):
        delta = body.get("delta")
        if isinstance(delta, dict):
            sid = delta.get("session_id")
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
        result = body.get("result")
        if isinstance(result, dict):
            sid = result.get("session_id") or result.get("sessionId")
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
    top = wire.get("session_id")
    if isinstance(top, str) and top.strip():
        return top.strip()
    return None


def _folder_for(session_id: Any) -> str:
    raw = session_id if isinstance(session_id, str) else None
    if not raw or not raw.strip() or raw == "unknown":
        return "__sessionless__"
    return _session_label(raw)


def _write_line(dest: Path, payload: Any) -> None:
    """追加一行 JSON（父目录按需创建）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with open(dest, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _append(role: str, session_id: Any, request_id: Any, data: Any) -> None:
    """E2A 原始报文落盘（每请求一文件）。"""
    if not _enabled():
        return
    rid = _sanitize(request_id)
    method = _sanitize(_REQUEST_METHOD.get(rid, ""))
    if isinstance(request_id, str) and _REQUEST_METHOD.get(request_id) in _PRIVATE_MEMORY_METHODS and isinstance(data, dict):
        # Management requests/results contain private profile text. Keep only
        # envelope diagnostics, even when raw E2A tracing is explicitly enabled.
        data = {key: value for key, value in data.items() if key in {
            "protocol_version", "method", "request_id", "response_id", "channel",
            "session_id", "timestamp", "is_final", "status", "response_kind",
        }}
        data["redacted"] = True
    try:
        with _LOCK:
            dest = _channel_root("e2a") / _folder_for(session_id) / f"{method}__{rid}.jsonl"
            _write_line(dest, {"role": role, "ts": time.time(), "data": data})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[e2a_trace] 写原始报文失败: %s", exc)


def trace_inbound(payload: Any) -> None:
    """记录一条客户端请求（原始 JSON）。任何异常都吞掉，不影响业务。"""
    try:
        if (isinstance(payload, dict) and isinstance(payload.get("method"), str)
                and payload["method"] in _PRIVATE_MEMORY_METHODS):
            # Remember the method even if tracing is enabled between request/response.
            _note_request_meta(payload)
        if not _enabled() or not isinstance(payload, dict):
            return
        _note_request_meta(payload)
        session_id = payload.get("session_id")
        request_id = payload.get("request_id")
        if isinstance(request_id, str) and request_id.strip() and isinstance(session_id, str) and session_id.strip():
            _REQUEST_SESSION[request_id] = session_id
        _append("in", session_id, request_id, payload)
    except Exception:  # noqa: BLE001
        return


def trace_outbound(wire: Any) -> None:
    """记录一帧服务端响应（原始 JSON）。任何异常都吞掉，不影响业务。"""
    try:
        if not _enabled() or not isinstance(wire, dict):
            if (isinstance(wire, dict) and isinstance(wire.get("request_id"), str)
                    and _REQUEST_METHOD.get(wire["request_id"]) in _PRIVATE_MEMORY_METHODS):
                _REQUEST_METHOD.pop(wire.get("request_id"), None)
            return
        _note_session_from_result(wire)
        request_id = wire.get("request_id")
        session_id = (
            _REQUEST_SESSION.get(request_id) if isinstance(request_id, str) else None
        ) or _session_from_wire(wire)
        if not session_id and wire.get("type") == "event":
            session_id = "__server__"
        _append("out", session_id, request_id, wire)
        if isinstance(request_id, str) and _REQUEST_METHOD.get(request_id) in _PRIVATE_MEMORY_METHODS:
            _REQUEST_METHOD.pop(request_id, None)
    except Exception:  # noqa: BLE001
        return


def _a2a_dict(payload: Any) -> dict[str, Any] | None:
    """把 A2A 报文规整成 dict（str/bytes 先按 JSON 解析）；解析不了返回 None。"""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            return None
    return payload if isinstance(payload, dict) else None


def _a2a_nested_dict(payload: Any) -> dict[str, Any] | None:
    """取 A2A 包装形态里 msgDetail 内嵌的 JSON-RPC 对象（若有）。"""
    outer = _a2a_dict(payload)
    if not outer:
        return None
    detail = outer.get("msgDetail")
    if isinstance(detail, str) and detail.strip():
        return _a2a_dict(detail)
    return detail if isinstance(detail, dict) else None


def _a2a_field(payload: Any, *keys: str) -> str:
    """按给定键名从外层/内层（msgDetail）取字段，取不到返回空串。"""
    outer = _a2a_dict(payload)
    nested = _a2a_nested_dict(payload)
    for source in (outer, nested):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    return ""


def _a2a_session_id(payload: Any) -> str | None:
    """从 A2A 报文里取会话 id（兼容多种字段名与 msgDetail 嵌套形态）。"""
    outer = _a2a_dict(payload)
    if outer is None:
        return None
    payload = outer
    for key in ("sessionId", "session_id", "conversationId", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = _a2a_nested_dict(payload)
    if isinstance(nested, dict):
        return _a2a_session_id(nested) or None
    return None


def _append_a2a(
    role: str,
    payload: Any,
    *,
    channel: str | None = None,
    transport: str | None = None,
    url_key: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """A2A 原始报文落盘：一行 = 一条报文 + 链路上下文（channel/连接/各类 id）。"""
    if not channel_enabled("a2a"):
        return
    try:
        sid = session_id or _a2a_session_id(payload)
        channel_name = str(channel or "").strip() or "unknown"
        line: dict[str, Any] = {
            "role": role,
            "ts": time.time(),
            "channel": channel_name,
            "transport": str(transport or ""),
            "url_key": str(url_key or ""),
            "agent_id": str(agent_id or "") or _a2a_field(payload, "agentId", "agent_id", "botId"),
            "session_id": str(sid or ""),
            "task_id": _a2a_field(payload, "taskId", "task_id"),
            "msg_type": _a2a_field(payload, "msgType", "msg_type", "type"),
            "method": _a2a_field(payload, "method"),
            "message_id": _a2a_field(payload, "id", "messageId", "msgId", "traceId"),
            "data": payload,
        }
        if isinstance(extra, dict) and extra:
            line.update(extra)
        with _LOCK:
            dest = _channel_root("a2a") / _sanitize(channel_name) / f"{_folder_for(sid)}.jsonl"
            _write_line(dest, line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[a2a_trace] 写原始报文失败: %s", exc)


def trace_a2a_inbound(
    payload: Any,
    *,
    channel: str | None = None,
    transport: str | None = None,
    url_key: str | None = None,
    agent_id: str | None = None,
) -> None:
    """记录一条 A2A 入站报文（云/中转 → 渠道，原始形态）。

    原始文本能解析成 JSON 对象时按对象落盘（便于直接检索字段），否则原样落字符串。
    行内附链路上下文：channel / transport / url_key / agent_id / session_id /
    task_id / msg_type / method / message_id，`data` 为完整报文。异常一律吞掉。
    """
    try:
        data = payload
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload).decode("utf-8", errors="replace")
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except Exception:  # noqa: BLE001
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
        _append_a2a(
            "in",
            data,
            channel=channel,
            transport=transport,
            url_key=url_key,
            agent_id=agent_id,
        )
    except Exception:  # noqa: BLE001
        return


def trace_a2a_outbound(
    payload: Any,
    *,
    channel: str | None = None,
    transport: str | None = None,
    url_key: str | None = None,
    agent_id: str | None = None,
) -> None:
    """记录一条 A2A 出站报文（渠道 → 云/中转，原始形态）。异常一律吞掉。"""
    try:
        _append_a2a(
            "out",
            payload,
            channel=channel,
            transport=transport,
            url_key=url_key,
            agent_id=agent_id,
        )
    except Exception:  # noqa: BLE001
        return


def trace_history_record(record: Any, session_id: str | None = None) -> None:
    """把一条落盘的历史记录镜像到 ``<root>/session_flat/<sid>_history.jsonl``。

    行内容就是记录本身（与 history.jsonl 一致）；不打印到 full.log 等日志。
    任何异常都吞掉——dump 绝不参与、也绝不打断历史写入。
    """
    try:
        if not isinstance(record, dict) or not channel_enabled("session"):
            return
        sid = session_id or record.get("request_id") or "unknown"
        with _LOCK:
            dest = _channel_root("session") / f"{_sanitize(sid)}_history.jsonl"
            _write_line(dest, record)
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "channel_enabled",
    "history_records_enabled",
    "trace_a2a_inbound",
    "trace_a2a_outbound",
    "trace_history_record",
    "trace_inbound",
    "trace_outbound",
]
