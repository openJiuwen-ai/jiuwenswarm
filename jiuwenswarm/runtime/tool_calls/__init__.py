"""Transport-independent contracts for tools requested by duplex backends."""

from .coordinator import ToolCallCoordinator
from .types import ToolCall, ToolCallAdapter, ToolEvent, ToolExecution, ToolExecutionContext, ToolExecutor, ToolResult

__all__ = [
    "ToolCall", "ToolCallAdapter", "ToolCallCoordinator", "ToolEvent", "ToolExecution",
    "ToolExecutionContext", "ToolExecutor", "ToolResult",
]
