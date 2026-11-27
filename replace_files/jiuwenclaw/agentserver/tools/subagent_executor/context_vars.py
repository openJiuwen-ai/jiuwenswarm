# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Context variables for subagent execution.

Provides context isolation and state passing between parent agent and subagents.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session
    from openjiuwen.core.context_engine.base import ModelContext


# Context variable to pass parent session from tool execution to executor
_subagent_parent_session: ContextVar[Optional["Session"]] = ContextVar(
    "subagent_parent_session", default=None
)

# Context variable to pass current agent's context for fork context retrieval
# This stores the actual ModelContext object (not agent) to get current messages
_current_agent_context: ContextVar[Optional["ModelContext"]] = ContextVar(
    "current_agent_context", default=None
)

# Context variable to store current agent's subagent_id (for nested session_id)
# Used when subagent creates fork_agent: fork's session_id includes parent subagent's id
# Format: "subagent_1222fc63" or "fork_295a9e7" (the suffix part of session_id)
_current_agent_subagent_id: ContextVar[Optional[str]] = ContextVar(
    "current_agent_subagent_id", default=None
)

# Per-request workspace dir (same resolution as RuntimePromptRail: metadata
# effective_project_dir or adapter default). Used by fork/spawn so subagent
# prompts and Workspace match the main agent for the current request.
_effective_request_workspace_dir: ContextVar[Optional[str]] = ContextVar(
    "effective_request_workspace_dir", default=None
)


def set_subagent_parent_session(session: Optional["Session"]) -> None:
    """Set the parent session context for subagent execution."""
    _subagent_parent_session.set(session)


def get_subagent_parent_session() -> Optional["Session"]:
    """Get the parent session from context."""
    return _subagent_parent_session.get()


def set_current_agent_context(context: Optional["ModelContext"]) -> None:
    """Set the current agent's context for fork context retrieval.

    This stores the actual ModelContext object, so fork_agent can get messages
    directly from the running context (not from storage which may be empty for subagent).

    Args:
        context: ModelContext object from agent's invoke loop
    """
    _current_agent_context.set(context)


def get_current_agent_context() -> Optional["ModelContext"]:
    """Get the current agent's context from context variable.

    Returns the ModelContext that is currently in use (main agent or spawn subagent).
    Used by fork_agent to get fork messages from the correct running context.
    """
    return _current_agent_context.get()


def set_current_agent_subagent_id(subagent_id: Optional[str]) -> None:
    """Set the current agent's subagent_id for nested session_id construction.

    This stores the suffix part of session_id (e.g., "subagent_1222fc63" or "fork_295a9e7").
    Used when subagent creates fork_agent to construct nested session_id.

    Args:
        subagent_id: The subagent_id of the current agent (suffix of session_id)
    """
    _current_agent_subagent_id.set(subagent_id)


def get_current_agent_subagent_id() -> Optional[str]:
    """Get the current agent's subagent_id from context variable.

    Returns the suffix part of session_id for the current agent.
    Used to construct nested session_id when creating fork from subagent.
    """
    return _current_agent_subagent_id.get()


def set_effective_request_workspace_dir(workspace_dir: Optional[str]) -> None:
    """Store workspace for the current request (aligned with RuntimePromptRail)."""
    _effective_request_workspace_dir.set(workspace_dir)


def get_effective_request_workspace_dir() -> Optional[str]:
    """Workspace dir for the current request, or None if not set."""
    return _effective_request_workspace_dir.get()


def _get_llm_trace_session_id_var() -> ContextVar[str]:
    """Get LLM trace session ID context var (lazy import to avoid circular dependency)."""
    from jiuwenclaw.agentserver.deep_agent.interface_deep import _LLM_TRACE_SESSION_ID
    return _LLM_TRACE_SESSION_ID