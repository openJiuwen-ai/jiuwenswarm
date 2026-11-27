# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Test subagents for SkillDev Agent — eval generation, execution, grading."""

from __future__ import annotations

from typing import Any, List, Optional

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.sys_operation import SysOperation
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenclaw.agentserver.skilldev_agent.subagents.test_case_generator import (
    build_test_case_generator_config,
)
from jiuwenclaw.agentserver.skilldev_agent.subagents.skill_executor import (
    build_skill_executor_config,
)
from jiuwenclaw.agentserver.skilldev_agent.subagents.grader import (
    build_grader_config,
)


def build_skilldev_subagents(
    model: Model,
    *,
    language: str = "cn",
    sys_operation: Optional[SysOperation] = None,
    agent_id: Optional[str] = None,
    workspace_dir: Optional[str] = None,
) -> list[SubAgentConfig]:
    """Build all test-related subagent configs for SkillDev Agent.

    Returns a list of SubAgentConfig that will be passed to create_deep_agent(subagents=...).
    The framework automatically registers SubagentRail + task_tool.

    workspace 显式设置为父 agent 的 workspace，确保 create_subagent 在
    ``spec.workspace is not None`` 时复用传入的 sys_operation，避免新建孤儿 sysop。
    """
    workspace = (
        Workspace(root_path=workspace_dir, language=language, directories=[])
        if workspace_dir is not None
        else None
    )
    return [
        build_skill_executor_config(
            model,
            language=language,
            sys_operation=sys_operation,
            agent_id=f"{agent_id}-executor" if agent_id else None,
            workspace=workspace,
        ),
        build_grader_config(
            model, language=language,
            sys_operation=sys_operation,
            agent_id=f"{agent_id}-grader" if agent_id else None,
            workspace=workspace,
        ),
    ]


__all__ = [
    "build_skilldev_subagents",
    "build_test_case_generator_config",
    "build_skill_executor_config",
    "build_grader_config",
]
