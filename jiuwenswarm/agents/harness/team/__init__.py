# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Agent Team 模块 - 多智能体协作团队支持.

Importing this package does not load TeamManager or DeepAgent. Those names
resolve on first use.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "load_team_spec_dict",
    "get_team_template_snapshot",
    "list_team_template_summaries",
    "TeamManager",
    "cancel_all_team_stream_tasks_across_managers",
    "find_team_skill_rail_across_managers",
    "get_all_team_managers",
    "get_team_manager",
    "refresh_team_shared_skill_links_across_managers",
    "reset_team_manager",
    "stop_team_session_runtime_across_managers",
    "TeamNameGenerationError",
    "generate_team_name",
    "TeamMonitorHandler",
    "WorkflowMonitorHandler",
]

_EXPORTS = {
    "load_team_spec_dict": "jiuwenswarm.agents.harness.team.config_loader",
    "get_team_template_snapshot": "jiuwenswarm.agents.harness.team.config_loader",
    "list_team_template_summaries": "jiuwenswarm.agents.harness.team.config_loader",
    "TeamManager": "jiuwenswarm.agents.harness.team.team_manager",
    "cancel_all_team_stream_tasks_across_managers": "jiuwenswarm.agents.harness.team.team_manager",
    "find_team_skill_rail_across_managers": "jiuwenswarm.agents.harness.team.team_manager",
    "get_all_team_managers": "jiuwenswarm.agents.harness.team.team_manager",
    "get_team_manager": "jiuwenswarm.agents.harness.team.team_manager",
    "refresh_team_shared_skill_links_across_managers": "jiuwenswarm.agents.harness.team.team_manager",
    "reset_team_manager": "jiuwenswarm.agents.harness.team.team_manager",
    "stop_team_session_runtime_across_managers": "jiuwenswarm.agents.harness.team.team_manager",
    "TeamNameGenerationError": "jiuwenswarm.agents.harness.team.team_name_generator",
    "generate_team_name": "jiuwenswarm.agents.harness.team.team_name_generator",
    "TeamMonitorHandler": "jiuwenswarm.agents.harness.team.handlers.team_monitor_handler",
    "WorkflowMonitorHandler": "jiuwenswarm.agents.harness.team.handlers.workflow_monitor_handler",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is not None:
        module = import_module(module_name)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if not name.startswith("_"):
        submodule = f"{__name__}.{name}"
        try:
            module = import_module(submodule)
        except ModuleNotFoundError as exc:
            if exc.name != submodule:
                raise
        else:
            globals()[name] = module
            return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
