# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: TUI MCP write-path dual-source routing (config.yaml ↔ state.json).

The TUI channel's add/enable/disable/remove/edit route by source: state.json
first (TUI-created / web-connected), config.yaml as the legacy fallback. New
TUI ``add`` always lands in state.json (``enabled=True``); a name already in
config.yaml (legacy stock) is updated in place there instead of migrating.
This keeps the user's view as one undifferentiated "custom MCP" list while the
backend locates each record in whichever file holds it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.common import config as cfg
from jiuwenswarm.server.runtime.mcp import state_store as ss


def _entry(name="dual-a", *, transport="sse", url="http://x/sse"):
    return {"name": name, "transport": transport, "url": url,
            "enabled": True, "server_id_scope": f"mcp:{name}"}


def test_add_new_lands_in_state_json(tmp_path: Path) -> None:
    """A brand-new TUI add goes to state.json (never config.yaml), enabled=True."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        # config.yaml empty so name is not legacy stock.
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": []}})
        entry, created = cfg.upsert_mcp_server(_entry("new-mcp"))
        assert created is True
        rec = ss.get_mcp_record("new-mcp")
        assert rec is not None
        assert rec["enabled"] is True
        assert rec["state"] == "connected"
        # config.yaml untouched.
        servers = cfg.get_config_yaml_mcp_servers()
        assert all(s.get("name") != "new-mcp" for s in servers)


def test_add_existing_in_config_yaml_updates_in_place(tmp_path: Path) -> None:
    """A name already in config.yaml (legacy stock) is updated in place — it
    does NOT migrate to state.json. The source stays where the user put it."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        legacy = {"name": "stock", "transport": "sse", "url": "http://old/sse", "enabled": True}
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": [legacy]}})
        updated = _entry("stock")
        updated["url"] = "http://new/sse"
        entry, created = cfg.upsert_mcp_server(updated)
        assert created is False  # existed in config.yaml
        # Updated in config.yaml, NOT state.json.
        assert ss.get_mcp_record("stock") is None
        yaml_servers = cfg.get_config_yaml_mcp_servers()
        assert any(s.get("name") == "stock" and s.get("url") == "http://new/sse"
                   for s in yaml_servers)


def test_enable_routes_to_state_json(tmp_path: Path) -> None:
    """enable on a state.json record flips it there (not config.yaml)."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": []}})
        ss.upsert_mcp_record("web-mcp", _entry("web-mcp"), enabled=False)
        cfg.set_mcp_server_enabled("web-mcp", True)
        assert ss.get_mcp_record("web-mcp")["enabled"] is True


def test_enable_routes_to_config_yaml_fallback(tmp_path: Path) -> None:
    """enable on a name only in config.yaml flips it there (legacy stock)."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        legacy = {"name": "stock", "transport": "sse", "url": "http://x/sse", "enabled": False}
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": [legacy]}})
        cfg.set_mcp_server_enabled("stock", True)
        servers = cfg.get_config_yaml_mcp_servers()
        assert any(s.get("name") == "stock" and s.get("enabled") is True
                   for s in servers)


def test_remove_routes_to_state_json(tmp_path: Path) -> None:
    """remove on a state.json record deletes it there."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": []}})
        ss.upsert_mcp_record("web-mcp", _entry("web-mcp"), enabled=True)
        cfg.remove_mcp_server("web-mcp")
        assert ss.get_mcp_record("web-mcp") is None


def test_remove_routes_to_config_yaml_fallback(tmp_path: Path) -> None:
    """remove on a name only in config.yaml deletes it there."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        legacy = {"name": "stock", "transport": "sse", "url": "http://x/sse", "enabled": True}
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": [legacy]}})
        cfg.remove_mcp_server("stock")
        assert all(s.get("name") != "stock"
                   for s in cfg.get_config_yaml_mcp_servers())


def test_remove_not_found_raises(tmp_path: Path) -> None:
    """remove on a name in neither source raises KeyError → MCP_NOT_FOUND."""
    with (
        patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path),
        patch("jiuwenswarm.common.config.CONFIG_YAML_PATH", tmp_path / "config.yaml"),
    ):
        cfg.dump_yaml_round_trip(tmp_path / "config.yaml", {"mcp": {"servers": []}})
        with pytest.raises(KeyError):
            cfg.remove_mcp_server("ghost")
