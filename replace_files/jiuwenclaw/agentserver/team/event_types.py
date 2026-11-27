# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team 事件类型定义.

定义 Team 模式的事件类型常量和 SDK 事件类型映射。
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
    MEMBER_ACTIVITY = "team.member.activity"


class TeamEventType(str, Enum):
    """Team 事件类型枚举.
    
    命名规范: team.{category}.{action}
    - member: 成员相关事件
    - task: 任务相关事件
    - message: 消息相关事件
    """
    
    # 成员事件
    MEMBER_SPAWNED = "team.member.spawned"
    MEMBER_STATUS_CHANGED = "team.member.status_changed"
    MEMBER_EXECUTION_CHANGED = "team.member.execution_changed"
    MEMBER_RESTARTED = "team.member.restarted"
    MEMBER_SHUTDOWN = "team.member.shutdown"
    
    # 任务事件
    TASK_CREATED = "team.task.created"
    TASK_CLAIMED = "team.task.claimed"
    TASK_COMPLETED = "team.task.completed"
    TASK_CANCELLED = "team.task.cancelled"
    TASK_UNBLOCKED = "team.task.unblocked"
    
    # 消息事件
    MESSAGE_P2P = "team.message.p2p"
    MESSAGE_BROADCAST = "team.message.broadcast"

    # 成员运行时活动事件（非 SDK 事件，来自 rail 本地广播）
    MEMBER_TOOL_CALL = "team.member.tool_call"
    MEMBER_TOOL_RESULT = "team.member.tool_result"


EVENT_TYPE_TO_CATEGORY: dict[TeamEventType, TeamEventCategory] = {
    # 成员事件
    TeamEventType.MEMBER_SPAWNED: TeamEventCategory.MEMBER,
    TeamEventType.MEMBER_STATUS_CHANGED: TeamEventCategory.MEMBER,
    TeamEventType.MEMBER_EXECUTION_CHANGED: TeamEventCategory.MEMBER,
    TeamEventType.MEMBER_RESTARTED: TeamEventCategory.MEMBER,
    TeamEventType.MEMBER_SHUTDOWN: TeamEventCategory.MEMBER,
    # 任务事件
    TeamEventType.TASK_CREATED: TeamEventCategory.TASK,
    TeamEventType.TASK_CLAIMED: TeamEventCategory.TASK,
    TeamEventType.TASK_COMPLETED: TeamEventCategory.TASK,
    TeamEventType.TASK_CANCELLED: TeamEventCategory.TASK,
    TeamEventType.TASK_UNBLOCKED: TeamEventCategory.TASK,
    # 消息事件
    TeamEventType.MESSAGE_P2P: TeamEventCategory.MESSAGE,
    TeamEventType.MESSAGE_BROADCAST: TeamEventCategory.MESSAGE,
    # 成员运行时活动事件
    TeamEventType.MEMBER_TOOL_CALL: TeamEventCategory.MEMBER_ACTIVITY,
    TeamEventType.MEMBER_TOOL_RESULT: TeamEventCategory.MEMBER_ACTIVITY,
}

SDK_TO_TEAM_EVENT_MAP: dict[MonitorEventType, TeamEventType] = {
    MonitorEventType.MEMBER_SPAWNED: TeamEventType.MEMBER_SPAWNED,
    MonitorEventType.MEMBER_STATUS_CHANGED: TeamEventType.MEMBER_STATUS_CHANGED,
    MonitorEventType.MEMBER_EXECUTION_CHANGED: TeamEventType.MEMBER_EXECUTION_CHANGED,
    MonitorEventType.MEMBER_RESTARTED: TeamEventType.MEMBER_RESTARTED,
    MonitorEventType.MEMBER_SHUTDOWN: TeamEventType.MEMBER_SHUTDOWN,
    MonitorEventType.TASK_CREATED: TeamEventType.TASK_CREATED,
    MonitorEventType.TASK_CLAIMED: TeamEventType.TASK_CLAIMED,
    MonitorEventType.TASK_COMPLETED: TeamEventType.TASK_COMPLETED,
    MonitorEventType.TASK_CANCELLED: TeamEventType.TASK_CANCELLED,
    MonitorEventType.TASK_UNBLOCKED: TeamEventType.TASK_UNBLOCKED,
    MonitorEventType.MESSAGE: TeamEventType.MESSAGE_P2P,
    MonitorEventType.BROADCAST: TeamEventType.MESSAGE_BROADCAST,
}


def get_team_event_type(sdk_event_type: MonitorEventType) -> TeamEventType | None:
    """将 SDK 事件类型映射为 Team 事件类型.
    
    Args:
        sdk_event_type: SDK 的 MonitorEventType
        
    Returns:
        TeamEventType 或 None（如果未映射）
    """
    return SDK_TO_TEAM_EVENT_MAP.get(sdk_event_type)


def get_event_category(event_type: TeamEventType) -> TeamEventCategory:
    """获取事件的大类.
    
    Args:
        event_type: Team 事件类型
        
    Returns:
        事件大类
    """
    return EVENT_TYPE_TO_CATEGORY.get(event_type, TeamEventCategory.MEMBER)


def is_message_event(event_type: TeamEventType) -> bool:
    """判断是否为消息事件（需要记录到历史）.
    
    Args:
        event_type: Team 事件类型
        
    Returns:
        是否为消息事件
    """
    return EVENT_TYPE_TO_CATEGORY.get(event_type) == TeamEventCategory.MESSAGE
