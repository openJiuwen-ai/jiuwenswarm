# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""会话目录名校验：防止路径穿越与非安全 path 分量写入 sessions 根目录."""

from __future__ import annotations

import re
from pathlib import Path

_MAX_LEN = 256
# 与前端 generateSessionId（sess_<hex>_<hex>）及常见 opaque id 对齐；禁止路径分隔符与「..」。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def normalize_safe_session_id(session_id: str) -> str | None:
    """若 ``session_id`` 可作为单一安全目录名则返回规范化字符串，否则返回 None。"""
    sid = str(session_id or "").strip()
    if not sid or len(sid) > _MAX_LEN:
        return None
    if ".." in sid or sid in (".", ".."):
        return None
    if "/" in sid or "\\" in sid or "\x00" in sid:
        return None
    if not _SESSION_ID_RE.match(sid):
        return None
    return sid


def resolve_session_dir_under_root(sessions_root: Path, session_id: str) -> Path | None:
    """解析 ``sessions_root / session_id``；若无法严格落在 ``sessions_root`` 下则返回 None。"""
    safe = normalize_safe_session_id(session_id)
    if safe is None:
        return None
    try:
        root = sessions_root.resolve()
        target = (sessions_root / safe).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    return target
