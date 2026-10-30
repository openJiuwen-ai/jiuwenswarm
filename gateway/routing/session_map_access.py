# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMap Repository 进程入口。

企业版 Gateway 启动时注入；``SessionMap`` 发现已注入后走存储屏蔽层
（DB 表 ``session_map``），未注入时仍读写 ``session_map.json``。
"""

from __future__ import annotations

from gateway.routing.session_map_repository import SessionMapRepository

_repo: SessionMapRepository | None = None


def set_session_map_repository(repo: SessionMapRepository | None) -> None:
    global _repo
    _repo = repo


def get_session_map_repository() -> SessionMapRepository | None:
    return _repo


def clear_session_map_repository() -> None:
    set_session_map_repository(None)


__all__ = [
    "clear_session_map_repository",
    "get_session_map_repository",
    "set_session_map_repository",
]
