# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Message schema compatibility exports.

``ReqMethod`` is owned by JiuwenSwarm in ``common.schema.req_method``. Other
message types continue to come from the protocol package.
"""

from __future__ import annotations

from enum import Enum

from e2a.message import (  # noqa: F401
    PROMPT_SUGGESTION_READY_EVENT,
    SESSION_REQUEST_COMPLETED_EVENT,
    TEAM_ATTACH_ONLY_PARAM,
    TOPIC_STARTERS_CONTROL_CHANGED_EVENT,
    TOPIC_STARTERS_READY_EVENT,
    EventType,
    Message,
    Mode,
)

from jiuwenswarm.common.schema.req_method import ReqMethod


def _ensure_protocol_member(enum_cls: type[Enum], name: str, value: str) -> None:
    """Register a member of a protocol enum the pinned protocol package does not ship yet.

    ``openjiuwen-protocol`` owns the enum; 这里只为协议包尚未发布的成员开一个缺口，
    待协议包提供同名同值成员后本函数与调用一并删除。
    """
    existing = enum_cls.__members__.get(name)
    if existing is not None:
        if existing.value != value:
            raise RuntimeError(
                f"{enum_cls.__name__}.{name} is {existing.value!r}, expected {value!r}"
            )
        return
    if any(item.value == value for item in enum_cls):
        raise RuntimeError(f"{enum_cls.__name__} value {value!r} is already registered")
    # Enum exposes no public API for adding a member, so extending the pinned
    # protocol enum requires its internal registries. Keep those accesses here.
    member = object.__new__(enum_cls)
    member._name_ = name  # pylint: disable=protected-access
    member._value_ = value  # pylint: disable=protected-access
    type.__setattr__(enum_cls, name, member)
    enum_cls._member_map_[name] = member  # pylint: disable=protected-access
    enum_cls._value2member_map_[value] = member  # pylint: disable=protected-access
    enum_cls._member_names_.append(name)  # pylint: disable=protected-access


# The Gateway forwards an event only when the protocol EventType knows it; the
# pinned protocol package does not define this one yet.
_ensure_protocol_member(EventType, "CHAT_CLIENT_TOOL_REQUEST", "chat.client_tool_request")
# Client attachments use this event; the pinned protocol package does not
# define it yet.
_ensure_protocol_member(EventType, "CLIENT_ATTACHMENTS_CHANGED", "client.attachments_changed")

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
