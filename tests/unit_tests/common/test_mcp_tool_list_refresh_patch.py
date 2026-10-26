# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise MCP tool-list refresh patch: mid-session new tools become visible."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import mcp_tool_list_refresh_patch as refresh_mod
from jiuwenswarm.common.mcp_tool_list_refresh_patch import (
    apply_mcp_tool_list_refresh_patch,
    refresh_registered_mcp_tool_lists,
    resolve_mcp_tool_list_ttl_s,
)
from openjiuwen.core.foundation.tool import McpServerConfig, McpToolCard
from openjiuwen.core.runner.resources_manager.resource_manager import ResourceMgr
from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr


class _FakeMcpClient:
    def __init__(self, tool_batches: list[list[str]], *, server_name: str = "mock") -> None:
        self._batches = list(tool_batches)
        self._server_name = server_name
        self.connect = AsyncMock(return_value=True)
        self.disconnect = AsyncMock(return_value=True)

    async def list_tools(self, *, timeout: float = -1) -> list[McpToolCard]:  # noqa: ARG002
        names = self._batches.pop(0) if self._batches else []
        cards: list[McpToolCard] = []
        for name in names:
            cards.append(
                McpToolCard(
                    name=name,
                    description=f"tool {name}",
                    input_params={},
                    server_name=self._server_name,
                )
            )
        return cards


@pytest.fixture()
def patched_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Allow re-apply in this process (module may already be patched by other tests).
    monkeypatch.setattr(refresh_mod, "_PATCHED", False)
    apply_mcp_tool_list_refresh_patch()


def test_resolve_ttl_env_and_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_TOOL_LIST_TTL_S", raising=False)
    assert resolve_mcp_tool_list_ttl_s(None) == 60.0
    assert resolve_mcp_tool_list_ttl_s({"mcp": {"tool_list_ttl_s": 15}}) == 15.0
    monkeypatch.setenv("MCP_TOOL_LIST_TTL_S", "0")
    assert resolve_mcp_tool_list_ttl_s({"mcp": {"tool_list_ttl_s": 15}}) == 0.0


@pytest.mark.asyncio
async def test_refresh_exposes_new_tools_in_id_to_card(patched_refresh: None) -> None:
    mgr = ResourceMgr()
    tool_mgr: ToolMgr = mgr._resource_registry.tool()  # noqa: SLF001

    cfg = McpServerConfig(
        server_id="svc-mock",
        server_name="mock-dynamic",
        server_path="http://127.0.0.1:19090/mcp",
        client_type="streamable-http",
    )
    client = _FakeMcpClient(
        [["echo", "ping"], ["echo", "ping", "new_tool"]],
        server_name=cfg.server_name,
    )

    # Bypass _create_client / connect — inject client + first tool snapshot.
    cards1 = await client.list_tools()
    for card in cards1:
        card.id = ToolMgr.generate_mcp_tool_id(cfg.server_id, cfg.server_name, card.name)
        from openjiuwen.core.foundation.tool import MCPTool

        tool_mgr._tools[card.id] = MCPTool(mcp_client=client, tool_info=deepcopy(card))  # noqa: SLF001
        mgr._id_to_card[card.id] = card  # noqa: SLF001
    from openjiuwen.core.runner.resources_manager.tool_manager import McpServerResource

    tool_mgr._mcp_server_resources[cfg.server_id] = McpServerResource(  # noqa: SLF001
        config=cfg,
        client=client,
        tool_ids=[c.id for c in cards1],
        last_update_time=time.time(),
        expiry_time=60.0,
    )
    tool_mgr._mcp_server_name_to_ids[cfg.server_name] = [cfg.server_id]  # noqa: SLF001

    infos_before = await mgr.get_mcp_tool_infos(server_id=cfg.server_id)
    names_before = sorted(str(getattr(i, "name", "") or "") for i in (infos_before or []))
    assert names_before == ["echo", "ping"]

    # Force refresh → second list_tools batch includes new_tool; _id_to_card synced.
    await mgr.refresh_mcp_server(server_id=cfg.server_id, force=True)
    infos_after = await mgr.get_mcp_tool_infos(server_id=cfg.server_id)
    names_after = sorted(str(getattr(i, "name", "") or "") for i in (infos_after or []))
    assert "new_tool" in names_after
    assert set(names_after) == {"echo", "ping", "new_tool"}


@pytest.mark.asyncio
async def test_refresh_registered_helper_detects_change(
    patched_refresh: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openjiuwen.core.runner import Runner

    mgr = ResourceMgr()
    monkeypatch.setattr(Runner, "resource_mgr", mgr, raising=False)

    tool_mgr: ToolMgr = mgr._resource_registry.tool()  # noqa: SLF001
    cfg = McpServerConfig(
        server_id="svc-mock-2",
        server_name="mock-2",
        server_path="http://127.0.0.1:19090/mcp",
        client_type="streamable-http",
    )
    client = _FakeMcpClient([["echo"], ["echo", "new_tool"]], server_name=cfg.server_name)
    cards1 = await client.list_tools()
    for card in cards1:
        card.id = ToolMgr.generate_mcp_tool_id(cfg.server_id, cfg.server_name, card.name)
        from openjiuwen.core.foundation.tool import MCPTool

        tool_mgr._tools[card.id] = MCPTool(mcp_client=client, tool_info=deepcopy(card))  # noqa: SLF001
        mgr._id_to_card[card.id] = card  # noqa: SLF001
    from openjiuwen.core.runner.resources_manager.tool_manager import McpServerResource

    tool_mgr._mcp_server_resources[cfg.server_id] = McpServerResource(  # noqa: SLF001
        config=cfg,
        client=client,
        tool_ids=[c.id for c in cards1],
        last_update_time=time.time(),
        expiry_time=None,
    )

    changed = await refresh_registered_mcp_tool_lists(
        server_ids=[cfg.server_id], ttl_s=0.0
    )
    assert changed is True
    infos = await mgr.get_mcp_tool_infos(server_id=cfg.server_id)
    names = {str(getattr(i, "name", "") or "") for i in (infos or [])}
    assert names == {"echo", "new_tool"}


def _seed_mcp_server(
    mgr: ResourceMgr,
    *,
    server_id: str,
    server_name: str,
    client: _FakeMcpClient,
    cards: list[McpToolCard],
    expiry_time: float | None = 60.0,
) -> None:
    from openjiuwen.core.foundation.tool import MCPTool
    from openjiuwen.core.runner.resources_manager.tool_manager import McpServerResource

    tool_mgr: ToolMgr = mgr._resource_registry.tool()  # noqa: SLF001
    for card in cards:
        card.id = ToolMgr.generate_mcp_tool_id(server_id, server_name, card.name)
        tool_mgr._tools[card.id] = MCPTool(  # noqa: SLF001
            mcp_client=client, tool_info=deepcopy(card)
        )
        mgr._id_to_card[card.id] = card  # noqa: SLF001
    cfg = McpServerConfig(
        server_id=server_id,
        server_name=server_name,
        server_path="http://127.0.0.1:19090/mcp",
        client_type="streamable-http",
    )
    tool_mgr._mcp_server_resources[server_id] = McpServerResource(  # noqa: SLF001
        config=cfg,
        client=client,
        tool_ids=[c.id for c in cards],
        last_update_time=time.time(),
        expiry_time=expiry_time,
    )
    tool_mgr._mcp_server_name_to_ids[server_name] = [server_id]  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_removes_deleted_tools_from_id_to_card(
    patched_refresh: None,
) -> None:
    """Force refresh must drop removed tools from `_id_to_card` (including empty)."""
    mgr = ResourceMgr()
    cfg_id = "svc-mock-del"
    cfg_name = "mock-del"
    client = _FakeMcpClient(
        [["echo", "ping", "new_tool"], ["echo", "ping"], []],
        server_name=cfg_name,
    )
    cards1 = await client.list_tools()
    _seed_mcp_server(
        mgr,
        server_id=cfg_id,
        server_name=cfg_name,
        client=client,
        cards=cards1,
    )
    new_tool_id = ToolMgr.generate_mcp_tool_id(cfg_id, cfg_name, "new_tool")
    assert new_tool_id in mgr._id_to_card  # noqa: SLF001

    # Partial delete: new_tool gone, echo/ping remain.
    await mgr.refresh_mcp_server(server_id=cfg_id, force=True)
    names_partial = {
        str(getattr(i, "name", "") or "")
        for i in (await mgr.get_mcp_tool_infos(server_id=cfg_id) or [])
    }
    assert names_partial == {"echo", "ping"}
    assert new_tool_id not in mgr._id_to_card  # noqa: SLF001

    # Full delete: remote returns no tools — `_id_to_card` must clear orphans.
    await mgr.refresh_mcp_server(server_id=cfg_id, force=True)
    names_empty = [
        i for i in (await mgr.get_mcp_tool_infos(server_id=cfg_id) or []) if i
    ]
    assert names_empty == []
    leftover = [
        tid
        for tid in mgr._id_to_card  # noqa: SLF001
        if str(tid).startswith(f"{cfg_id}.{cfg_name}.")
    ]
    assert leftover == []


@pytest.mark.asyncio
async def test_list_tools_failure_keeps_previous_tools(
    patched_refresh: None,
) -> None:
    """list_tools error must not wipe the previous ToolMgr snapshot."""
    mgr = ResourceMgr()
    tool_mgr: ToolMgr = mgr._resource_registry.tool()  # noqa: SLF001
    cfg_id = "svc-mock-fail"
    cfg_name = "mock-fail"

    class _FailingClient(_FakeMcpClient):
        async def list_tools(self, *, timeout: float = -1):  # noqa: ARG002
            if self._batches:
                return await super().list_tools()
            raise RuntimeError("remote mcp down")

    client = _FailingClient([["echo", "ping"]], server_name=cfg_name)
    cards1 = await client.list_tools()
    _seed_mcp_server(
        mgr,
        server_id=cfg_id,
        server_name=cfg_name,
        client=client,
        cards=cards1,
    )
    before_ids = set(tool_mgr.get_mcp_tool_ids(cfg_id) or [])
    assert before_ids

    with pytest.raises(RuntimeError, match="remote mcp down"):
        await mgr.refresh_mcp_server(server_id=cfg_id, force=True)

    after_ids = set(tool_mgr.get_mcp_tool_ids(cfg_id) or [])
    assert after_ids == before_ids
    names = {
        str(getattr(i, "name", "") or "")
        for i in (await mgr.get_mcp_tool_infos(server_id=cfg_id) or [])
    }
    assert names == {"echo", "ping"}
