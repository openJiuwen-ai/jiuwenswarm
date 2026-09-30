"""Compatibility exports for the former Qwen-specific task contract."""
from .task_tools import *  # noqa: F403
from .task_tools import (
    RealtimeToolCall as QwenOmniToolCall,
    parse_realtime_tool_call as parse_qwen_omni_tool_call,
    realtime_tools,
)


def qwen_omni_tools():
    return [{"type": "function", "function": tool} for tool in realtime_tools()]
