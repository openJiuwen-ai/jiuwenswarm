"""会话对外信息（SessionInfo）投影：会话元数据 → 面向 Web/TUI 消费的字段。

中立门面，Gateway（web handlers）与 AgentServer（E2A handler / 适配器）共用，
避免两侧各自实现一套投影导致字段兜底不一致。

用途：`session.list` / `project.get_sessions` / `project.pinned_sessions` 等
需要把原始 session metadata 投影为对外字段（排除 ``delivery_context`` /
``channel_metadata`` 等内部字段，并为 ``work_mode`` / ``mode`` 提供兜底值）。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE


def to_session_info(meta: dict[str, Any]) -> dict[str, Any]:
    """将会话元数据投影为 SessionInfo（排除内部字段，补齐兜底值）。

    与 Web fallback ``_to_session_info`` 输出完全一致，确保迁移后
    单机模式 ``session.list`` 的外部接口行为不变。
    """
    lum = meta.get("last_user_message_at")
    return {
        "session_id": str(meta.get("session_id", "")),
        "title": str(meta.get("title", "")),
        "created_at": meta.get("created_at", 0),
        "last_message_at": meta.get("last_message_at", 0),
        "message_count": int(meta.get("message_count", 0)),
        "mode": str(meta.get("mode", "unknown")),
        "team_name": str(meta.get("team_name", "")),
        "agent_group_name": str(meta.get("agent_group_name", "")),
        "pinned": bool(meta.get("pinned", False)),
        "pin_order": int(meta.get("pin_order", 0)),
        "project_dir": str(meta.get("project_dir", "")),
        "project_id": str(meta.get("project_id", "")),
        "cron_id": str(meta.get("cron_id", "")),
        "last_user_message_at": (
            lum
            if isinstance(lum, (int, float)) and not isinstance(lum, bool)
            else None
        ),
        "model": str(meta.get("model", "")),
        "work_mode": str(meta.get("work_mode") or DEFAULT_WEB_WORK_MODE),
    }
