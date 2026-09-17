# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: build_config_entry transport normalization.

openjiuwen's MCP client registry only accepts: sse / stdio / streamable-http
/ streamable_http. Marketplaces use a mix of spellings (``streamableHttp``,
``streamable-http``, ``sse``, or omit ``type``). build_config_entry writes
the config.yaml transport value, so it MUST store one openjiuwen can
register — otherwise reload logs ``Unsupported MCP client type: http`` and
the server never enters the agent.

Only three transports are kept: sse / stdio / streamable-http.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import registry as reg


def _mk_pkg(workspace: Path, name: str, *, mcp: dict[str, Any] | None) -> Path:
    pkg = workspace / "mcp" / "mcp_builtins" / name
    pkg.mkdir(parents=True, exist_ok=True)
    if mcp is not None:
        (pkg / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return pkg


def test_transport_sse_kept(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "baidu", mcp={
        "mcpServers": {"baidu": {"type": "sse", "url": "https://x/sse"}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("baidu")
    assert entry["transport"] == "sse"


def test_transport_streamablehttp_normalized(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "tyc", mcp={
        "mcpServers": {"tyc": {"type": "streamableHttp", "url": "https://x", "headers": {"Authorization": "${K}"}}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("tyc")
    assert entry["transport"] == "streamable-http"


def test_transport_omitted_with_url_defaults_streamable_http(tmp_path: Path) -> None:
    # GitHub/Notion/kdocs: no type, only url -> must NOT be 'http' (no such client)
    _mk_pkg(tmp_path, "github", mcp={
        "mcpServers": {"github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer ${T}"}}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("github")
    assert entry["transport"] == "streamable-http"


def test_transport_http_normalized(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "x", mcp={"mcpServers": {"x": {"type": "http", "url": "https://x"}}})
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("x")
    assert entry["transport"] == "streamable-http"


def test_transport_stdio_when_command(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "gmail", mcp={
        "mcpServers": {"gmail": {"command": "npx", "args": ["-y", "mcp-email"], "env": {"K": "${K}"}}}
    })
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("gmail")
    assert entry["transport"] == "stdio"


def test_transport_streamable_http_underscore_normalized(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "y", mcp={"mcpServers": {"y": {"type": "streamable_http", "url": "https://y"}}})
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path):
        entry = reg.build_config_entry("y")
    assert entry["transport"] == "streamable-http"
