# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: MCP servers registered at runtime are propagated to subagent specs.

Background: this adapter registers MCP servers at runtime onto the MAIN agent
only (``Runner.resource_mgr`` + main ``ability_manager``), and calls
``create_deep_agent`` without ``mcps=`` — so ``SubAgentConfig.mcps`` stays
empty and subagents (e.g. the general-purpose subagent) never see the
``mcp_<server>_*`` tools the main agent has.

``_propagate_mcps_to_subagent_specs`` mirrors the adapter's live registered
set onto ``_deep_config.subagents``, reusing the same ``McpServerConfig``
objects (same ``server_id``), so a subagent created later via
``DeepAgent.create_subagent`` hits ``_register_pending_mcps``'s
"already registered" branch — it re-tags the shared server onto the subagent
card id and mounts it without spawning a second MCP process.

These tests exercise the propagation rules directly and the
register/unregister hooks that trigger it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.schema.config import SubAgentConfig


def _mk_cfg(name: str, server_id: str) -> McpServerConfig:
    return McpServerConfig(
        server_id=server_id,
        server_name=name,
        server_path=f"stdio://{name}",
        client_type="stdio",
    )


def _gp_spec(mcps=None) -> SubAgentConfig:
    return SubAgentConfig(
        agent_card=AgentCard(name="general-purpose"),
        system_prompt="",
        mcps=list(mcps or []),
    )


def _new_adapter(specs: list[SubAgentConfig] | None = None):
    """A bare JiuWenSwarmDeepAdapter with an instance exposing ``subagents``."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = MagicMock()
    adapter._instance._deep_config.subagents = list(specs or [])
    adapter._registered_mcp_server_ids = set()
    adapter._registered_mcp_servers = {}
    adapter._last_propagated_mcp_ids = set()
    return adapter


def _reg(adapter, cfg: McpServerConfig) -> None:
    adapter._registered_mcp_server_ids.add(cfg.server_id)
    adapter._registered_mcp_servers[cfg.server_id] = cfg


def _unreg(adapter, cfg: McpServerConfig) -> None:
    adapter._registered_mcp_server_ids.discard(cfg.server_id)
    adapter._registered_mcp_servers.pop(cfg.server_id, None)


def _names(spec: SubAgentConfig) -> list[str]:
    return [c.server_name for c in (spec.mcps or [])]


def test_empty_spec_inherits_live_set() -> None:
    adapter = _new_adapter([_gp_spec()])
    cfg = _mk_cfg("career", "sid-1")
    _reg(adapter, cfg)

    adapter._propagate_mcps_to_subagent_specs()

    assert _names(adapter._instance._deep_config.subagents[0]) == ["career"]
    assert adapter._last_propagated_mcp_ids == {"sid-1"}


def test_inherited_set_refreshes_on_later_registration() -> None:
    adapter = _new_adapter([_gp_spec()])
    cfg1 = _mk_cfg("career", "sid-1")
    _reg(adapter, cfg1)
    adapter._propagate_mcps_to_subagent_specs()
    assert _names(adapter._instance._deep_config.subagents[0]) == ["career"]

    cfg2 = _mk_cfg("search", "sid-2")
    _reg(adapter, cfg2)
    adapter._propagate_mcps_to_subagent_specs()

    assert _names(adapter._instance._deep_config.subagents[0]) == ["career", "search"]
    assert adapter._last_propagated_mcp_ids == {"sid-1", "sid-2"}


def test_unregister_drops_inherited_entry() -> None:
    adapter = _new_adapter([_gp_spec()])
    cfg1 = _mk_cfg("career", "sid-1")
    _reg(adapter, cfg1)
    adapter._propagate_mcps_to_subagent_specs()
    assert _names(adapter._instance._deep_config.subagents[0]) == ["career"]

    _unreg(adapter, cfg1)
    adapter._propagate_mcps_to_subagent_specs()

    assert adapter._instance._deep_config.subagents[0].mcps == []
    assert adapter._last_propagated_mcp_ids == set()


def test_own_mcps_are_preserved_and_stale_inherited_stripped() -> None:
    own = _mk_cfg("own-server", "sid-own")
    adapter = _new_adapter([_gp_spec(mcps=[own])])

    inherited = _mk_cfg("career", "sid-1")
    _reg(adapter, inherited)
    adapter._propagate_mcps_to_subagent_specs()
    # Spec declared its own set — not touched by inheritance.
    assert _names(adapter._instance._deep_config.subagents[0]) == ["own-server"]

    # Simulate a mixed state (own entry + a stale propagated entry mixed in by
    # an earlier pass): the propagated entry must be stripped, own kept.
    spec = adapter._instance._deep_config.subagents[0]
    spec.mcps = [own, inherited]
    adapter._last_propagated_mcp_ids = {"sid-1"}
    adapter._propagate_mcps_to_subagent_specs()
    assert _names(adapter._instance._deep_config.subagents[0]) == ["own-server"]


@pytest.mark.asyncio
async def test_register_mcp_server_hook_propagates_to_specs() -> None:
    adapter = _new_adapter([_gp_spec()])
    cfg = _mk_cfg("career", "sid-1")

    with patch(
        "openjiuwen.core.runner.Runner.resource_mgr.add_mcp_server",
        new=AsyncMock(return_value=SimpleNamespace(is_ok=lambda: True)),
    ):
        ok = await adapter._register_mcp_server(cfg, tag="agent.main")

    assert ok is True
    assert _names(adapter._instance._deep_config.subagents[0]) == ["career"]


@pytest.mark.asyncio
async def test_unregister_mcp_server_hook_propagates_to_specs() -> None:
    adapter = _new_adapter([_gp_spec()])
    cfg = _mk_cfg("career", "sid-1")
    _reg(adapter, cfg)
    adapter._propagate_mcps_to_subagent_specs()
    assert _names(adapter._instance._deep_config.subagents[0]) == ["career"]

    with patch(
        "openjiuwen.core.runner.Runner.resource_mgr.remove_mcp_server",
        new=AsyncMock(return_value=None),
    ):
        await adapter._unregister_mcp_server("sid-1")

    assert adapter._instance._deep_config.subagents[0].mcps == []
