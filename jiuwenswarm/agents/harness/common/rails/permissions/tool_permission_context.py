"""Tool permission channel context.

The openjiuwen permission rail uses host callbacks that need to know which
channel is executing (web/acp/tui). We keep this as a ContextVar owned by
jiuwenswarm so request handlers can set/reset it without depending on the
legacy permissions implementation.
"""

from __future__ import annotations

import contextvars

# 当前 asyncio Task 的 channel_id（供工具权限/宿主确认判断）；由接口层在 run_agent 前 set、结束后 reset。
TOOL_PERMISSION_CHANNEL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "jiuwenswarm_tool_permission_channel_id",
    default="",
)


__all__ = ["TOOL_PERMISSION_CHANNEL_ID"]

