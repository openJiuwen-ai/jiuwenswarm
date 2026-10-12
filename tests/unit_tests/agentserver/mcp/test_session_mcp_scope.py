# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: MCP init/reload load strategy branches by channel, not scope.

The TUI channel (root + session children) registers its global-default set
(config.yaml ``enabled`` ∪ state.json ``enabled=True``) on init and reload —
TUI loads MCPs directly from config, this is the original behavior. The web
channel loads NOTHING on init/reload: web is session-level, its MCPs come
solely from reconcile_session_mcp (chat.send's ``mcp`` field; None and [] both
mean "no MCP this turn"). This guards web's default-False contract without
breaking the TUI's config-driven load.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from openjiuwen.core.foundation.tool import McpServerConfig


def _mk_cfg(name: str) -> McpServerConfig:
    return McpServerConfig(server_id=f"sid:{name}", server_name=name,
                           server_path="http://x", client_type="sse")


def _make_adapter(*, channel_id: str = "", session_scoped: bool = False):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    a = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    a._is_session_scoped_adapter = session_scoped
    a._channel_id = channel_id
    a._instance = MagicMock()
    a._registered_mcp_server_ids = set()
    a._registered_mcp_servers = {}
    a._session_selected_mcp = set()
    return a


def test_sync_mcp_skips_global_set_for_web_channel() -> None:
    """A web-channel adapter (root or session child) must not consult
    config.yaml/state.json at all — web's desired set is only the session's
    _session_selected_mcp, so a reload never pulls the global set into a web
    session."""
    adapter = _make_adapter(channel_id="web", session_scoped=True)
    adapter._yaml_enabled_mcp_entries = MagicMock(
        side_effect=AssertionError("web channel must not read config.yaml"))
    adapter._state_enabled_mcp_entries = MagicMock(
        side_effect=AssertionError("web channel must not read state.json"))
    adapter._build_mcp_server_config = MagicMock(
        side_effect=AssertionError("web channel has empty selection — "
                                   "must build nothing"))
    adapter._register_mcp_server = AsyncMock()

    asyncio.new_event_loop().run_until_complete(
        adapter._sync_mcp_servers_for_runtime({"mcp": {"servers": [
            {"name": "tui-only", "enabled": True}]}}))
    # If we got here without AssertionError, the global-set readers and builder
    # were never called — exactly the web-channel contract.
    adapter._register_mcp_server.assert_not_awaited()


def test_sync_mcp_registers_global_set_for_tui_channel() -> None:
    """A TUI-channel adapter registers the global-default set (config.yaml) on
    reload — TUI loads MCPs directly from config, the original behavior."""
    adapter = _make_adapter(channel_id="tui", session_scoped=False)
    built: list[str] = []
    adapter._yaml_enabled_mcp_entries = MagicMock(
        return_value=[{"name": "tui-mcp", "transport": "sse",
                        "url": "http://x"}])
    adapter._state_enabled_mcp_entries = MagicMock(return_value=[])
    adapter._build_mcp_server_config = MagicMock(
        side_effect=lambda e: built.append(e.get("name")) or _mk_cfg(e.get("name")))
    adapter._register_mcp_server = AsyncMock(return_value=True)

    asyncio.new_event_loop().run_until_complete(
        adapter._sync_mcp_servers_for_runtime({}))
    assert "tui-mcp" in built
    adapter._register_mcp_server.assert_awaited()


def test_sync_mcp_registers_global_set_for_tui_session_child() -> None:
    """A TUI session-scoped child ALSO registers the global set — this is the
    TUI's original behavior that must not regress (TUI chat runs on session
    children, which load config directly)."""
    adapter = _make_adapter(channel_id="tui", session_scoped=True)
    built: list[str] = []
    adapter._yaml_enabled_mcp_entries = MagicMock(
        return_value=[{"name": "tui-mcp", "transport": "sse",
                        "url": "http://x"}])
    adapter._state_enabled_mcp_entries = MagicMock(return_value=[])
    adapter._build_mcp_server_config = MagicMock(
        side_effect=lambda e: built.append(e.get("name")) or _mk_cfg(e.get("name")))
    adapter._register_mcp_server = AsyncMock(return_value=True)

    asyncio.new_event_loop().run_until_complete(
        adapter._sync_mcp_servers_for_runtime({}))
    assert "tui-mcp" in built
    adapter._register_mcp_server.assert_awaited()
