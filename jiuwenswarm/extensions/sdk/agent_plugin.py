"""Services available to agent plugins loaded by an extension."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class AgentPluginServices:
    agent: Any
    load_plugin_spec: Callable[[Any], Awaitable[Any | None]]
    register_request_context: Callable[[Callable[[Any], Any]], None]
    register_runtime_tools: Callable[[Any], None]
    runtime_context: Any
