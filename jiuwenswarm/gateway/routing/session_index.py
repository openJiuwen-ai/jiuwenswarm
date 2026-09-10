# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway 侧轻量会话索引（仅在 web_session_storage=remote 时启用）。

预览与排序依赖 ``chat.final`` 以及 ``chat.send`` / ``chat.resume`` 路径上的索引更新；
若某条链路未产生 ``chat.final`` 或内容在其它字段，列表预览可能为空或不更新。
同步文件 I/O 封装在 ``asyncio.to_thread`` 中，避免阻塞事件循环。

每条记录格式：
  {
    "session_id": str,
    "role":       str,       # 最近一条消息的角色，如 "user" / "assistant"
    "timestamp":  float,     # Unix 时间戳（最近活动时间）
    "content":    str,       # 最近一条消息的内容预览（截断）
    "user":       str,       # 用户标识（用于多用户隔离）
  }

最多保留 MAX_SESSIONS 条，按 timestamp 降序排列；写入时自动淘汰最旧条目。
``list_sessions_page`` 按 user 过滤，确保多用户环境下不会跨用户泄漏预览。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

MAX_SESSIONS = 100
CONTENT_PREVIEW_LEN = 200

_lock = threading.Lock()


def _index_path() -> Path:
    from jiuwenswarm.common.utils import get_user_workspace_dir

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


def upsert(session_id: str, role: str, content: str, timestamp: float, user: str = "") -> None:
    """插入或更新一条会话记录，并维持最多 MAX_SESSIONS 条限制。

    ``user`` 用于多用户隔离：索引条目携带 user 字段，``list_sessions_page`` 据此过滤。
    """
    preview = content[:CONTENT_PREVIEW_LEN] if content else ""
    with _lock:
        entries = _read_index()
        entries = [e for e in entries if e.get("session_id") != session_id]
        entries.insert(0, {
            "session_id": session_id,
            "role": role,
            "timestamp": timestamp,
            "content": preview,
            "user": user or "",
        })
        entries.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
        entries = entries[:MAX_SESSIONS]
        _write_index(entries)


async def upsert_async(
    session_id: str, role: str, content: str, timestamp: float, user: str = "",
) -> None:
    """``upsert`` 的异步包装，将同步文件 I/O offload 到线程池，避免阻塞事件循环。"""
    await asyncio.to_thread(upsert, session_id, role, content, timestamp, user)


def remove(session_id: str) -> None:
    """从索引中删除指定会话。"""
    with _lock:
        entries = _read_index()
        new_entries = [e for e in entries if e.get("session_id") != session_id]
        if len(new_entries) != len(entries):
            _write_index(new_entries)


async def remove_async(session_id: str) -> None:
    """``remove`` 的异步包装，将同步文件 I/O offload 到线程池，避免阻塞事件循环。"""
    await asyncio.to_thread(remove, session_id)


def list_sessions() -> list[dict[str, Any]]:
    """读取当前索引（最多 MAX_SESSIONS 条，按时间降序）。"""
    with _lock:
        return list(_read_index())


def list_sessions_page(
    params: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """读取索引并应用与本地 ``session.list`` 一致的 limit/offset 分页。

    从 ``params`` 中提取 ``user`` / ``user_id`` 并按用户过滤，确保多用户环境下
    用户只能看到自己的会话预览。
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

    # 用户隔离：从 params 提取 user / user_id，仅返回该用户的会话
    raw_user = params.get("user") or params.get("user_id")
    if isinstance(raw_user, str):
        user = raw_user.strip()
    elif isinstance(raw_user, int):
        user = str(raw_user)
    else:
        user = ""

    with _lock:
        all_entries = list(_read_index())
    if user:
        all_entries = [e for e in all_entries if str(e.get("user", "")) == user]
    total = len(all_entries)
    page = all_entries[offset:offset + limit]
    return page, total, limit, offset


async def list_sessions_page_async(
    params: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """``list_sessions_page`` 的异步包装，将同步文件 I/O offload 到线程池。"""
    return await asyncio.to_thread(list_sessions_page, params)


def is_remote_storage() -> bool:
    """根据配置判断是否启用网关索引（remote）模式。"""
    val = os.getenv("GATEWAY_WEB_SESSION_STORAGE", "").strip().lower()
    if val in ("remote", "local"):
        return val == "remote"
    try:
        from jiuwenswarm.common.config import get_config

        raw = str(
            (get_config().get("gateway") or {}).get("web_session_storage", "local") or "local"
        ).strip().lower()
        return raw == "remote"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session_index] 读取 web_session_storage 配置失败，使用 local: %s", exc)
        return False
