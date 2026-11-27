# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Fork Agent and Spawn Agent tools - Create subagents for task execution."""

from __future__ import annotations

import asyncio
from typing import Any

from openjiuwen.core.foundation.llm import AssistantMessage, SystemMessage
from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.session.agent import Session

from jiuwenclaw.agentserver.tools.subagent_executor import (
    ForkAgentExecutor,
    get_subagent_parent_session,
    get_current_agent_context,
    get_fork_agent_executor,
)
from jiuwenclaw.agentserver.tools.subagent_models import (
    ForkAgentTaskSpec,
    SubagentTaskSpec,
)
from jiuwenclaw.utils import logger


@tool(
    name="fork_agent",
    description=(
        "Create fork subAgent inheriting parent Agent's context. **Do NOT use by default** unless "
        "you need shared context (e.g., parallel tasks with consistent document understanding). "
        "Use spawn_subagent (isolated) for normal tasks. Fork inherits parent history which may "
        "contaminate subAgent reasoning."
    ),
)
async def fork_agent(
    objective: str,
    prompt: str = "",
) -> dict[str, Any]:
    """
    Create fork subAgent inheriting parent Agent's message history (shared context).

    **Default choice: spawn_subagent. Use fork_agent only when you need shared context understanding.**

    Key features:
    - Inherits parent's messages (KVCache reuse for efficiency)
    - All fork agents share the same document understanding (consistent style)
    - Model decides execution: serial (one by one) or parallel (batch)

    Args:
        objective: Task objective (what to accomplish)
        prompt: Execution prompt (optional, detailed instructions)

    Returns:
        {"success": bool, "task_id": str, "role_id": str, "result": str, "error": str, "usage": dict}
    """
    executor = get_fork_agent_executor()
    if executor is None:
        return {"success": False, "error": "Fork agent tools not initialized"}

    parent_session: Session | None = get_subagent_parent_session()

    # Get fork messages from parent agent's context
    fork_messages = await get_fork_messages()

    task = ForkAgentTaskSpec(
        objective=objective,
        prompt=prompt,
    )

    result = await executor.execute_fork(
        task, fork_messages=fork_messages, parent_session=parent_session
    )

    return result.model_dump()


@tool(
    name="spawn_subagent",
    description=(
        "Spawn a subagent to execute a task with isolated context. **Default choice** for "
        "most tasks. The subagent has full Agent capabilities: multi-round reasoning, tool calls, "
        "skill loading. Use fork_agent only when you need shared context (parallel tasks with "
        "consistent document understanding)."
    ),
)
async def spawn_subagent(
    objective: str,
    role_id: str = "MainAgent",
    prompt: str = "",
) -> dict[str, Any]:
    """
    Spawn a subagent to execute a task, blocking until result is returned.

    The subagent has full Agent capabilities: multi-round reasoning, tool calls, skill loading.
    Streaming events are forwarded to the parent session with 'subagent.' prefix.

    **Key feature**: Isolated context (no message history inheritance). This is the default choice.
    Use fork_agent only when you need shared context understanding (e.g., parallel document processing).

    **Nested scenario**: spawn_subagent can call fork_agent inside. The fork_agent will inherit
    spawn's context (not main agent's), enabling consistent context within spawn's task scope.

    Args:
        objective: Task objective description
        role_id: Role ID to use (default: MainAgent)
        prompt: Execution prompt (optional)

    Returns:
        {"success": bool, "task_id": str, "role_id": str, "result": str, "error": str, "usage": dict}
    """
    executor = get_fork_agent_executor()
    if executor is None:
        return {"success": False, "error": "Subagent tools not initialized"}

    parent_session: Session | None = get_subagent_parent_session()

    task = SubagentTaskSpec(
        role_id=role_id,
        objective=objective,
        prompt=prompt,
    )

    result = await executor.execute_spawn(
        task, parent_session=parent_session
    )

    return result.model_dump()


async def get_fork_messages() -> list[Any]:
    """
    Get messages from agent's current context for fork.

    Returns messages excluding SystemMessage (to avoid duplication with invoke's system prompt).
    Also excludes the last AssistantMessage with tool_calls if it exists, because that
    represents the current pending tool execution (fork_agent call itself) which has
    no corresponding ToolMessage yet.

    Priority:
    1. Use current context from context variable (correct for nested scenarios)
    2. Fallback to empty list (fork agent will have minimal context)

    Returns:
        List of messages from agent's context (excluding SystemMessage and pending tool calls)
    """
    # Priority 1: Use current context from context variable
    # This is the correct way to get messages from the running context
    current_context = get_current_agent_context()
    if current_context is not None:
        try:
            context_window = await current_context.get_context_window(
                system_messages=[], tools=None
            )
            messages = context_window.get_messages()

            # Filter out SystemMessage
            filtered_messages = [m for m in messages if not isinstance(m, SystemMessage)]

            # Remove the last AssistantMessage if it has tool_calls
            # This is the current pending fork_agent call which has no ToolMessage yet
            # Including it would cause _fix_incomplete_tool_context to add placeholders
            if filtered_messages and isinstance(filtered_messages[-1], AssistantMessage):
                last_msg = filtered_messages[-1]
                if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                    # Check if this AssistantMessage has pending tool calls (no corresponding ToolMessage)
                    # by looking for tool_calls that aren't followed by ToolMessages
                    tool_call_ids = {getattr(tc, 'id', '') for tc in last_msg.tool_calls}
                    # Check if there are ToolMessages after this AssistantMessage
                    has_tool_messages_after = any(
                        hasattr(m, 'tool_call_id') and getattr(m, 'tool_call_id', '') in tool_call_ids
                        for m in filtered_messages[-1:]  # Only check messages after last AssistantMessage
                    )
                    if not has_tool_messages_after:
                        # Remove the pending AssistantMessage with tool_calls
                        filtered_messages = filtered_messages[:-1]
                        logger.debug(
                            f"[ForkAgent] Removed last AssistantMessage with pending tool_calls "
                            f"(had {len(last_msg.tool_calls)} calls)"
                        )

            logger.info(
                f"[ForkAgent] Got {len(filtered_messages)} messages from current context "
                f"(filtered from {len(messages)} total)"
            )
            return filtered_messages
        except Exception as e:
            logger.warning(f"[ForkAgent] Failed to get messages from current context: {e}")

    # Priority 2: Fallback to empty list
    # This happens when fork_agent is called outside of a running context
    logger.warning("[ForkAgent] Cannot get fork messages: no current context available")
    return []