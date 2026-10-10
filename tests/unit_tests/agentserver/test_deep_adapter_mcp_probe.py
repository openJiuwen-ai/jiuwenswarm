# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``mcp_config.probe_mcp_live_connection``.

The probe is the connect-time live-connect gate added so a marketplace MCP is
actually reachable (stdio spawns + handshake / remote HTTP connects) before
``connected`` is reported. These tests cover the short-circuit paths that
don't need a real ``Runner.resource_mgr``:

* skill-only / pure-CLI MCPs have no server entry → ``(True, "")`` (nothing
  to probe; they surface via bundled skills).
* stdio MCP whose ``command`` is not on PATH → ``(False, reason)`` before any
  spawn is attempted.
* empty name → ``(False, ...)`` before touching config/Runner.

Plus one test confirming ``AgentManager.probe_mcp_live_connection`` is a thin
delegate to the shared function (so cold-start — no live agent — still probes
via the process-level resource manager).

The full spawn/handshake path lives in the integration layer (openjiuwen's
``add_tool_server``); a unit test cannot exercise it without a live MCP.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwenswarm.common.mcp_config import probe_mcp_live_connection
from jiuwenswarm.server.runtime.agent_manager import AgentManager


@pytest.mark.asyncio
async def test_probe_skill_only_mcp_returns_ok() -> None:
    """A skill-only / pure-CLI MCP has no server entry (``get_mcp_server_config``
    returns None) — there is no MCP server to connect to, so the probe is a
    no-op success. The MCP surfaces via its bundled skills, not the server
    registry."""
    with patch(
        "jiuwenswarm.common.mcp_config.get_mcp_server_config",
        return_value=None,
    ):
        ok, reason = await probe_mcp_live_connection("feishu-cli")
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
async def test_probe_stdio_missing_command_returns_failure() -> None:
    """A stdio MCP whose ``command`` is not on PATH fails fast at the probe —
    no spawn is attempted. This is the gate that surfaces "npx not installed"
    at connect time instead of degrading to "no tools" on the first chat
    message."""
    entry = {
        "name": "context7",
        "transport": "stdio",
        "command": "definitely-not-a-real-binary-xyz",
        "args": [],
    }
    with patch(
        "jiuwenswarm.common.mcp_config.get_mcp_server_config",
        return_value=entry,
    ), patch(
        "jiuwenswarm.common.mcp_config.preflight_mcp_server_reachable",
        return_value=(True, ""),
    ):
        ok, reason = await probe_mcp_live_connection("context7")
    assert ok is False
    assert "not found on PATH" in reason


@pytest.mark.asyncio
async def test_probe_empty_name_returns_failure() -> None:
    """Empty/whitespace name → (False, ...) without touching config/Runner."""
    ok, reason = await probe_mcp_live_connection("   ")
    assert ok is False
    assert "required" in reason


@pytest.mark.asyncio
async def test_agent_manager_probe_delegates_to_shared_function() -> None:
    """``AgentManager.probe_mcp_live_connection`` is a thin delegate to the
    shared ``mcp_config.probe_mcp_live_connection``. That function talks to
    the process-level ``Runner.resource_mgr`` directly (no adapter instance),
    so cold-start — no conversation, ``agents`` empty — still probes and
    caches the connection for the first chat turn to reuse. This replaces the
    old fan-out-to-adapters design that returned ``(False, "no live agent")``
    when no adapter existed yet."""
    am = object.__new__(AgentManager)
    am.agents = {}  # cold start — no live agent
    with patch(
        "jiuwenswarm.common.mcp_config.probe_mcp_live_connection",
        return_value=(True, ""),
    ) as mock_probe:
        ok, reason = await am.probe_mcp_live_connection("github")  # type: ignore[misc]
    assert ok is True
    assert reason == ""
    mock_probe.assert_awaited_once_with("github")
