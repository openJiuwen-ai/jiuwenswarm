# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Rails for subagent execution.

Provides message injection and context variable management for fork/spawn agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.utils import logger
from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    set_current_agent_context,
    set_current_agent_subagent_id,
    set_subagent_parent_session,
)
from jiuwenclaw.agentserver.deep_agent.rails.stream_event_rail import (
    JiuClawStreamEventRail,
)

if TYPE_CHECKING:
    from openjiuwen.core.session.agent import Session

if TYPE_CHECKING:
    pass


class ForkMessageInjectionRail(DeepAgentRail):
    """Rail that injects fork_messages into agent's context.

    Key insight: ctx.context is None at before_invoke time (context is created
    inside lifecycle after BEFORE_INVOKE triggers). Also, DeepAgent and ReActAgent
    use separate ctx objects, so ctx.extra cannot pass data across agents.

    Solution: Use before_model_call to inject messages when ctx.context is available.
    The fork_messages are stored in self._fork_messages (rail instance field) and
    accessed directly without relying on ctx.extra.

    Message Order Fix:
    - ReActAgent adds UserMessage(query) BEFORE before_model_call triggers
    - ForkMessageInjectionRail injects fork_messages AFTER query is added
    - This results in wrong order: [query, fork_messages]
    - Correct order should be: [fork_messages, query]
    - Fix: Remove last UserMessage, inject fork_messages, re-add the query message

    Note: before_model_call is a BRIDGE_EVENT, registered on ReActAgent where
    the context is actually created and available.
    """

    priority = 5  # Execute before other rails

    def __init__(self, fork_messages: list[Any]) -> None:
        """Initialize with fork messages to inject.

        Args:
            fork_messages: Messages from parent agent to inject into fork agent's context
        """
        super().__init__()
        self._fork_messages = fork_messages
        self._injected = False  # Flag to ensure single injection

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Inject fork_messages into ctx.context with correct ordering."""
        # Only inject once, and only if context is available
        if self._injected or ctx.context is None or not self._fork_messages:
            return

        from openjiuwen.core.foundation.llm import UserMessage

        # Get current messages to check ordering
        messages = ctx.context.get_messages()

        # Find and remove the last UserMessage (the query added by ReActAgent)
        # This is needed to ensure correct ordering: [fork_messages, query]
        last_user_msg = None
        if messages and isinstance(messages[-1], UserMessage):
            last_user_msg = messages[-1]
            # Pop all messages and rebuild without the last UserMessage
            ctx.context.pop_messages(len(messages))
            # Re-add all except the last UserMessage
            for msg in messages[:-1]:
                await ctx.context.add_messages(msg)
            logger.debug(
                f"[ForkInjectionRail] Removed last UserMessage for reordering"
            )

        # Inject fork_messages (historical context from parent agent)
        for msg in self._fork_messages:
            await ctx.context.add_messages(msg)
        logger.info(
            f"[ForkInjectionRail] Injected {len(self._fork_messages)} fork messages into context"
        )

        # Re-add the query UserMessage if we removed it
        if last_user_msg is not None:
            await ctx.context.add_messages(last_user_msg)
            logger.debug(
                f"[ForkInjectionRail] Re-added query UserMessage after fork_messages"
            )

        self._injected = True  # Mark as injected to avoid re-injection


class SubagentContextRail(DeepAgentRail):
    """Minimal rail for spawn/fork agent to set context for nested fork_agent calls.

    This rail handles:
    - Context variable setup for fork_agent inheritance
    - Stream event emission for tool calls (so frontend can see subagent's tool execution)
    - before_tool_call: sets _current_agent_context and _subagent_parent_session, emits tool_call event
    - after_tool_call: clears context variables, emits tool_result event

    Does NOT handle pause/abort (those are handled by parent agent's JiuClawStreamEventRail).

    IMPORTANT: ctx.session is the Session created by Runner.run_agent internally, NOT the
    SubagentSessionProxy we pass to Runner. So we must store parent_session in the rail
    and use it directly for event emission.
    """

    priority = 80

    def __init__(
        self,
        subagent_id: str | None = None,
        parent_session: Session | None = None,
    ) -> None:
        """Initialize with optional subagent_id and parent_session for event forwarding.

        Args:
            subagent_id: The subagent_id of this agent (e.g., "subagent_1222fc63" or "fork_295a9e7")
            parent_session: The parent session to forward events to (NOT ctx.session which is internal)
        """
        super().__init__()
        self._subagent_id = subagent_id
        self._parent_session = parent_session  # Store parent session for event emission

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Set context variables for fork_agent to get correct messages and emit tool_call event."""
        # Set current agent context for fork_agent to get correct messages
        # This ensures fork_agent gets messages from the running context
        # (not from storage which may be empty for subagent scenarios)
        if ctx.context is not None:
            set_current_agent_context(ctx.context)

        # Set subagent_id for nested session_id construction
        if self._subagent_id:
            set_current_agent_subagent_id(self._subagent_id)

        # CRITICAL: Set parent session for nested subagent/fork tools
        # Use self._parent_session (SubagentSessionProxy) NOT ctx.session (Runner's internal Session)
        # This ensures fork_agent created inside spawn can forward events correctly
        if self._parent_session is not None:
            set_subagent_parent_session(self._parent_session)
        elif ctx.session is not None:
            # Fallback: only if no parent_session stored, use ctx.session
            actual_session = getattr(ctx.session, '_parent', ctx.session) if ctx.session else None
            set_subagent_parent_session(actual_session)

        # Emit tool_call event using stored parent_session (NOT ctx.session)
        # ctx.session is Runner's internal session, not our SubagentSessionProxy
        # Reuse JiuClawStreamEventRail's static methods to avoid code duplication
        if self._parent_session is not None and isinstance(ctx.inputs, ToolCallInputs):
            tc = ctx.inputs.tool_call
            await JiuClawStreamEventRail._emit_tool_call(self._parent_session, tc)
            await JiuClawStreamEventRail._emit_tool_update(self._parent_session, tc, status="in_progress")

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Clear context after tool execution and emit tool_result event."""
        # Emit tool_result event using stored parent_session
        # Reuse JiuClawStreamEventRail's static method
        if self._parent_session is not None and isinstance(ctx.inputs, ToolCallInputs):
            tc = ctx.inputs.tool_call
            result = ctx.inputs.tool_result
            await JiuClawStreamEventRail._emit_tool_result(self._parent_session, tc, result)

        # Clear context variables
        set_current_agent_context(None)
        set_current_agent_subagent_id(None)
        set_subagent_parent_session(None)

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        """Clear context on exception."""
        set_current_agent_context(None)
        set_current_agent_subagent_id(None)
        set_subagent_parent_session(None)