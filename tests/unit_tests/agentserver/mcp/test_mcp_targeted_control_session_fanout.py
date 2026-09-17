# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: phase-2 targeted MCP control must fan out to session-scoped child
adapters.

MCP tools actually live in session-scoped child adapters (each session has its
own _registered_mcp_servers). register_mcp_by_name / unregister_mcp_by_name on
the parent must propagate to every live session child, otherwise disconnect
leaves the child's MCP registered and the agent can still call those tools.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_adapter(*, session_scoped: bool):
    """Build a bare adapter with the minimal attrs the targeted-control
    methods touch. session_scoped=True emulates a session child."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    from openjiuwen.core.foundation.tool import McpServerConfig

    a = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    a._instance = MagicMock()
    a._is_session_scoped_adapter = session_scoped
    a._session_adapters = {}
    a._registered_mcp_server_ids = set()
    a._registered_mcp_servers = {}
    return a


def _cfg(name: str, sid: str):
    from openjiuwen.core.foundation.tool import McpServerConfig
    return McpServerConfig(server_id=sid, server_name=name,
                          server_path="https://x", client_type="sse")


@pytest.mark.anyio
async def test_unregister_propagates_to_session_children() -> None:
    """Parent + 2 session children all hold the MCP; unregister on parent
    must invoke _unregister_mcp_server on all three."""
    parent = _make_adapter(session_scoped=False)
    child1 = _make_adapter(session_scoped=True)
    child2 = _make_adapter(session_scoped=True)
    parent._session_adapters = {"s1": child1, "s2": child2}

    for a in (parent, child1, child2):
        a._registered_mcp_server_ids = {"sid"}
        a._registered_mcp_servers = {"sid": _cfg("baidu", "sid")}
        a._unregister_mcp_server = AsyncMock()

    ok = await parent.unregister_mcp_by_name("baidu")

    assert ok is True
    parent._unregister_mcp_server.assert_awaited_once_with("sid")
    child1._unregister_mcp_server.assert_awaited_once_with("sid")
    child2._unregister_mcp_server.assert_awaited_once_with("sid")


@pytest.mark.anyio
async def test_unregister_skips_session_fanout_when_session_scoped() -> None:
    """A session child must only unregister its own MCP, never recurse."""
    child = _make_adapter(session_scoped=True)
    child._registered_mcp_server_ids = {"sid"}
    child._registered_mcp_servers = {"sid": _cfg("baidu", "sid")}
    child._unregister_mcp_server = AsyncMock()

    ok = await child.unregister_mcp_by_name("baidu")

    assert ok is True
    child._unregister_mcp_server.assert_awaited_once_with("sid")


@pytest.mark.anyio
async def test_unregister_when_no_session_children_still_works() -> None:
    """Parent with no live session children still clears its own MCP."""
    parent = _make_adapter(session_scoped=False)
    parent._registered_mcp_server_ids = {"sid"}
    parent._registered_mcp_servers = {"sid": _cfg("baidu", "sid")}
    parent._unregister_mcp_server = AsyncMock()

    ok = await parent.unregister_mcp_by_name("baidu")

    assert ok is True
    parent._unregister_mcp_server.assert_awaited_once_with("sid")


@pytest.mark.anyio
async def test_unregister_when_not_registered_anywhere_returns_false() -> None:
    """Nothing registered under the name → parent returns False, no session
    child touched."""
    parent = _make_adapter(session_scoped=False)
    child1 = _make_adapter(session_scoped=True)
    parent._session_adapters = {"s1": child1}
    parent._unregister_mcp_server = AsyncMock()
    child1._unregister_mcp_server = AsyncMock()

    ok = await parent.unregister_mcp_by_name("baidu")
    assert ok is False
    parent._unregister_mcp_server.assert_not_awaited()
    child1._unregister_mcp_server.assert_not_awaited()


@pytest.mark.anyio
async def test_register_propagates_to_session_children() -> None:
    """register on parent must also register in every live session child
    (so a freshly connected MCP is visible to all sessions)."""
    from openjiuwen.core.foundation.tool import McpServerConfig
    parent = _make_adapter(session_scoped=False)
    child1 = _make_adapter(session_scoped=True)
    child2 = _make_adapter(session_scoped=True)
    parent._session_adapters = {"s1": child1, "s2": child2}

    fake_entry = {"name": "baidu", "transport": "sse", "url": "https://x",
                  "enabled": True, "server_id_scope": "mcp:baidu"}

    for a in (parent, child1, child2):
        a._build_mcp_server_config = MagicMock(
            return_value=McpServerConfig(server_name="baidu",
                                        server_path="https://x", client_type="sse"))
        a._register_mcp_server = AsyncMock(return_value=True)

    with patch("jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_mcp_server_config",
               return_value=fake_entry):
        ok = await parent.register_mcp_by_name("baidu")

    assert ok is True
    parent._register_mcp_server.assert_awaited_once()
    child1._register_mcp_server.assert_awaited_once()
    child2._register_mcp_server.assert_awaited_once()
