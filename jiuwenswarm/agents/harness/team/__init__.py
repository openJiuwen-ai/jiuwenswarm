# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent Team 模块 - 多智能体协作团队支持.

此模块提供：
- Team 配置加载
- Team 生命周期管理 (Persistent模式)
- Team Monitor 集成

Exports are lazy so subpackages such as ``team.expert_org`` can be imported
without pulling TeamManager / config_loader dependencies first.
"""

from __future__ import annotations

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


def __getattr__(name: str):
    if name in {
        "load_team_spec_dict",
        "get_team_template_snapshot",
        "list_team_template_summaries",
    }:
        from jiuwenswarm.agents.harness.team.config_loader import (
            get_team_template_snapshot,
            list_team_template_summaries,
            load_team_spec_dict,
        )

        mapping = {
            "load_team_spec_dict": load_team_spec_dict,
            "get_team_template_snapshot": get_team_template_snapshot,
            "list_team_template_summaries": list_team_template_summaries,
        }
        return mapping[name]
    if name in {
        "TeamManager",
        "cancel_all_team_stream_tasks_across_managers",
        "find_team_skill_rail_across_managers",
        "get_all_team_managers",
        "get_team_manager",
        "reload_team_skill_views_across_managers",
        "reset_team_manager",
        "stop_all_paused_team_session_runtimes_across_managers",
        "stop_team_session_runtime_across_managers",
    }:
        from jiuwenswarm.agents.harness.team.team_manager import (
            TeamManager,
            cancel_all_team_stream_tasks_across_managers,
            find_team_skill_rail_across_managers,
            get_all_team_managers,
            get_team_manager,
            reload_team_skill_views_across_managers,
            reset_team_manager,
            stop_all_paused_team_session_runtimes_across_managers,
            stop_team_session_runtime_across_managers,
        )

        mapping = {
            "TeamManager": TeamManager,
            "cancel_all_team_stream_tasks_across_managers": cancel_all_team_stream_tasks_across_managers,
            "find_team_skill_rail_across_managers": find_team_skill_rail_across_managers,
            "get_all_team_managers": get_all_team_managers,
            "get_team_manager": get_team_manager,
            "reload_team_skill_views_across_managers": reload_team_skill_views_across_managers,
            "reset_team_manager": reset_team_manager,
            "stop_all_paused_team_session_runtimes_across_managers": (
                stop_all_paused_team_session_runtimes_across_managers
            ),
            "stop_team_session_runtime_across_managers": stop_team_session_runtime_across_managers,
        }
        return mapping[name]
    if name in {"TeamNameGenerationError", "generate_team_name"}:
        from jiuwenswarm.agents.harness.team.team_name_generator import (
            TeamNameGenerationError,
            generate_team_name,
        )

        return {
            "TeamNameGenerationError": TeamNameGenerationError,
            "generate_team_name": generate_team_name,
        }[name]
    if name == "TeamMonitorHandler":
        from jiuwenswarm.agents.harness.team.handlers.team_monitor_handler import (
            TeamMonitorHandler,
        )

        return TeamMonitorHandler
    if name == "WorkflowMonitorHandler":
        from jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler import (
            WorkflowMonitorHandler,
        )

        return WorkflowMonitorHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
