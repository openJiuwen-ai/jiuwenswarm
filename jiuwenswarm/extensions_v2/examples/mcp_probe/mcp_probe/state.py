# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session state kept by the MCP Probe example."""

from __future__ import annotations

from dataclasses import dataclass

from openjiuwen.core.events.ops_impl.mcp import (
    McpCallOutput,
    McpConnectOutput,
    McpRegisterValue,
)


@dataclass(slots=True)
class ProbeState:
    """Latest register, connect and call results for one session."""

    connect: McpConnectOutput | None = None
    register: McpRegisterValue | None = None
    register_error: str = ""
    last_call: McpCallOutput | None = None
    last_tool: str = ""
    calls: int = 0
