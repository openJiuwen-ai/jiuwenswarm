# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch
import pytest
from pathlib import Path
from tests.unit_tests.agentserver.mcp.manifest_helpers import write_manifest


# --- from test_fetch_mcp_tools_resolve.py ---

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
    from jiuwenswarm.server.handlers.mcp import _fetch_mcp_tools_from_config

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
        tools = await _fetch_mcp_tools_from_config(entry)

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
    from jiuwenswarm.server.handlers.mcp import _fetch_mcp_tools_from_config

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
        await _fetch_mcp_tools_from_config(entry)
    assert captured_cfg["cfg"].server_path == "https://mcp.notion.com/mcp"

# --- from test_mcp_finalize.py ---

def test_finalize_cli_no_mcp_entry_for_pure_cli(tmp_path: Path) -> None:
    """Pure CLI connectors (feishu: no mcp.json, no `mcp` subcommand) must NOT
    register a stdio MCP entry — the CLI exposes business subcommands
    (docs/im/...) that a SKILL.md teaches the agent to call via exec, not MCP.
    Registering a bogus ``lark-cli mcp`` stdio entry made stdio connect fail.

    Pure CLI still writes a state.json record (carrying skills, no MCP entry)
    so disconnect/enable/disable can find it — that's expected, not an MCP reg.
    """
    mp = tmp_path / "mcp" / "mcp_builtins" / "feishu"
    mp.mkdir(parents=True)
    (mp / "cli.json").write_text(
        '{"runtime":{"type":"node","version":">=18"},'
        '"init":{"win32":"npm install -g @larksuite/cli"},'
        '"versionCheck":{"command":{"win32":"lark-cli.cmd --version"},"minVersion":"1.0.77"},'
        '"auth":[{"command":{"win32":"lark-cli.cmd auth login --recommend"},'
        '"authWaitForExit":true,"authUrlDomain":"accounts.feishu.cn"}],'
        '"unAuth":{"win32":"lark-cli.cmd auth logout"},'
        '"status":{"win32":"lark-cli.cmd auth status"},'
        '"statusMatchJson":{"identity":"user"}}',
        encoding="utf-8",
    )
    write_manifest(mp, "cli", credentials_type="cli-oauth")
    # NOTE: no mcp.json — pure CLI.

    upserts: list[dict] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.cli_driver.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, **e})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills", return_value={"installed": ["lark-doc"]}):
        from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli

        inst = InstallResult(name="feishu", installed=True, version="1.0.80", min_version="1.0.77", version_ok=True)
        result = _finalize_cli("feishu", inst)

    # A state.json record IS written (carries skills, no MCP entry) — that's
    # the pure-CLI state record, not an MCP registration.
    assert len(upserts) == 1
    assert "command" not in upserts[0]  # no stdio command — not an MCP entry
    assert "server_id_scope" in upserts[0]
    # No stdio MCP entry registered (lark-cli has no `mcp` subcommand).
    assert result["mcp_entry"] is None
    assert result["installed_skills"] == ["lark-doc"]

def test_finalize_cli_ignores_undeclared_mcp_file(tmp_path: Path) -> None:
    """A CLI manifest does not infer a second integration from stray mcp.json."""
    mp = tmp_path / "mcp" / "mcp_builtins" / "cloudbase"
    mp.mkdir(parents=True)
    (mp / "cli.json").write_text(
        '{"runtime":{"type":"node","version":">=18"},'
        '"init":{"win32":"npm install -g @cloudbase/cli@latest"},'
        '"versionCheck":{"command":{"win32":"tcb.cmd --version"},"minVersion":"3.6.4"}}',
        encoding="utf-8",
    )
    (mp / "mcp.json").write_text(
        '{"mcpServers":{"cloudbase":{"command":"npx","args":["-y","@cloudbase/cloudbase-mcp@latest"]}}}',
        encoding="utf-8",
    )
    write_manifest(mp, "cli", credentials_type="cli-oauth")

    upserts: list[dict] = []
    with patch("jiuwenswarm.server.runtime.mcp.registry.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.get_workspace_dir", return_value=tmp_path), \
         patch("jiuwenswarm.server.runtime.mcp.state_store.upsert_mcp_record",
               side_effect=lambda n, e, **kw: upserts.append({"name": n, **e})), \
         patch("jiuwenswarm.server.runtime.mcp.skill_installer.install_mcp_skills", return_value={"installed": []}):
        from jiuwenswarm.server.runtime.mcp.cli_driver import InstallResult
        from jiuwenswarm.server.runtime.mcp.registry import _finalize_cli

        inst = InstallResult(name="cloudbase", installed=True, version="3.6.4", min_version="3.6.4", version_ok=True)
        result = _finalize_cli("cloudbase", inst)

    # The undeclared mcp.json does not turn this into a hybrid package.
    assert len(upserts) == 1
    assert "command" not in upserts[0]
    assert result["mcp_entry"] is None

