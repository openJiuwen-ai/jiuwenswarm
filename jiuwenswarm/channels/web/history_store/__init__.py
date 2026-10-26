# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved

"""Web 会话历史库包：个人版纯内存 / 企业版 mysql·pg 经 foundation DB-actor。

公开 API（保 ``from jiuwenswarm.channels.web.history_store import ...`` 路径不变）：
- ``ChatHistoryStore`` / ``HistoryBackend``
- ``HistoryFrameRunner`` / ``make_history_callback`` / ``FrameCallback``
- ``set_default_store`` / ``get_default_store``
- ``list_sessions_sync`` / ``get_session_detail_sync``

个人版（memory）仅加载 ``settings``/``store``/``callback``/``api``，不 import foundation；
``tables``/``db_actor`` 仅企业版远程路径惰性加载。
"""

from __future__ import annotations

from .api import (
    count_sessions_sync,
    get_default_store,
    get_session_detail_strict_sync,
    get_session_detail_sync,
    list_sessions_sync,
    set_default_store,
)
from .callback import FrameCallback, HistoryFrameRunner, make_history_callback
from .settings import resolve_history_db_type
from .store import ChatHistoryStore, HistoryBackend

__all__ = [
    "ChatHistoryStore",
    "HistoryBackend",
    "HistoryFrameRunner",
    "make_history_callback",
    "FrameCallback",
    "set_default_store",
    "get_default_store",
    "list_sessions_sync",
    "count_sessions_sync",
    "get_session_detail_sync",
    "get_session_detail_strict_sync",
    "resolve_history_db_type",
]
