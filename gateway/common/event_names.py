# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway↔Web 线协议事件名（与母仓 ``jiuwenswarm.common.schema.message`` 一致）。

这些事件名尚未进入 openjiuwen-protocol，两侧字面量必须保持相同；新增事件请
同时更新母仓同名模块。
"""

from __future__ import annotations

SESSION_REQUEST_COMPLETED_EVENT = "chat.request_completed"
PROMPT_SUGGESTION_READY_EVENT = "prompt_suggestion.ready"
TOPIC_STARTERS_READY_EVENT = "topic_starters.ready"
TOPIC_STARTERS_CONTROL_CHANGED_EVENT = "topic_starters.control_changed"

__all__ = [
    "PROMPT_SUGGESTION_READY_EVENT",
    "SESSION_REQUEST_COMPLETED_EVENT",
    "TOPIC_STARTERS_CONTROL_CHANGED_EVENT",
    "TOPIC_STARTERS_READY_EVENT",
]
