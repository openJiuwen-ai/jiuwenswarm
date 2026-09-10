"""Private AgentServer-to-Gateway method names for A2A outbound tools."""

A2A_TOOL_FIND_AGENTS = "a2a.outbound.tool.find_agents"
A2A_TOOL_DISPATCH_TASK = "a2a.outbound.tool.dispatch_task"
A2A_TOOL_GET_DISPATCH = "a2a.outbound.tool.get_dispatch"
A2A_TOOL_CANCEL_CALL = "a2a.outbound.tool.cancel_call"
A2A_TOOL_METHODS = frozenset(
    {
        A2A_TOOL_FIND_AGENTS,
        A2A_TOOL_DISPATCH_TASK,
        A2A_TOOL_GET_DISPATCH,
        A2A_TOOL_CANCEL_CALL,
    }
)

__all__ = [
    "A2A_TOOL_DISPATCH_TASK",
    "A2A_TOOL_CANCEL_CALL",
    "A2A_TOOL_FIND_AGENTS",
    "A2A_TOOL_GET_DISPATCH",
    "A2A_TOOL_METHODS",
]
