# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway 侧轻量会话索引（仅在 web_session_storage=remote 时启用）。

预览与排序依赖 ``chat.final`` 以及 ``chat.send`` / ``chat.resume`` 路径上的索引更新；
若某条链路未产生 ``chat.final`` 或内容在其它字段，列表预览可能为空或不更新。
高流量下每次更新为同步读改写 JSON（threading.Lock），可能成为热点，后续如需可改为批量或异步落盘。

每条记录格式：
  {
    "session_id": str,
    "role":       str,       # 最近一条消息的角色，如 "user" / "assistant"
    "timestamp":  float,     # Unix 时间戳（最近活动时间）
    "content":    str,       # 最近一条消息的内容预览（截断）
  }

最多保留 MAX_SESSIONS 条，按 timestamp 降序排列；写入时自动淘汰最旧条目。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

MAX_SESSIONS = 10
CONTENT_PREVIEW_LEN = 200

_lock = threading.Lock()
_web_session_storage: str | None = None


def _index_path() -> Path:
    from jiuwenclaw.utils import get_user_workspace_dir

    p = get_user_workspace_dir() / "gateway" / "session_index.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_index] 读取索引失败: %s", exc)
    return []


def _write_index(entries: list[dict[str, Any]]) -> None:
    try:
        path = _index_path()
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_index] 写入索引失败: %s", exc)


def upsert(session_id: str, role: str, content: str, timestamp: float) -> None:
    """插入或更新一条会话记录，并维持最多 MAX_SESSIONS 条限制。"""
    preview = content[:CONTENT_PREVIEW_LEN] if content else ""
    with _lock:
        entries = _read_index()
        # 移除同 session_id 的旧条目
        entries = [e for e in entries if e.get("session_id") != session_id]
        entries.insert(0, {
            "session_id": session_id,
            "role": role,
            "timestamp": timestamp,
            "content": preview,
        })
        # 按时间降序排列后截断
        entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        entries = entries[:MAX_SESSIONS]
        _write_index(entries)


def remove(session_id: str) -> None:
    """从索引中删除指定会话。"""
    with _lock:
        entries = _read_index()
        new_entries = [e for e in entries if e.get("session_id") != session_id]
        if len(new_entries) != len(entries):
            _write_index(new_entries)


def list_sessions() -> list[dict[str, Any]]:
    """读取当前索引（最多 MAX_SESSIONS 条，按时间降序）。"""
    with _lock:
        return list(_read_index())


def list_sessions_page(
    params: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """读取索引并应用与本地 ``session.list`` 一致的 limit/offset 分页。

    索引本身最多保留 MAX_SESSIONS 条，故 total 不超过 MAX_SESSIONS；
    limit 默认 20、最大 200，与网关本地扫盘列表行为对齐。
    """
    params = dict(params or {})
    limit = 20
    offset = 0
    raw_limit = params.get("limit")
    if isinstance(raw_limit, int):
        limit = raw_limit
    elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
        limit = int(raw_limit.strip())
    raw_offset = params.get("offset")
    if isinstance(raw_offset, int):
        offset = raw_offset
    elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
        offset = int(raw_offset.strip())
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with _lock:
        all_entries = list(_read_index())
    total = len(all_entries)
    page = all_entries[offset:offset + limit]
    return page, total, limit, offset


def is_remote_storage() -> bool:
    """根据配置判断是否启用网关索引（remote）模式。"""
    global _web_session_storage
    if _web_session_storage is not None:
        return _web_session_storage == "remote"
    val = os.getenv("GATEWAY_WEB_SESSION_STORAGE", "").strip().lower()
    if val in ("remote", "local"):
        _web_session_storage = val
        return val == "remote"
    try:
        from jiuwenclaw.config import get_config

        raw = str(
            (get_config().get("gateway") or {}).get("web_session_storage", "local") or "local"
        ).strip().lower()
        _web_session_storage = raw
        return raw == "remote"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_index] 读取 web_session_storage 配置失败，使用 local: %s", exc)
        return False
