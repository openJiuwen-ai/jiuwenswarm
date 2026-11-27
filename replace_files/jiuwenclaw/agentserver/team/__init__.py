# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Agent Team 模块 - 多智能体协作团队支持.

此模块提供：
- Team 配置加载
- Team 生命周期管理 (Persistent模式)
- Team Monitor 集成
"""

from __future__ import annotations

from jiuwenclaw.agentserver.team.config_loader import load_team_spec_dict
from jiuwenclaw.agentserver.team.team_manager import TeamManager, get_team_manager
from jiuwenclaw.agentserver.team.monitor_handler import TeamMonitorHandler

__all__ = [
    "load_team_spec_dict",
    "TeamManager",
    "get_team_manager",
    "TeamMonitorHandler",
]
