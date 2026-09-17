# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent Team 模块 - 多智能体协作团队支持.

此模块提供：
- Team 配置加载
- Team 生命周期管理 (Persistent模式)
- Team Monitor 集成
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.team.config_loader import (
    get_team_template_snapshot,
    list_team_template_summaries,
    load_team_spec_dict,
)
from jiuwenswarm.agents.harness.team.team_manager import (
    cancel_all_team_stream_tasks_across_managers,
    TeamManager,
    find_team_skill_rail_across_managers,
    get_all_team_managers,
    get_team_manager,
    reload_team_skill_views_across_managers,
    reset_team_manager,
    stop_all_paused_team_session_runtimes_across_managers,
    stop_team_session_runtime_across_managers,
)
from jiuwenswarm.agents.harness.team.team_name_generator import (
    TeamNameGenerationError,
    generate_team_name,
)
from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler

__all__ = [
    "load_team_spec_dict",
    "get_team_template_snapshot",
    "list_team_template_summaries",
    "TeamManager",
    "cancel_all_team_stream_tasks_across_managers",
    "find_team_skill_rail_across_managers",
    "get_all_team_managers",
    "get_team_manager",
    "reload_team_skill_views_across_managers",
    "reset_team_manager",
    "stop_all_paused_team_session_runtimes_across_managers",
    "stop_team_session_runtime_across_managers",
    "TeamNameGenerationError",
    "generate_team_name",
    "TeamMonitorHandler",
    "WorkflowMonitorHandler",
]
