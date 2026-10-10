# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the CLI auth poller (``_await_cli_auth``).

After ``connect_mcp`` returns an ``auth_required`` sentinel (the CLI process
already opened the browser), :meth:`_handle_mcp_connect` calls
``_await_cli_auth`` which holds the RPC open — the frontend shows a
"connecting…" spinner — and loops ``complete_cli_auth`` until the user
finishes OAuth, then returns a single ``connected`` (or ``auth_failed`` on
timeout/error) dict for the handler to send as the RPC response.

These tests pin that contract. ``max_attempts``/``delay`` are injected as
tiny values so the loop resolves in milliseconds (no real sleep, no 10-min
wait); ``complete_cli_auth`` is mocked so no real CLI runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


def _pending_item(name: str = "feishu") -> dict[str, Any]:
    """complete_cli_auth result while the user hasn't finished in browser."""
    return {
        "name": name,
        "integration_type": "cli",
        "auth_required": True,
        "auth_pending": True,
        "step_index": 0,
        "output": "auth process still running",
    }


def _connected_item(name: str = "feishu") -> dict[str, Any]:
    """complete_cli_auth result after OAuth completed."""
    return {
        "name": name,
        "integration_type": "cli",
        "auth_required": False,
        "installed_skills": ["feishu"],
    }


def _new_server():
    """A bare AgentWebSocketServer with _await_cli_auth's deps mocked out.

    We only assert the returned contract (connected / auth_failed), so
    _finalize_cli_auth (post-auth side-effects) is stubbed.
    """
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._finalize_cli_auth = AsyncMock(return_value={
        "type": "connected", "name": "feishu", "applied": True,
        "item": _connected_item(),
    })
    server._mask_sensitive_fields = lambda d: d
    return server


@pytest.mark.anyio
async def test_await_returns_connected_after_auth_completes() -> None:
    """Pending → pending → connected: returns the connected payload once."""
    server = _new_server()

    results = [_pending_item(), _pending_item(), _connected_item()]
    with patch(
        "jiuwenswarm.server.runtime.mcp.registry.complete_cli_auth",
        side_effect=results,
    ):
        result = await server._await_cli_auth(
            "feishu", 0, max_attempts=10, delay=0,
        )

    assert result["type"] == "connected"
    assert result["name"] == "feishu"
    assert result["applied"] is True


@pytest.mark.anyio
async def test_await_returns_auth_failed_on_timeout() -> None:
    """If every attempt stays pending, returns auth_failed (no crash, no hang)."""
    server = _new_server()
    server._finalize_cli_auth = AsyncMock()  # must not run on timeout

    with patch(
        "jiuwenswarm.server.runtime.mcp.registry.complete_cli_auth",
        return_value=_pending_item(),
    ):
        result = await server._await_cli_auth(
            "feishu", 0, max_attempts=3, delay=0,
        )

    server._finalize_cli_auth.assert_not_called()
    assert result["type"] == "auth_failed"
    assert result["name"] == "feishu"
    assert "timed out" in str(result["error"]).lower()


@pytest.mark.anyio
async def test_await_returns_auth_failed_on_exception() -> None:
    """If complete_cli_auth raises, returns auth_failed (not crash)."""
    server = _new_server()
    server._finalize_cli_auth = AsyncMock()

    with patch(
        "jiuwenswarm.server.runtime.mcp.registry.complete_cli_auth",
        side_effect=RuntimeError("cli crashed"),
    ):
        result = await server._await_cli_auth(
            "feishu", 0, max_attempts=10, delay=0,
        )

    server._finalize_cli_auth.assert_not_called()
    assert result["type"] == "auth_failed"
    assert "cli crashed" in str(result["error"])


@pytest.mark.anyio
async def test_await_multi_step_advances_step_index_then_connected() -> None:
    """Multi-step CLI: step 0 done → step 1 needs user → step 1 done → connected.

    ``complete_cli_auth`` carries the authoritative ``step_index`` for the
    *next* poll when a step completes and the next needs user action. The
    poller must adopt it as ``cur_step`` so subsequent polls query the new
    step — otherwise it re-queries step 0 forever (a real dead-loop on
    multi-step CLIs).
    """
    server = _new_server()
    calls: list[int] = []

    def fake_complete_cli_auth(name: str, step_index: int, **kw: Any) -> dict[str, Any]:
        calls.append(step_index)
        if step_index == 0:
            if len(calls) == 1:
                return _pending_item()  # step 0 still pending
            # step 0 done → advanced to step 1, which needs user action.
            return {
                "name": name, "integration_type": "cli",
                "auth_required": True, "step_index": 1, "steps_total": 2,
                "auth_url": "https://accounts.feishu.cn/login?step=2",
                "auth_domain": "accounts.feishu.cn",
            }
        # step_index == 1
        if len(calls) == 3:
            return _pending_item()  # step 1 still pending
        return _connected_item()  # step 1 done → connected

    with patch(
        "jiuwenswarm.server.runtime.mcp.registry.complete_cli_auth",
        side_effect=fake_complete_cli_auth,
    ):
        result = await server._await_cli_auth(
            "feishu", 0, max_attempts=10, delay=0,
        )

    # step_index sequence must advance: 0, 0, 1, 1 (NOT 0,0,0,0 — the
    # dead-loop signature). Call 2 returns step_index=1, which the poller
    # must adopt for call 3.
    assert calls == [0, 0, 1, 1], f"step_index did not advance: {calls}"
    assert result["type"] == "connected"
