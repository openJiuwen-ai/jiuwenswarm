# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``_finalize_cli_auth`` post-auth side-effects (form C).

After CLI OAuth completes, ``_await_cli_auth`` calls ``_finalize_cli_auth``
which syncs MCP tokens into os.environ and promotes state connecting→connected.
The MCP is NOT loaded into the agent here — session-level enable is driven by
chat.send's ``mcp`` field. This pins the state contract:

* finalize → state promoted connecting→connected, returns ``connected``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _services():
    """业务函数只需要当前 Agent 管理器，用来同步凭证。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.sync_mcp_credentials = MagicMock(return_value=True)
    return SimpleNamespace(agent_manager=manager)


@pytest.mark.asyncio
async def test_finalize_cli_auth_promotes_to_connected() -> None:
    """finalize promotes state connecting→connected and returns the
    ``connected`` payload. No apply_mcp_change — the MCP is loaded into the
    agent only when a chat turn selects it via the ``mcp`` field."""
    from jiuwenswarm.server.handlers.mcp_hub import _finalize_cli_auth

    set_state_calls: list[tuple[str, str]] = []

    def fake_set_state(name, *, state):
        set_state_calls.append((name, state))

    with patch(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        side_effect=fake_set_state,
    ):
        result = await _finalize_cli_auth(_services(), "cloudbase", {
            "name": "cloudbase", "integration_type": "cli",
            "auth_required": False, "installed_skills": ["cloudbase"],
        })

    assert result["type"] == "connected"
    assert result["name"] == "cloudbase"
    # promoted connecting → connected
    assert ("cloudbase", "connected") in set_state_calls

