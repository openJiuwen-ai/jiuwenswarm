# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""权限引擎 allow/deny 判定的「暂存 → 取出」。

permission rail 的 ``before_tool_call`` 阶段 ``write_stream`` 未进入 consumer 读取
窗口，直接下发 ``chat.permission_decision`` 会丢事件；因此判定先暂存到
``ctx.extra``（按 tool_call_id），由 ``stream_event_rail.after_tool_call`` 在工具
结果时刻经会话通道下发。

本模块保持 Electron/框架无关、无循环依赖，便于单测覆盖暂存/取出语义。
"""

from __future__ import annotations

from typing import Any

# ctx.extra 中的暂存键：{tool_call_id: {tool_name, tool_call_id, decision, matched_rule, reason}}
PERMISSION_DECISIONS_EXTRA_KEY = "_jiuwenswarm_permission_decisions"


def stash_permission_decision(
    ctx: Any,
    tool_call: Any,
    decision: str,
    *,
    matched_rule: str = "",
    reason: str = "",
) -> None:
    """暂存一次 allow/deny 判定（ask 等其它级别忽略）。

    ``ctx.extra`` 不是 dict 时静默跳过（回调边界可能不带 extra）。
    """
    if decision not in ("allow", "deny"):
        return
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        return
    store = extra.get(PERMISSION_DECISIONS_EXTRA_KEY)
    if not isinstance(store, dict):
        store = {}
        extra[PERMISSION_DECISIONS_EXTRA_KEY] = store
    tool_call_id = str(getattr(tool_call, "id", "") or "")
    store[tool_call_id] = {
        "tool_name": str(getattr(tool_call, "name", "") or ""),
        "tool_call_id": tool_call_id,
        "decision": decision,
        "matched_rule": str(matched_rule or ""),
        "reason": str(reason or ""),
    }


def pop_permission_decision(ctx: Any, tool_call_id: str) -> dict[str, str] | None:
    """取出并移除该 tool_call 暂存的判定；无则返回 None。"""
    extra = getattr(ctx, "extra", None)
    if not isinstance(extra, dict):
        return None
    store = extra.get(PERMISSION_DECISIONS_EXTRA_KEY)
    if not isinstance(store, dict):
        return None
    value = store.pop(str(tool_call_id or ""), None)
    return value if isinstance(value, dict) else None