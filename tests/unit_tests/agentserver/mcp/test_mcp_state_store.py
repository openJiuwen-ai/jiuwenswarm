# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: MCP state store.

state.json is the per-MCP connection store, decoupled from config.yaml.
These tests cover upsert/remove/read + BOM tolerance + connector_to_mcp_entry
shape + placeholder preservation. The ``enabled`` flag is the TUI/global-
default switch (web ignores it, loading by chat.send's ``mcp`` field): upsert
defaults it to False on first insert and carries it through to the entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.server.runtime.mcp import state_store as ss


def _mk_entry(name="baidu", *, transport="sse", url="https://x/sse",
              headers=None, command=None, args=None, env=None):
    e: dict = {"name": name, "transport": transport,
               "server_id_scope": f"mcp:{name}"}
    if url:
        e["url"] = url
    if headers is not None:
        e["headers"] = headers
    if command:
        e["command"] = command
    if args is not None:
        e["args"] = args
    if env is not None:
        e["env"] = env
    return e


def test_upsert_then_read_roundtrip(tmp_path: Path) -> None:
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        entry = _mk_entry(headers={"Authorization": "Bearer ${T}"})
        rec = ss.upsert_mcp_record("baidu", entry)
        assert rec["state"] == "connected"
        got = ss.get_mcp_record("baidu")
        assert got is not None
        assert got["transport"] == "sse"
        assert got["headers"] == {"Authorization": "Bearer ${T}"}
        assert got["state"] == "connected"


def test_upsert_preserves_placeholders(tmp_path: Path) -> None:
    """${VAR} must stay literal — state.json never resolves env vars.
    Token resolution happens later at McpServerConfig build via CredentialStore."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        entry = _mk_entry(headers={"Authorization": "Bearer ${GITHUB_TOKEN}"})
        ss.upsert_mcp_record("github", entry)
        rec = ss.get_mcp_record("github")
        assert rec["headers"]["Authorization"] == "Bearer ${GITHUB_TOKEN}"


def test_remove(tmp_path: Path) -> None:
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("baidu", _mk_entry())
        removed = ss.remove_mcp_record("baidu")
        assert removed is not None
        assert ss.get_mcp_record("baidu") is None
        # idempotent: removing again returns None
        assert ss.remove_mcp_record("baidu") is None


def test_list_connected_includes_connecting(tmp_path: Path) -> None:
    """list_connected_mcps returns connected AND connecting (both are "live":
    registered-with-agent-or-in-progress), but NOT registered/disconnected."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("baidu", _mk_entry(), state="connected")
        ss.upsert_mcp_record("github", _mk_entry("github"), state="disconnected")
        ss.upsert_mcp_record("feishu", _mk_entry("feishu"), state="connecting")
        ss.upsert_mcp_record("dingtalk", _mk_entry("dingtalk"), state="registered")
        connected = ss.list_connected_mcps()
        names = {c["name"] for c in connected}
        assert names == {"baidu", "feishu"}
        # truly_connected excludes connecting — frontend "connected" badge.
        truly = ss.list_truly_connected_mcps()
        assert {c["name"] for c in truly} == {"baidu"}
        # connecting-only helper drives the "connecting" badge.
        conn_ing = ss.list_connecting_mcps()
        assert {c["name"] for c in conn_ing} == {"feishu"}


def test_upsert_merges_fields_preserving_extras(tmp_path: Path) -> None:
    """A second upsert preserves integration_type/skills from prior state if
    not re-supplied (so a re-connect doesn't clobber metadata)."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("feishu", _mk_entry("feishu", transport="http", url=""),
                                  integration_type="cli", skills=["feishu"])
        # Re-upsert with just entry (no integration_type/skills) — prior values stay.
        ss.upsert_mcp_record("feishu", _mk_entry("feishu", transport="http", url=""))
        rec = ss.get_mcp_record("feishu")
        assert rec["integration_type"] == "cli"
        assert rec["skills"] == ["feishu"]


def test_read_tolerates_bom(tmp_path: Path) -> None:
    """A state.json written with a UTF-8 BOM must still parse (matching
    CredentialStore's BOM tolerance)."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        p = ss._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "mcp": {
            "baidu": {"transport": "sse", "state": "connected"}
        }, "mounts": {}})
        p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        rec = ss.get_mcp_record("baidu")
        assert rec is not None
        assert rec["state"] == "connected"


def test_read_missing_returns_empty(tmp_path: Path) -> None:
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        st = ss.read_mcp_state()
        assert st["mcp"] == {}
        assert ss.get_mcp_record("nope") is None


def test_read_corrupt_returns_empty(tmp_path: Path) -> None:
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        p = ss._state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json {{{", encoding="utf-8")
        st = ss.read_mcp_state()
        assert st["mcp"] == {}


def test_connector_to_mcp_entry_shape(tmp_path: Path) -> None:
    """record_to_mcp_entry yields a dict the adapter's
    _build_mcp_server_config can consume identically to a config.yaml entry.
    ``enabled`` is only carried through when the record has it (TUI-managed
    records); a record without it (web-connected, never TUI-enabled) yields no
    ``enabled`` key — same as before, web loads by chat.send's mcp field."""
    record = {
        "transport": "sse", "url": "https://x/sse",
        "headers": {"Authorization": "Bearer ${T}"},
        "server_id_scope": "mcp:baidu",
    }
    entry = ss.record_to_mcp_entry("baidu", record)
    assert entry["name"] == "baidu"
    assert entry["transport"] == "sse"
    assert entry["url"] == "https://x/sse"
    assert entry["headers"] == {"Authorization": "Bearer ${T}"}
    assert "enabled" not in entry
    assert entry["server_id_scope"] == "mcp:baidu"


def test_connector_to_mcp_entry_skill_only_returns_none() -> None:
    """Skill-only connectors (no transport/url/command — only server_id_scope
    + skills) return None. They have no MCP server to register, so they must
    NOT appear in the merged mcp.servers list — otherwise the adapter tries to
    build a McpServerConfig from a fake streamable-http entry with no url and
    logs a spurious "invalid entry" warning on every reload."""
    # skill-only record: only name + server_id_scope + skills, no MCP host
    record = {
        "server_id_scope": "mcp:ctrip-wendao",
        "integration_type": "skill-only",
        "skills": ["ctrip-wendao"],
    }
    assert ss.record_to_mcp_entry("ctrip-wendao", record) is None


def test_connector_to_mcp_entry_pure_cli_returns_none() -> None:
    """Pure CLI connectors (feishu: no mcp.json, tools via skill + CLI binary)
    also return None — same as skill-only, they have no MCP server."""
    record = {
        "server_id_scope": "mcp:feishu",
        "integration_type": "cli",
        "skills": ["lark-doc", "lark-im"],
    }
    assert ss.record_to_mcp_entry("feishu", record) is None


def test_connector_to_mcp_entry_stdio_has_host_returns_entry() -> None:
    """stdio connectors (transport=stdio + command) DO return an entry —
    they have a real MCP server to spawn."""
    record = {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-jira"],
        "env": {"JIRA_TOKEN": "${JIRA_TOKEN}"},
        "server_id_scope": "mcp:jira",
    }
    entry = ss.record_to_mcp_entry("jira", record)
    assert entry is not None
    assert entry["transport"] == "stdio"
    assert entry["command"] == "npx"


def test_persists_across_instances(tmp_path: Path) -> None:
    """Two store accesses over the same workspace share state (file-backed)."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("baidu", _mk_entry())
        # Simulate a "new process" by just calling read again (no in-memory cache).
        assert ss.get_mcp_record("baidu") is not None


def test_upsert_defaults_enabled_false(tmp_path: Path) -> None:
    """First insert defaults enabled=False (TUI/global switch off) so a
    web-connected MCP doesn't pollute the TUI default set. TUI add passes
    enabled=True explicitly."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        rec = ss.upsert_mcp_record("baidu", _mk_entry())
        assert rec["enabled"] is False
        tui_rec = ss.upsert_mcp_record("tui-mcp", _mk_entry("tui-mcp"), enabled=True)
        assert tui_rec["enabled"] is True


def test_upsert_preserves_enabled_when_not_passed(tmp_path: Path) -> None:
    """A later upsert that omits enabled (e.g. a re-connect flipping state)
    must keep the previously set value — connect shouldn't reset the switch."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("baidu", _mk_entry(), enabled=True)
        # Re-upsert without enabled — should stay True.
        rec = ss.upsert_mcp_record("baidu", _mk_entry(), state="connected")
        assert rec["enabled"] is True


def test_set_mcp_enabled_flips_without_touching_state(tmp_path: Path) -> None:
    """TUI enable/disable flips only enabled; connection state stays. Off keeps
    the connection alive — it just hides the MCP from the TUI default set."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record("baidu", _mk_entry(), state="connected", enabled=False)
        ss.set_mcp_enabled("baidu", enabled=True)
        rec = ss.get_mcp_record("baidu")
        assert rec["enabled"] is True
        assert rec["state"] == "connected"  # untouched


def test_record_to_mcp_entry_carries_enabled_when_present(tmp_path: Path) -> None:
    """A record that went through upsert carries its enabled through to the
    entry so the TUI loader can filter on it; a hand-built record without the
    key (legacy/web-shape) yields no enabled key — backward compatible."""
    with patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path):
        # TUI record (enabled=True)
        ss.upsert_mcp_record("tui-mcp", _mk_entry("tui-mcp"), enabled=True)
        rec = ss.get_mcp_record("tui-mcp")
        entry = ss.record_to_mcp_entry("tui-mcp", rec)
        assert entry["enabled"] is True
        # web record (upsert default enabled=False) carries False through
        ss.upsert_mcp_record("web-mcp", _mk_entry("web-mcp"))
        rec2 = ss.get_mcp_record("web-mcp")
        entry2 = ss.record_to_mcp_entry("web-mcp", rec2)
        assert entry2["enabled"] is False
        # legacy record shape (no enabled key) yields no enabled in entry
        legacy = {"transport": "sse", "url": "https://x/sse",
                  "server_id_scope": "mcp:legacy"}
        entry3 = ss.record_to_mcp_entry("legacy", legacy)
        assert "enabled" not in entry3

