"""Backward-compatible middleware API shim.

This patch source overlays an older browser runtime implementation onto a newer
`openjiuwen` install where `single_agent.middleware` was renamed to
`single_agent.rail`.  Keep the old import path working so browser runtime code
can run against the current package layout.
"""

from openjiuwen.core.single_agent.rail.base import (
    AgentCallback,
    AgentCallbackContext,
    AgentCallbackEvent,
    AgentRail,
    AnyAgentCallback,
    EVENT_METHOD_MAP,
    EventInputs,
    ForceFinishRequest,
    InvokeInputs,
    ModelCallInputs,
    SyncAgentCallback,
    TaskIterationInputs,
    ToolCallInputs,
    rail,
)


class AgentMiddleware(AgentRail):
    """Legacy alias kept for browser runtime compatibility."""


__all__ = [
    "AgentCallback",
    "AgentCallbackContext",
    "AgentCallbackEvent",
    "AgentMiddleware",
    "AgentRail",
    "AnyAgentCallback",
    "EVENT_METHOD_MAP",
    "EventInputs",
    "ForceFinishRequest",
    "InvokeInputs",
    "ModelCallInputs",
    "SyncAgentCallback",
    "TaskIterationInputs",
    "ToolCallInputs",
    "rail",
]
