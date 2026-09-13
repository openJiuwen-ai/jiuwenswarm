# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""Web 会话历史公开同步 API（http.server / fastapi 线程用）。"""

from __future__ import annotations

import threading
from typing import Any

from .store import ChatHistoryStore

_default_store: ChatHistoryStore | None = None
_default_store_lock = threading.Lock()


def get_default_store() -> ChatHistoryStore:
    """进程内单例，避免 HTTP 每次请求重建库。"""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = ChatHistoryStore.from_env()
        return _default_store


def set_default_store(store: ChatHistoryStore) -> None:
    global _default_store
    with _default_store_lock:
        _default_store = store


def _coerce_store(store: Any | None) -> ChatHistoryStore | None:
    if store is None:
        st = get_default_store()
        return st if st.available else None
    if isinstance(store, ChatHistoryStore):
        return store if store.available else None
    return None


def list_sessions_sync(
    store: ChatHistoryStore | None = None,
    *,
    limit: int = 20,
    offset: int = 0,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> list[dict[str, Any]]:
    """同步读会话列表（http.server 线程用）。库不可用返回空。"""
    st = _coerce_store(store)
    if st is None:
        return []
    return st.list_sessions_blocking(
        limit=limit, offset=offset, user=user, group_id=group_id, bot_id=bot_id,
    )


def get_session_detail_sync(
    session_id: str,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> dict[str, Any] | None:
    """同步读会话详情。行身份与查询身份不符时返回 None（等同不存在）。"""
    if not session_id:
        return None
    st = _coerce_store(store)
    if st is None:
        return None
    return st.get_session_detail_blocking(
        str(session_id), user=user, group_id=group_id, bot_id=bot_id,
    )


def count_sessions_sync(
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> int:
    """同步统计会话总数（分页 total 用）。

    与 list_sessions_sync 不同：store/DB 不可用时抛异常，调用方据此区分
    "确实为空"与"库故障"。
    """
    st = _coerce_store(store)
    if st is None:
        raise RuntimeError("web history store unavailable")
    return st.count_sessions_blocking(user=user, group_id=group_id, bot_id=bot_id)


def get_session_detail_strict_sync(
    session_id: str,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> dict[str, Any] | None:
    """同步读会话详情（严格版）：store/DB 故障抛异常，None 仅表示会话不存在。"""
    if not session_id:
        return None
    st = _coerce_store(store)
    if st is None:
        raise RuntimeError("web history store unavailable")
    return st.get_session_detail_strict_blocking(
        str(session_id), user=user, group_id=group_id, bot_id=bot_id,
    )


def delete_session_sync(
    session_id: str,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> bool:
    """同步删除会话（sessions 行 + messages 行）。行身份 scope 不匹配拒绝删除。"""
    if not session_id:
        return False
    st = _coerce_store(store)
    if st is None:
        return False
    return st.delete_session_blocking(
        str(session_id), user=user, group_id=group_id, bot_id=bot_id,
    )


def set_session_pinned_sync(
    session_id: str,
    pinned: bool,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> tuple[bool, int] | None:
    """同步置顶/取消置顶会话（remote 模式 session.pin 用）。

    严格版语义：store/DB 不可用抛异常（调用方区分故障与不存在）；
    会话不存在返回 ``None``；成功返回 ``(pinned, pin_order)``。
    重编号范围限定当前身份 scope。
    """
    if not session_id:
        return None
    st = _coerce_store(store)
    if st is None:
        raise RuntimeError("web history store unavailable")
    return st.set_session_pinned_blocking(
        str(session_id), bool(pinned), user=user, group_id=group_id, bot_id=bot_id,
    )


def rename_session_sync(
    session_id: str,
    title: str | None,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> dict[str, Any] | None:
    """同步改会话标题（remote 模式 session.rename 用）。

    严格版语义：store/DB 不可用抛异常（调用方区分故障与不存在）；
    会话不存在或行身份 scope 不匹配返回 ``None``；成功返回
    ``{"title", "previous_title"}``。``title=None`` 仅查询；``""`` 清空；
    非空为设置（截断由调用方完成）。
    """
    if not session_id:
        return None
    st = _coerce_store(store)
    if st is None:
        raise RuntimeError("web history store unavailable")
    return st.rename_session_blocking(
        str(session_id), title, user=user, group_id=group_id, bot_id=bot_id,
    )


def list_pinned_sessions_sync(
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> list[dict[str, Any]]:
    """同步读当前身份 scope 内全部置顶会话（remote 模式 project.pinned_sessions 用）。库不可用返回空。"""
    st = _coerce_store(store)
    if st is None:
        return []
    return st.list_pinned_sessions_blocking(user=user, group_id=group_id, bot_id=bot_id)


def list_all_sessions_sync(
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """同步读当前身份 scope 内全部会话行（project 视图切 PG 用）。

    库不可用返回 ``None``，调用方据此回退 pod 拉取。
    """
    st = _coerce_store(store)
    if st is None:
        return None
    return st.list_all_sessions_blocking(user=user, group_id=group_id, bot_id=bot_id)


def ensure_session_row_sync(
    session_id: str,
    store: ChatHistoryStore | None = None,
    *,
    user: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
    project_id: str | None = None,
    cron_id: str | None = None,
    work_mode: str | None = None,
    title: str | None = None,
    ts: float,
) -> bool:
    """session.create 落行（web handler 与 cron scheduler 共用）。

    严格版：store/DB 不可用抛异常，由调用方决定忽略（会话行会由首条
    消息的 record_user 兜底建立）。
    """
    if not session_id:
        return False
    st = _coerce_store(store)
    if st is None:
        raise RuntimeError("web history store unavailable")
    return st.ensure_session_row_blocking(
        str(session_id),
        user=user,
        group_id=group_id,
        bot_id=bot_id,
        project_id=project_id,
        cron_id=cron_id,
        work_mode=work_mode,
        title=title,
        ts=ts,
    )


def touch_session_sync(
    session_id: str,
    store: ChatHistoryStore | None = None,
    *,
    ts: float,
) -> bool:
    """仅刷新会话活动时间（cron run 后维持面板排序）。行不存在为 no-op。"""
    if not session_id:
        return False
    st = _coerce_store(store)
    if st is None:
        return False
    return st.touch_session_blocking(str(session_id), ts=ts)
