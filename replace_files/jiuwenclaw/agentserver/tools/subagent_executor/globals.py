# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Global executor instance management for subagent execution."""

from __future__ import annotations

from openjiuwen.core.foundation.llm import Model
from openjiuwen.harness import DeepAgent

from jiuwenclaw.utils import logger
from jiuwenclaw.agentserver.tools.subagent_executor.executor import ForkAgentExecutor


# Global executor instance (initialized by init_subagent_executor)
_executor: ForkAgentExecutor | None = None


def get_fork_agent_executor() -> ForkAgentExecutor | None:
    """Get the global fork agent executor."""
    return _executor


def init_subagent_executor(
    parent_agent: DeepAgent,
    model: Model,
    default_role_prompts: dict[str, str] | None = None,
) -> None:
    """Initialize the subagent executor with parent agent and model.

    Args:
        parent_agent: Parent DeepAgent instance
        model: Model instance for creating subagents
        default_role_prompts: Default role prompts (used when role_id not found)
    """
    global _executor
    _executor = ForkAgentExecutor(
        parent_agent,
        model=model,
        default_role_prompts=default_role_prompts,
    )
    logger.info("[Subagent] Initialized subagent executor")