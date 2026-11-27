# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""session.rename 共享实现：AgentWebSocketServer 与 cli_channel 本地回退共用。

单次 rename 允许的最大标题长度，与 cli 侧保持一致，防御异常/恶意输入污染 metadata.json。
"""
from __future__ import annotations

from typing import Any

_RENAME_TITLE_MAX_LEN = 200


def apply_session_rename(
    params: Any,
    connection_session_id: str,
    *,
    init_channel_id: str = "tui",
) -> tuple[bool, dict[str, Any] | None, str | None, str | None]:
    """实现 session.rename 三种语义：查询(None) / 清除(空串 strip 后) / 设置。

    Returns:
        (True, payload, None, None) 成功；
        (False, None, error_message, error_code) 失败（error_code 如 ``BAD_REQUEST``）。
    """
    from jiuwenclaw.agentserver.session_metadata import (
        get_session_metadata,
        init_session_metadata,
        update_session_metadata,
    )

    if not isinstance(params, dict):
        params = {}

    target = str(params.get("session_id") or connection_session_id).strip()
    if not target:
        return False, None, "session_id is required", "BAD_REQUEST"

    metadata = get_session_metadata(target)
    raw_title = params.get("title")

    if raw_title is None:
        current_title = metadata.get("title", "") if metadata else ""
        payload = {
            "session_id": target,
            "title": current_title,
            "previous_title": current_title,
        }
        return True, payload, None, None

    if not metadata:
        init_session_metadata(session_id=target, channel_id=init_channel_id)
        metadata = get_session_metadata(target)
    previous_title = metadata.get("title", "")

    new_title = str(raw_title).strip()[:_RENAME_TITLE_MAX_LEN]

    if new_title:
        update_session_metadata(session_id=target, title=new_title)
    else:
        update_session_metadata(session_id=target, clear_title=True)

    updated = get_session_metadata(target)
    payload = {
        "session_id": target,
        "title": updated.get("title", ""),
        "previous_title": previous_title,
    }
    return True, payload, None, None
