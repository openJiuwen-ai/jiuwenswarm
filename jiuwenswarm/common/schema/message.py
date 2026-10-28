# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``openjiuwen_protocol.message``。

仅保留 ``jiuwenswarm.common.schema.message`` 旧 import 路径兼容，re-export 协议包
的同一对象——``Message`` / ``ReqMethod`` / ``EventType`` / ``Mode`` 及线协议事件名
常量。``ReqMethod`` 的成员扩展（``RECOMMENDATION`` / ``TRAJECTORY_*``）现由协议包
直接提供，本模块不再做运行时枚举补丁（同一枚举类在两仓必须只有一个定义）。
"""

from __future__ import annotations

from openjiuwen_protocol.message import (  # noqa: F401
    PROMPT_SUGGESTION_READY_EVENT,
    SESSION_REQUEST_COMPLETED_EVENT,
    TEAM_ATTACH_ONLY_PARAM,
    TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
    TOPIC_STARTERS_READY_EVENT,
    EventType,
    Message,
    Mode,
    ReqMethod,
)

__all__ = [
    "PROMPT_SUGGESTION_READY_EVENT",
    "SESSION_REQUEST_COMPLETED_EVENT",
    "TEAM_ATTACH_ONLY_PARAM",
    "TOPIC_STARTERS_CONTROL_CHANGED_EVENT",
    "TOPIC_STARTERS_READY_EVENT",
    "EventType",
    "Message",
    "Mode",
    "ReqMethod",
]
