# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名：实现已迁 ``openjiuwen_protocol.message``。

仅保留 ``jiuwenswarm.common.schema.message`` 旧 import 路径兼容，re-export 协议包
的同一对象——``Message`` / ``ReqMethod`` / ``EventType`` / ``Mode`` 及线协议事件名
常量。``ReqMethod`` 的成员扩展（``RECOMMENDATION`` / ``TRAJECTORY_*``）现由协议包
直接提供；仅协议包当前 pin 尚未发布的成员（``SESSION_ASSETS_REMOVE``）保留最小
运行时补齐，避免两仓各维护一份枚举类。
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


def _ensure_req_method(name: str, value: str) -> None:
    """Register a ReqMethod member the pinned protocol package does not ship yet.

    ``openjiuwen-protocol`` owns the enum; 这里只为协议包尚未发布的成员开一个缺口，
    待协议包提供同名同值成员后本函数与调用一并删除。
    """
    existing = ReqMethod.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"ReqMethod.{name} is {existing.value!r}, expected {value!r}"
            )
        return
    if any(item.value == value for item in ReqMethod):
        raise RuntimeError(f"ReqMethod value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(ReqMethod)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(ReqMethod, name, member)
    ReqMethod._member_map_[name] = member  # pylint: disable=protected-access
    ReqMethod._value2member_map_[value] = member  # pylint: disable=protected-access
    ReqMethod._member_names_.append(name)  # pylint: disable=protected-access


# 移除 composer 附件 chip 时同时清理 session-asset 注册表项（见 session_assets.py）；
# 协议包当前 pin 尚未定义该成员。
_ensure_req_method("SESSION_ASSETS_REMOVE", "session.assets.remove")

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
