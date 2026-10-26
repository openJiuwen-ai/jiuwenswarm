# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team 事件类型定义.

把 SDK 的 ``MonitorEventType`` 解析为前端消费的 ``team.<category>.<action>``
事件类型字符串及其大类。事件类型字符串本身已编码了大类与动作，故无需再维护
一份枚举 / 映射表：除 message/broadcast 两个特例外，其余全部由 SDK 事件值推导，
新增 SDK 事件无需改动本文件。
"""

from enum import Enum

from openjiuwen.agent_teams.monitor.models import MonitorEventType


class TeamEventCategory(str, Enum):
    """Team 事件大类枚举.

    前端根据大类分别显示在不同区域：
    - team.member: 成员事件区域
    - team.task: 任务事件区域
    - team.message: 消息事件区域（需要记录到历史）
    """
    MEMBER = "team.member"
    TASK = "team.task"
    MESSAGE = "team.message"


# 不符合 ``<category>_<action>`` 结构、无法从 SDK 事件值推导的特例。
_TYPE_OVERRIDES: dict[MonitorEventType, str] = {
    MonitorEventType.MESSAGE: "team.message.p2p",
    MonitorEventType.BROADCAST: "team.message.broadcast",
}

# 不下发给前端的 SDK 事件：team 生命周期与 member_canceled 仅用于内部/可观测性。
_NOT_FORWARDED: frozenset[MonitorEventType] = frozenset({
    MonitorEventType.TEAM_CREATED,
    MonitorEventType.TEAM_CLEANED,
    MonitorEventType.TEAM_STANDBY,
    MonitorEventType.MEMBER_CANCELED,
})


def resolve_team_event(
    sdk_event_type: MonitorEventType,
) -> tuple[str, TeamEventCategory] | None:
    """把 SDK 事件解析为前端事件类型字符串及其大类.

    事件类型字符串形如 ``team.<category>.<action>``：SDK 事件值的第一段为大类，
    其余为动作（如 ``member_status_changed`` -> ``team.member.status_changed``）。
    message/broadcast 走 ``_TYPE_OVERRIDES`` 特例。

    Args:
        sdk_event_type: SDK 的 MonitorEventType。

    Returns:
        ``(事件类型字符串, 大类)``；当该事件不下发给前端时返回 None。
    """
    if sdk_event_type in _NOT_FORWARDED:
        return None
    type_str = _TYPE_OVERRIDES.get(sdk_event_type)
    if type_str is None:
        # e.g. task_submitted_for_review -> team.task.submitted_for_review
        type_str = "team." + sdk_event_type.value.replace("_", ".", 1)
    category = TeamEventCategory("team." + type_str.split(".")[1])
    return type_str, category
