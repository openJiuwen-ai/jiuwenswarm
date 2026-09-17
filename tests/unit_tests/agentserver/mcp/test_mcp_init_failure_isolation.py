# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: init per-MCP failure isolation + connecting→connected promotion.

``_register_mcp_servers_from_config`` (root agent init) registers the TUI
global-default set: config.yaml ``enabled`` mcp.servers ∪ state.json
``enabled=True`` records. These tests exercise the config.yaml path only, so
an autouse fixture stubs ``_state_enabled_mcp_entries`` to ``[]`` and isolates
state.json. Two guarantees:

1. **Failure isolation** — one bad MCP (unreachable / register rejected) must
   not abort the loop and starve the remaining MCPs. The failure is logged,
   the bad MCP's state degrades to ``disconnected`` (so the next restart
   doesn't retry it forever), and the loop continues.

2. **connecting → connected promotion** — a config.yaml MCP whose state.json
   record is ``connecting`` (left over from a connect interrupted by a restart)
   that registers OK here is promoted to connected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_json(monkeypatch):
    """These tests exercise the config.yaml path of init; stub the state.json
    enabled-set reader to [] so a real workspace state.json doesn't inject
    extra MCPs into the init loop."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter, "_state_enabled_mcp_entries", staticmethod(lambda: [])
    )



def _mk_cfg(name: str):
    """A minimal McpServerConfig-like object for the init loop."""
    return SimpleNamespace(
        server_name=name,
        client_type="sse",
        server_path=f"http://{name}.example/mcp",
    )


def _new_adapter():
    """A bare JiuWenSwarmDeepAdapter with init-loop deps mocked."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = MagicMock()  # truthy so _register_mcp_server proceeds
    adapter._registered_mcp_server_ids = set()
    adapter._registered_mcp_servers = {}
    adapter._session_selected_mcp = set()
    return adapter


@pytest.mark.asyncio
async def test_init_one_mcp_failure_does_not_starve_the_rest(tmp_path, monkeypatch) -> None:
    """MCP-A register raises → degrade A to disconnected; MCP-B still registers."""
    adapter = _new_adapter()

    calls: list[str] = []

    def fake_build(self, entry):
        return _mk_cfg(str(entry.get("name", "")))

    async def fake_register(self, cfg, *, tag):
        calls.append(cfg.server_name)
        if cfg.server_name == "bad":
            raise RuntimeError("bad host unreachable")
        return True

    monkeypatch.setattr(type(adapter), "_build_mcp_server_config", fake_build)
    monkeypatch.setattr(type(adapter), "_register_mcp_server", fake_register)

    degraded: list[tuple[str, str]] = []

    def fake_set_state(name, *, state):
        degraded.append((name, state))

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config_yaml_mcp_servers",
        return_value=[
            {"name": "bad", "transport": "sse", "url": "http://bad/mcp", "enabled": True},
            {"name": "good", "transport": "sse", "url": "http://good/mcp", "enabled": True},
        ],
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        side_effect=fake_set_state,
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
        return_value={"state": "connected"},
    ):
        await adapter._register_mcp_servers_from_config({}, tag="agent.main")

    # Both were attempted (loop did NOT abort on bad's failure).
    assert calls == ["bad", "good"]
    # bad degraded to disconnected (won't retry next restart).
    assert ("bad", "disconnected") in degraded
    # good was NOT degraded (it registered OK).
    assert not any(n == "good" for n, _ in degraded)


@pytest.mark.asyncio
async def test_init_preflight_false_degrades_to_disconnected(tmp_path, monkeypatch) -> None:
    """_register_mcp_server returns False (preflight unreachable) → degrade,
    not a silent skip that leaves state==connected for infinite retry."""
    adapter = _new_adapter()

    def fake_build(self, entry):
        return _mk_cfg(str(entry.get("name", "")))

    async def fake_register(self, cfg, *, tag):
        return False  # preflight unreachable

    monkeypatch.setattr(type(adapter), "_build_mcp_server_config", fake_build)
    monkeypatch.setattr(type(adapter), "_register_mcp_server", fake_register)

    degraded: list[tuple[str, str]] = []

    def fake_set_state(name, *, state):
        degraded.append((name, state))

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config_yaml_mcp_servers",
        return_value=[{"name": "down", "transport": "sse", "url": "http://down/mcp", "enabled": True}],
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        side_effect=fake_set_state,
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
        return_value={"state": "connected"},
    ):
        await adapter._register_mcp_servers_from_config({}, tag="agent.main")

    assert ("down", "disconnected") in degraded


@pytest.mark.asyncio
async def test_init_promotes_connecting_to_connected(tmp_path, monkeypatch) -> None:
    """A config.yaml MCP whose state.json record is ``connecting`` (restart
    mid-connect) that registers OK is promoted to connected — otherwise the
    frontend would stay "connecting" forever even though the server is live."""
    adapter = _new_adapter()

    def fake_build(self, entry):
        return _mk_cfg(str(entry.get("name", "")))

    async def fake_register(self, cfg, *, tag):
        return True  # registered OK

    monkeypatch.setattr(type(adapter), "_build_mcp_server_config", fake_build)
    monkeypatch.setattr(type(adapter), "_register_mcp_server", fake_register)

    promoted: list[tuple[str, str]] = []

    def fake_set_state(name, *, state):
        promoted.append((name, state))

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config_yaml_mcp_servers",
        return_value=[{"name": "feishu", "transport": "sse", "url": "http://feishu/mcp", "enabled": True}],
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        side_effect=fake_set_state,
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
        return_value={"state": "connecting"},  # left over mid-connect
    ):
        await adapter._register_mcp_servers_from_config({}, tag="agent.main")

    # connecting → connected promoted.
    assert ("feishu", "connected") in promoted


@pytest.mark.asyncio
async def test_init_connected_stays_connected_no_promote(tmp_path, monkeypatch) -> None:
    """A connected MCP that registers OK stays connected — no spurious flip.
    (The promote path only fires when state==connecting.)"""
    adapter = _new_adapter()

    def fake_build(self, entry):
        return _mk_cfg(str(entry.get("name", "")))

    async def fake_register(self, cfg, *, tag):
        return True

    monkeypatch.setattr(type(adapter), "_build_mcp_server_config", fake_build)
    monkeypatch.setattr(type(adapter), "_register_mcp_server", fake_register)

    set_state_calls: list[tuple[str, str]] = []

    def fake_set_state(name, *, state):
        set_state_calls.append((name, state))

    with patch(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.get_config_yaml_mcp_servers",
        return_value=[{"name": "baidu", "transport": "sse", "url": "http://baidu/mcp", "enabled": True}],
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.set_mcp_state",
        side_effect=fake_set_state,
    ), patch(
        "jiuwenswarm.server.runtime.mcp.state_store.get_mcp_record",
        return_value={"state": "connected"},
    ):
        await adapter._register_mcp_servers_from_config({}, tag="agent.main")

    # No set_mcp_state call at all — connected stays connected.
    assert set_state_calls == []
