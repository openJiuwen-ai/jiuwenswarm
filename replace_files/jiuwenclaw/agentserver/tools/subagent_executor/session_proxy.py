# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Session proxy for subagent event forwarding.

Provides event filtering and forwarding to parent session for proper frontend display.
"""

from __future__ import annotations

from typing import Any, Union

from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream.base import OutputSchema

from jiuwenclaw.utils import logger


class SubagentSessionProxy:
    """Session proxy that forwards execution events to parent session.

    Forward tool calls and thinking process, suppress user-facing messages.
    Fork agent's tool execution is shown alongside main Agent's fork_agent call info.

    Supports nested session_id for trace hierarchy:
    - Main agent: sess_xxx
    - Subagent from main: sess_xxx_subagent_1222fc63
    - Fork from subagent: sess_xxx_subagent_1222fc63_fork_295a9e7
    """

    # Event types to forward (tool execution + thinking process + permission requests)
    # Include tool_update for showing tool execution status (in_progress, etc.)
    FORWARD_TYPES = {
        "tool_call", "tool_result", "tool_update",
        "thinking", "llm_reasoning",
        "retry_notification", "chat.ask_user_question",
        "context.compressed",  # Context compression info
    }
    # Event types to suppress (user-facing messages only)
    SUPPRESS_TYPES = {"answer", "complete", "start"}

    def __init__(
        self,
        parent_session: Session,
        subagent_id: str,
        role_id: str,
    ) -> None:
        self._parent = parent_session
        self._subagent_id = subagent_id
        self._role_id = role_id
        # Construct nested session_id for trace hierarchy
        # Simply append subagent_id to parent_session's session_id
        # Works for both direct and nested scenarios
        self._session_id = f"{parent_session.get_session_id()}_{subagent_id}"

    async def write_stream(self, data: Union[dict, OutputSchema]) -> None:
        """Forward tool execution events, suppress user-facing messages."""
        event_type = None
        output_data = None

        if isinstance(data, OutputSchema):
            event_type = data.type
            output_data = data
        elif isinstance(data, dict):
            event_type = data.get("type", "unknown")
            output_data = OutputSchema(
                type=event_type,
                index=data.get("index", 0),
                payload=data.get("payload", {}),
            )

        # Only forward tool execution events
        if event_type in self.FORWARD_TYPES:
            await self._parent.write_stream(output_data)
        elif event_type in self.SUPPRESS_TYPES:
            logger.debug(f"[SubagentSession] Suppressed event: {event_type}")
        else:
            # Unknown event type - forward by default for debugging
            logger.debug(f"[SubagentSession] Forwarding unknown event: {event_type}")
            await self._parent.write_stream(output_data)

    def get_session_id(self) -> str:
        """Return composite session ID."""
        return self._session_id

    def get_env(self, key: str, default: Any = None) -> Any:
        """Proxy to parent session."""
        return self._parent.get_env(key, default)

    def get_envs(self) -> dict:
        """Proxy to parent session."""
        return self._parent.get_envs()

    def update_state(self, data: dict) -> None:
        """Proxy to parent session."""
        return self._parent.update_state(data)

    def get_state(self, key: Union[str, list, dict] = None) -> Any:
        """Proxy to parent session."""
        return self._parent.get_state(key)

    async def write_custom_stream(self, data: dict) -> None:
        """Forward custom stream (typically not user-facing, pass through)."""
        await self._parent.write_custom_stream(data)

    def __getattr__(self, name: str) -> Any:
        """Fallback: proxy any other attributes to parent session."""
        return getattr(self._parent, name)

    def get_parent_session(self) -> Session:
        """Return the underlying parent session.

        Useful when tools need the actual session (not the proxy).
        """
        return self._parent