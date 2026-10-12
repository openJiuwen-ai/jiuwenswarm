# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: _fetch_mcp_tools_from_config resolves ${VAR} placeholders.

Regression: the temp-connection tool fetcher built its McpServerConfig from
the raw config entry without resolving ``${VAR}`` placeholders — so form B
(token) connectors (baidu-netdisk / tyc-mcp / github) sent the literal
``Bearer ${BAIDU_ACCESS_TOKEN}`` string to the remote MCP, got auth-failed
(usually an empty tool list), and list_tools showed nothing. The fix resolves
placeholders via the CredentialStore before constructing the client.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest


class _FakeCard:
    def __init__(self, name: str) -> None:
        self.id = name
        self.name = name
        self.description = f"desc {name}"
        self.input_params = {}


class _FakeClient:
    """Records the cfg it was built with; returns canned tool cards."""
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.disconnected = False

    async def connect(self) -> bool:
        return True

    async def list_tools(self) -> list[_FakeCard]:
        return [_FakeCard("tool_a"), _FakeCard("tool_b")]

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.mark.anyio
async def test_fetch_resolves_token_placeholder_before_connect(tmp_path, monkeypatch) -> None:
    """${BAIDU_ACCESS_TOKEN} in headers must be replaced with the stored value."""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    # CredentialStore backs onto <workspace>/connectors/credentials/<name>.json.
    # Patch get_workspace_dir so the store reads our temp file.
    cred_dir = tmp_path / "mcp" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    (cred_dir / "baidu-netdisk.json").write_text(
        '{"BAIDU_ACCESS_TOKEN": "real-token-xyz"}', encoding="utf-8"
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_workspace_dir", lambda: tmp_path
    )
    # The store module caches nothing, but it imports get_workspace_dir at
    # call time via the module — patch where credential.py sees it.
    import jiuwenswarm.server.runtime.mcp.credential as cred_mod
    monkeypatch.setattr(cred_mod, "get_workspace_dir", lambda: tmp_path)

    entry = {
        "name": "baidu-netdisk",
        "transport": "sse",
        "url": "https://mcp-pan.baidu.com/sse",
        "headers": {"Authorization": "Bearer ${BAIDU_ACCESS_TOKEN}"},
        "enabled": True,
    }

    captured_cfg: dict[str, Any] = {}
    def fake_create(cfg):
        captured_cfg["cfg"] = cfg
        return _FakeClient(cfg)

    with patch(
        "openjiuwen.core.runner.resources_manager.tool_manager.ToolMgr._create_client",
        side_effect=fake_create,
    ):
        tools = await AgentWebSocketServer._fetch_mcp_tools_from_config(entry)

    # Token was injected into the client cfg's auth_headers (where openjiuwen's
    # SseClient reads it) — not the literal placeholder, not in params.headers.
    sent_headers = captured_cfg["cfg"].auth_headers
    assert sent_headers == {"Authorization": "Bearer real-token-xyz"}
    # params.headers must NOT carry the token (openjiuwen ignores it).
    assert "headers" not in (captured_cfg["cfg"].params or {})
    # Tools surfaced.
    assert len(tools) == 2
    assert tools[0]["name"] == "tool_a"


@pytest.mark.anyio
async def test_fetch_no_placeholder_passes_through_unchanged(tmp_path, monkeypatch) -> None:
    """A form-A entry with no placeholders is not distorted by resolution."""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    cred_dir = tmp_path / "mcp" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_workspace_dir", lambda: tmp_path
    )
    import jiuwenswarm.server.runtime.mcp.credential as cred_mod
    monkeypatch.setattr(cred_mod, "get_workspace_dir", lambda: tmp_path)

    entry = {
        "name": "notion",
        "transport": "http",
        "url": "https://mcp.notion.com/mcp",
        "enabled": True,
    }
    captured_cfg: dict[str, Any] = {}
    with patch(
        "openjiuwen.core.runner.resources_manager.tool_manager.ToolMgr._create_client",
        side_effect=lambda cfg: (captured_cfg.__setitem__("cfg", cfg), _FakeClient(cfg))[1],
    ):
        await AgentWebSocketServer._fetch_mcp_tools_from_config(entry)
    assert captured_cfg["cfg"].server_path == "https://mcp.notion.com/mcp"
