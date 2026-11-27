# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Subagent executor package - Fork agent and spawn subagent execution.

This package provides the core infrastructure for creating and managing
subagents (fork_agent and spawn_subagent) in the DeepAgent architecture.
"""

from __future__ import annotations

# Context variables
from .context_vars import (
    set_subagent_parent_session,
    get_subagent_parent_session,
    set_current_agent_context,
    get_current_agent_context,
    set_current_agent_subagent_id,
    get_current_agent_subagent_id,
    _get_llm_trace_session_id_var,
)

# Rails
from .rails import (
    ForkMessageInjectionRail,
    SubagentContextRail,
)

# Session proxy
from .session_proxy import (
    SubagentSessionProxy,
)

# Executor
from .executor import (
    ForkAgentExecutor,
)

# Global instance management
from .globals import (
    get_fork_agent_executor,
    init_subagent_executor,
)


__all__ = [
    # Context variables
    "set_subagent_parent_session",
    "get_subagent_parent_session",
    "set_current_agent_context",
    "get_current_agent_context",
    "set_current_agent_subagent_id",
    "get_current_agent_subagent_id",
    "_get_llm_trace_session_id_var",
    # Rails
    "ForkMessageInjectionRail",
    "SubagentContextRail",
    # Session proxy
    "SubagentSessionProxy",
    # Executor
    "ForkAgentExecutor",
    # Global instance management
    "get_fork_agent_executor",
    "init_subagent_executor",
]