# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: get_mcp_servers merges config.yaml + mcp/state.json.

MCP connection/enabled state lives in state.json; get_mcp_servers remains
the single read entry for the adapter / handlers / registry. It merges both
sources (hand-written config.yaml MCPs + state.json MCPs with state==connected).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import jiuwenswarm.common.config as cfg_mod
from jiuwenswarm.common import utils as common_utils
from jiuwenswarm.server.runtime.mcp import state_store as ss


def _write_config_yaml(tmp_path: Path, servers: list) -> None:
    (tmp_path / "config.yaml").write_text(
        json.dumps({"mcp": {"servers": servers}}) if servers else "{}",
        encoding="utf-8",
    )


def _write_state_json(tmp_path: Path, connectors: dict) -> None:
    d = tmp_path / "mcp"
    d.mkdir(exist_ok=True)
    (d / "state.json").write_text(
        json.dumps({"version": 1, "mcp": connectors, "mounts": {}}),
        encoding="utf-8",
    )


def test_get_mcp_servers_merges_config_yaml_and_state(tmp_path: Path) -> None:
    _write_config_yaml(tmp_path, [
        {"name": "manual-mcp", "transport": "stdio", "command": "npx",
         "args": ["-y", "x"], "enabled": True}
    ])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "url": "https://x/sse",
                  "headers": {"Authorization": "Bearer ${T}"},
                  "state": "connected", "enabled": True,
                  "server_id_scope": "mcp:baidu"}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    names = {s["name"] for s in servers}
    assert names == {"manual-mcp", "baidu"}


def test_state_disconnected_excluded(tmp_path: Path) -> None:
    """Only state==connected connectors surface; disconnected ones don't."""
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "state": "connected", "enabled": True},
        "github": {"transport": "http", "state": "disconnected", "enabled": True}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    names = {s["name"] for s in servers}
    assert names == {"baidu"}


def test_state_connecting_merged_like_connected(tmp_path: Path) -> None:
    """state==connecting is merged into get_mcp_servers (so apply_mcp_change /
    init can register the entry) — same as connected, NOT excluded like
    disconnected/registered. This is what lets the connect handler call
    apply_mcp_change(add) without a pre-flip to connected."""
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "state": "connected", "enabled": True},
        "feishu": {"transport": "http", "state": "connecting", "enabled": True},
        "github": {"transport": "http", "state": "registered", "enabled": True}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    names = {s["name"] for s in servers}
    # connected AND connecting merge; registered does NOT.
    assert names == {"baidu", "feishu"}


def test_skill_only_connector_excluded_from_mcp_servers(tmp_path: Path) -> None:
    """Skill-only connectors (no transport/url/command — only skills) do NOT
    appear in get_mcp_servers. They have no MCP server to register; surfacing
    via the mcp.servers list would make the adapter try to build a fake
    streamable-http entry with no url and log spurious "invalid entry"
    warnings. Skill-only connectors surface via SkillManager instead."""
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "ctrip-wendao": {
            "state": "connected", "enabled": True,
            "integration_type": "skill-only",
            "server_id_scope": "mcp:ctrip-wendao",
            "skills": ["ctrip-wendao"],
        },
        "baidu": {"transport": "sse", "url": "https://x/sse",
                  "state": "connected", "enabled": True,
                  "server_id_scope": "mcp:baidu"},
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    names = {s["name"] for s in servers}
    # ctrip-wendao (skill-only) excluded; baidu (sse) included.
    assert names == {"baidu"}


def test_state_connected_no_enabled_field_defaults_registered(tmp_path: Path) -> None:
    """state.json no longer stores a per-MCP enabled flag (session-level
    enable is driven by chat.send's ``mcp`` field). A connected MCP has no
    ``enabled`` key in its merged entry — extract_enabled_mcp_server_entries
    treats missing as True (default), so connected MCPs register on init."""
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "state": "connected",
                  "url": "https://x", "server_id_scope": "mcp:baidu"}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
        baidu = next(s for s in servers if s["name"] == "baidu")
        assert "enabled" not in baidu  # state.json MCPs carry no enabled flag
        # extract treats missing enabled as True (default) → baidu registers.
        from jiuwenswarm.common.mcp_config import extract_enabled_mcp_server_entries
        enabled_entries = extract_enabled_mcp_server_entries()
        assert any(e["name"] == "baidu" for e in enabled_entries)


def test_state_overrides_config_yaml_on_name_conflict(tmp_path: Path) -> None:
    """During migration overlap, an MCP may exist in both config.yaml
    (stale) and state.json (authoritative). state.json wins on conflict."""
    _write_config_yaml(tmp_path, [
        {"name": "baidu", "transport": "http", "url": "https://stale",
         "enabled": True}
    ])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "url": "https://fresh/sse",
                  "state": "connected", "enabled": True}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    baidu = [s for s in servers if s["name"] == "baidu"]
    assert len(baidu) == 1  # deduped, not duplicated
    assert baidu[0]["transport"] == "sse"  # state.json value wins
    assert baidu[0]["url"] == "https://fresh/sse"


def test_get_mcp_server_config_finds_in_state(tmp_path: Path) -> None:
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "url": "https://x", "state": "connected", "enabled": True}
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        item = cfg_mod.get_mcp_server_config("baidu")
    assert item is not None
    assert item["name"] == "baidu"
    assert cfg_mod.get_mcp_server_config("nope") is None


def test_no_state_file_returns_config_yaml_only(tmp_path: Path) -> None:
    _write_config_yaml(tmp_path, [
        {"name": "manual", "transport": "stdio", "command": "x", "enabled": True}
    ])
    # no state.json
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        servers = cfg_mod.get_mcp_servers()
    assert [s["name"] for s in servers] == ["manual"]


def test_disabled_connector_still_connected_in_list(tmp_path: Path) -> None:
    """A connected MCP stays "connected" in _connected_server_names —
    enable/disable is now session-level (chat.send ``mcp`` field), so the
    stored connection state is unaffected by per-session selection; only
    disconnect removes it. Regression: switching tabs re-fetched mcp.list,
    which used to report MCPs as disconnected spuriously, so the user
    couldn't tell selection from connection."""
    from jiuwenswarm.server.runtime.mcp.registry import _connected_server_names
    _write_config_yaml(tmp_path, [])
    _write_state_json(tmp_path, {
        "baidu": {"transport": "sse", "url": "https://x", "state": "connected",
                  "server_id_scope": "mcp:baidu"},
    })
    with patch.object(cfg_mod, "CONFIG_YAML_PATH", tmp_path / "config.yaml"), \
         patch.object(common_utils, "get_workspace_dir", return_value=tmp_path), \
         patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        names = _connected_server_names()
    assert "baidu" in names

